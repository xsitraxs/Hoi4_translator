//! The translation engine: providers, cache, validation and the fallback chain.
//!
//! A translation only leaves this module if it survived validation: a value that
//! lost a `$STATE$`, gained a raw quote or broke the one-line format is a failed
//! attempt, and the next engine in the chain gets the same sentence.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::Duration;

use regex::Regex;

use crate::cache::TranslationCache;
use crate::config::SEMAPHORE_LIMIT;
use crate::glossary::Glossary;
use crate::providers::{
    deepl_language_code, fallback_chain, provider_language_code, GOOGLE_ENDPOINT_ENGINE,
};
use crate::text::{
    protect_placeholders, restore_placeholders, split_long_text, PostProcessor, MAX_QUERY_CHARS,
};
use crate::validation::{
    validate_line, validate_preview_translations, BrokenRow, ValidationReport,
};

const GOOGLE_ENDPOINT: &str = "https://translate.googleapis.com/translate_a/single";
const MYMEMORY_ENDPOINT: &str = "https://api.mymemory.translated.net/get";
const DEEPL_FREE_ENDPOINT: &str = "https://api-free.deepl.com/v2/translate";
const DEEPL_PRO_ENDPOINT: &str = "https://api.deepl.com/v2/translate";

/// Attempts per engine, per line.
pub const DEFAULT_RETRIES: usize = 3;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// Why a single value could not be translated.
#[derive(Debug, Clone)]
pub enum TranslationError {
    /// The user stopped the run.
    Cancelled,
    /// Every engine failed; nothing usable came back.
    AllEnginesFailed { last: String },
    /// Every engine answered, but no answer may be written to the game.
    ValidationFailed {
        message: String,
        source: String,
        /// The last answer, kept so the pipeline can validate the row itself and
        /// decide between refusing the file and keeping the English text.
        candidate: String,
        issues: Vec<String>,
        attempts: Vec<String>,
    },
}

impl TranslationError {
    pub fn candidate(&self) -> Option<&str> {
        match self {
            TranslationError::ValidationFailed { candidate, .. } if !candidate.is_empty() => {
                Some(candidate)
            }
            _ => None,
        }
    }
}

impl fmt::Display for TranslationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TranslationError::Cancelled => formatter.write_str("Перевод остановлен пользователем"),
            TranslationError::AllEnginesFailed { last } => write!(
                formatter,
                "Все доступные движки перевода завершились ошибкой ({last})"
            ),
            TranslationError::ValidationFailed { message, .. } => formatter.write_str(message),
        }
    }
}

impl std::error::Error for TranslationError {}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

fn http_agent() -> &'static ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT.get_or_init(|| {
        ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(8))
            .timeout(Duration::from_secs(20))
            .user_agent(concat!("Hoi4Translator/", env!("CARGO_PKG_VERSION")))
            .build()
    })
}

fn placeholder_token_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"<<PH\d+>>").expect("placeholder token regex"))
}

/// Remove every `<<PHn>>` mask, leaving only text that needs translating.
fn strip_placeholder_tokens(text: &str) -> String {
    placeholder_token_re().replace_all(text, "").to_string()
}

fn parse_google_response(body: &str) -> Result<String, String> {
    let value: serde_json::Value = serde_json::from_str(body)
        .map_err(|error| format!("Google вернул некорректный JSON: {error}"))?;
    let mut output = String::new();
    if let Some(segments) = value.get(0).and_then(|item| item.as_array()) {
        for segment in segments {
            if let Some(text) = segment.get(0).and_then(|item| item.as_str()) {
                output.push_str(text);
            }
        }
    }
    if output.is_empty() {
        return Err("Google вернул пустой перевод".to_string());
    }
    Ok(output)
}

fn parse_deepl_response(body: &str) -> Result<String, String> {
    let value: serde_json::Value = serde_json::from_str(body)
        .map_err(|error| format!("DeepL вернул некорректный JSON: {error}"))?;
    let mut output = String::new();
    if let Some(translations) = value.get("translations").and_then(|item| item.as_array()) {
        for translation in translations {
            if let Some(text) = translation.get("text").and_then(|item| item.as_str()) {
                output.push_str(text);
            }
        }
    }
    if output.is_empty() {
        return Err("DeepL вернул пустой перевод".to_string());
    }
    Ok(output)
}

fn parse_mymemory_response(body: &str) -> Result<String, String> {
    let value: serde_json::Value = serde_json::from_str(body)
        .map_err(|error| format!("MyMemory вернул некорректный JSON: {error}"))?;
    if let Some(status) = value.get("responseStatus").and_then(|item| item.as_i64()) {
        if status != 200 {
            let details = value
                .get("responseDetails")
                .and_then(|item| item.as_str())
                .unwrap_or("без подробностей");
            return Err(format!("MyMemory ответил статусом {status}: {details}"));
        }
    }
    let text = value
        .get("responseData")
        .and_then(|data| data.get("translatedText"))
        .and_then(|item| item.as_str())
        .unwrap_or("");
    if text.is_empty() || text.contains("MYMEMORY WARNING") {
        return Err("MyMemory вернул пустой перевод".to_string());
    }
    Ok(text.to_string())
}

// ---------------------------------------------------------------------------
// Translator
// ---------------------------------------------------------------------------

/// A configured translation run: engine, cache, glossary and statistics.
pub struct Translator {
    engine: String,
    source_lang: String,
    api_key: Option<String>,
    concurrency: usize,
    glossary: Option<Glossary>,
    cache: TranslationCache,
    validation_report: Mutex<ValidationReport>,
    fallbacks: Mutex<BTreeMap<String, usize>>,
    /// Sharing cache entries across the fallback chain saves traffic, but it
    /// also means a user who deliberately switched engines keeps receiving the
    /// old text; that trade-off is opt-in.
    reuse_fallback_cache: bool,
}

impl Translator {
    pub fn new(
        engine: &str,
        api_key: Option<String>,
        source_lang: &str,
        concurrency: usize,
        glossary: Option<Glossary>,
    ) -> anyhow::Result<Self> {
        let cache = TranslationCache::open(&crate::config::cache_file())?;
        Ok(Self::with_cache(
            engine,
            api_key,
            source_lang,
            concurrency,
            glossary,
            cache,
        ))
    }

    pub fn with_cache(
        engine: &str,
        api_key: Option<String>,
        source_lang: &str,
        concurrency: usize,
        glossary: Option<Glossary>,
        cache: TranslationCache,
    ) -> Self {
        Self {
            engine: engine.to_string(),
            source_lang: source_lang.to_string(),
            api_key,
            concurrency: concurrency.max(1),
            glossary,
            cache,
            validation_report: Mutex::new(ValidationReport::new()),
            fallbacks: Mutex::new(BTreeMap::new()),
            reuse_fallback_cache: false,
        }
    }

    pub fn engine(&self) -> &str {
        &self.engine
    }

    pub fn source_lang(&self) -> &str {
        &self.source_lang
    }

    pub fn concurrency(&self) -> usize {
        self.concurrency.max(1)
    }

    pub fn glossary(&self) -> Option<&Glossary> {
        self.glossary.as_ref()
    }

    pub fn cache(&self) -> &TranslationCache {
        &self.cache
    }

    pub fn set_reuse_fallback_cache(&mut self, reuse: bool) {
        self.reuse_fallback_cache = reuse;
    }

    fn report(&self) -> MutexGuard<'_, ValidationReport> {
        self.validation_report
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn broken_for_file(&self, file: &str) -> Vec<BrokenRow> {
        self.report().broken_for_file(file)
    }

    pub fn validation_summary(&self) -> String {
        self.report().summary()
    }

    pub fn fallback_counts(&self) -> BTreeMap<String, usize> {
        self.fallbacks
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    fn cache_engines(&self) -> Vec<String> {
        let mut engines = vec![self.engine.clone()];
        if self.reuse_fallback_cache {
            engines.extend(
                fallback_chain(&self.engine)
                    .iter()
                    .map(|name| name.to_string()),
            );
        }
        engines
    }

    fn note_fallback(&self, used_engine: &str) {
        let pair = format!("{} → {used_engine}", self.engine);
        let mut counts = self
            .fallbacks
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *counts.entry(pair).or_insert(0) += 1;
    }

    /// Translate one value with post-processing, validation and fallback.
    pub fn translate_text(
        &self,
        text: &str,
        target_lang: &str,
        file_ctx: &str,
        key_ctx: &str,
    ) -> Result<String, TranslationError> {
        if text.trim().is_empty() {
            return Ok(text.to_string());
        }

        // A glossary changes what the engine is asked, so a cached translation
        // made without it must not be served.
        if self.glossary.is_none() {
            for cached_engine in self.cache_engines() {
                if let Some(cached) =
                    self.cache
                        .get(text, &self.source_lang, target_lang, &cached_engine)
                {
                    let cached_issues =
                        validate_preview_translations(&[text.to_string()], &[cached.clone()]);
                    if !cached_issues.is_empty() {
                        log::warn!(
                            "Invalid cached translation ignored ({cached_engine}): {}",
                            cached_issues.join("; ")
                        );
                        continue;
                    }
                    log::debug!("Cache hit ({cached_engine})");
                    return Ok(cached);
                }
            }
        }

        let (protected, tokens) = protect_placeholders(text, self.glossary.as_ref());

        if strip_placeholder_tokens(&protected).trim().is_empty() {
            // The whole value was a glossary term or a marker: nothing to
            // translate, but the glossary translation still has to be used.
            let restored_only = restore_placeholders(&protected, &tokens);
            return Ok(if restored_only == text {
                text.to_string()
            } else {
                restored_only
            });
        }

        let engines_to_try: Vec<String> = std::iter::once(self.engine.clone())
            .chain(
                fallback_chain(&self.engine)
                    .iter()
                    .map(|name| name.to_string()),
            )
            .collect();

        let mut attempts: Vec<String> = Vec::new();
        let mut issues: Vec<String> = Vec::new();
        let mut candidate = protected.clone();
        let mut answered = false;
        let mut last_error = String::new();

        for engine in &engines_to_try {
            let raw_text = match self.call_engine(engine, &protected, target_lang, DEFAULT_RETRIES)
            {
                Ok(value) => value,
                Err(error) => {
                    attempts.push(format!("{engine}: {error}"));
                    log::warn!("Движок {engine} упал: {error}. Пробуем следующий...");
                    last_error = error;
                    continue;
                }
            };

            answered = true;

            // Restore the masks and judge the answer: a lost marker or a broken
            // format is a failed attempt, not something to hand to the pipeline.
            candidate = restore_placeholders(&raw_text, &tokens);
            issues = validate_line(text, &candidate);
            if issues.is_empty() {
                candidate = PostProcessor::process(&candidate, target_lang);
                issues = validate_line(text, &candidate);
                if !issues.is_empty() {
                    // Post-processing must not silently damage a token that was
                    // intact right after the translation itself.
                    attempts.push(format!(
                        "{engine}: постобработка нарушила формат ({})",
                        issues.join("; ")
                    ));
                }
            } else {
                attempts.push(format!("{engine}: {}", issues.join("; ")));
            }

            if !issues.is_empty() {
                log::warn!(
                    "Движок {engine} вернул битый перевод [{key_ctx}]: {}",
                    issues.join("; ")
                );
                continue;
            }

            if engine != &self.engine {
                // A paid engine silently degrading to a free one must be
                // visible, not only in the log file.
                log::info!("Фолбэк сработал: {} → {engine}", self.engine);
                self.note_fallback(engine);
            }

            {
                let mut report = self.report();
                report.record(file_ctx, key_ctx, &[], false);
            }
            if !candidate.is_empty() && candidate != text && self.glossary.is_none() {
                // Only a value that passed validation may be reused later.
                self.cache
                    .set(text, &candidate, &self.source_lang, target_lang, engine);
            }
            return Ok(candidate);
        }

        if !answered {
            // Never write untranslated source text as if it succeeded.
            log::error!(
                "Все движки недоступны для ключа {}",
                if key_ctx.is_empty() { "?" } else { key_ctx }
            );
            return Err(TranslationError::AllEnginesFailed { last: last_error });
        }

        let report_issues = if issues.is_empty() {
            vec!["перевод не прошёл проверку".to_string()]
        } else {
            issues.clone()
        };
        {
            let mut report = self.report();
            report.record(file_ctx, key_ctx, &report_issues, false);
        }
        Err(TranslationError::ValidationFailed {
            message: format!(
                "Ни один движок не вернул корректный перевод [{}]: {}",
                if key_ctx.is_empty() { "?" } else { key_ctx },
                attempts.join("; ")
            ),
            source: text.to_string(),
            candidate,
            issues,
            attempts,
        })
    }

    fn call_engine(
        &self,
        engine: &str,
        protected: &str,
        target: &str,
        retries: usize,
    ) -> Result<String, String> {
        match engine {
            "Google" => self.google(protected, target, retries),
            "MyMemory" => self.mymemory(protected, target, retries),
            "DeepL" => {
                let Some(api_key) = self.api_key.as_deref().filter(|key| !key.is_empty()) else {
                    return Err("DeepL API key is missing".to_string());
                };
                self.deepl(api_key, protected, target, retries)
            }
            other => Err(format!("Неизвестный движок перевода: {other}")),
        }
    }

    fn google(&self, text: &str, target: &str, retries: usize) -> Result<String, String> {
        let google_target = provider_language_code(GOOGLE_ENDPOINT_ENGINE, target, true);
        let mut output = String::new();
        for chunk in split_long_text(text, MAX_QUERY_CHARS) {
            output.push_str(&self.google_request(&chunk, &google_target, retries)?);
        }
        Ok(output)
    }

    fn google_request(&self, chunk: &str, target: &str, retries: usize) -> Result<String, String> {
        let attempts = retries.max(1);
        let mut last_error = String::from("Google Translate request failed");
        for attempt in 0..attempts {
            let response = http_agent()
                .get(GOOGLE_ENDPOINT)
                .query("client", "gtx")
                .query("sl", &self.source_lang)
                .query("tl", target)
                .query("dt", "t")
                .query("q", chunk)
                .call();
            match response {
                Ok(response) => {
                    let body = response
                        .into_string()
                        .map_err(|error| format!("Google ответил без тела: {error}"))?;
                    return parse_google_response(&body);
                }
                Err(ureq::Error::Status(429, _)) => {
                    last_error =
                        "Google Translate rate-limited the request after all retries".to_string();
                    if attempt + 1 >= attempts {
                        return Err(last_error);
                    }
                    let wait_seconds = 2u64.pow(attempt as u32) * 2;
                    log::warn!("Rate limited (429). Ждём {wait_seconds}s...");
                    std::thread::sleep(Duration::from_secs(wait_seconds));
                }
                Err(error) => {
                    last_error = error.to_string();
                    if attempt + 1 < attempts {
                        std::thread::sleep(Duration::from_millis(1000 * (attempt as u64 + 1)));
                    }
                }
            }
        }
        Err(last_error)
    }

    fn mymemory(&self, text: &str, target: &str, retries: usize) -> Result<String, String> {
        let source_locale = provider_language_code("MyMemory", &self.source_lang, false);
        let target_locale = provider_language_code("MyMemory", target, true);
        let language_pair = format!("{source_locale}|{target_locale}");
        let mut output = String::new();
        for chunk in split_long_text(text, MAX_QUERY_CHARS) {
            output.push_str(&self.mymemory_request(&chunk, &language_pair, retries)?);
        }
        Ok(output)
    }

    fn mymemory_request(
        &self,
        chunk: &str,
        language_pair: &str,
        retries: usize,
    ) -> Result<String, String> {
        let attempts = retries.max(1);
        let mut last_error = String::from("MyMemory request failed");
        for attempt in 0..attempts {
            let response = http_agent()
                .get(MYMEMORY_ENDPOINT)
                .query("q", chunk)
                .query("langpair", language_pair)
                .call();
            match response {
                Ok(response) => {
                    let body = response
                        .into_string()
                        .map_err(|error| format!("MyMemory ответил без тела: {error}"))?;
                    return parse_mymemory_response(&body);
                }
                Err(error) => {
                    last_error = error.to_string();
                    if attempt + 1 < attempts {
                        std::thread::sleep(Duration::from_millis(1000 * (attempt as u64 + 1)));
                    }
                }
            }
        }
        Err(last_error)
    }

    fn deepl(
        &self,
        api_key: &str,
        text: &str,
        target: &str,
        retries: usize,
    ) -> Result<String, String> {
        // Free keys carry the ":fx" suffix; everything else is the paid API.
        let endpoint = if api_key.ends_with(":fx") {
            DEEPL_FREE_ENDPOINT
        } else {
            DEEPL_PRO_ENDPOINT
        };
        let source_code = deepl_language_code(&self.source_lang, false);
        let target_code = deepl_language_code(target, true);
        let attempts = retries.max(1);
        let mut last_error = String::from("DeepL request failed");
        for attempt in 0..attempts {
            let response = http_agent()
                .post(endpoint)
                .set("Authorization", &format!("DeepL-Auth-Key {api_key}"))
                .send_form(&[
                    ("text", text),
                    ("target_lang", target_code.as_str()),
                    ("source_lang", source_code.as_str()),
                ]);
            match response {
                Ok(response) => {
                    let body = response
                        .into_string()
                        .map_err(|error| format!("DeepL ответил без тела: {error}"))?;
                    return parse_deepl_response(&body);
                }
                Err(error) => {
                    last_error = error.to_string();
                    if attempt + 1 < attempts {
                        std::thread::sleep(Duration::from_millis(1500 * (attempt as u64 + 1)));
                    }
                }
            }
        }
        Err(last_error)
    }

    /// Translate a whole file's values, `concurrency` at a time.
    ///
    /// `stop` is polled between values; a stop request ends the batch with
    /// [`TranslationError::Cancelled`] so the caller can leave the file alone.
    pub fn translate_batch(
        &self,
        texts: &[String],
        target_lang: &str,
        keys: Option<&[String]>,
        file_ctx: &str,
        progress: Option<&(dyn Fn(usize, usize) + Sync)>,
        stop: &AtomicBool,
    ) -> Result<Vec<String>, TranslationError> {
        let total = texts.len();
        if total == 0 {
            return Ok(Vec::new());
        }

        let workers = self.concurrency().min(total);
        let next = AtomicUsize::new(0);
        let finished = AtomicUsize::new(0);
        let aborted = AtomicBool::new(false);
        let results: Mutex<Vec<Option<String>>> = Mutex::new(vec![None; total]);
        let fatal: Mutex<Option<TranslationError>> = Mutex::new(None);

        std::thread::scope(|scope| {
            for _ in 0..workers {
                scope.spawn(|| loop {
                    if stop.load(Ordering::SeqCst) || aborted.load(Ordering::SeqCst) {
                        return;
                    }
                    let index = next.fetch_add(1, Ordering::SeqCst);
                    if index >= total {
                        return;
                    }
                    let key = keys
                        .and_then(|keys| keys.get(index))
                        .cloned()
                        .unwrap_or_else(|| index.to_string());

                    match self.translate_text(&texts[index], target_lang, file_ctx, &key) {
                        Ok(value) => {
                            let mut guard = results
                                .lock()
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                            guard[index] = Some(value);
                        }
                        Err(TranslationError::ValidationFailed { candidate, .. }) => {
                            // One unusable line must not cost the rest of the
                            // file: the pipeline validates every row and decides
                            // between refusing the file and keeping the English
                            // text for this line. Nothing was cached.
                            log::warn!("Строка [{key}] не переведена");
                            let fallback = if candidate.is_empty() {
                                texts[index].clone()
                            } else {
                                candidate
                            };
                            let mut guard = results
                                .lock()
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                            guard[index] = Some(fallback);
                        }
                        Err(error) => {
                            let mut slot = fatal
                                .lock()
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                            if slot.is_none() {
                                *slot = Some(error);
                            }
                            aborted.store(true, Ordering::SeqCst);
                            return;
                        }
                    }

                    let done = finished.fetch_add(1, Ordering::SeqCst) + 1;
                    if let Some(progress) = progress {
                        progress(done, total);
                    }
                });
            }
        });

        if let Some(error) = fatal
            .into_inner()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
        {
            return Err(error);
        }
        if stop.load(Ordering::SeqCst) || aborted.load(Ordering::SeqCst) {
            return Err(TranslationError::Cancelled);
        }

        let results = results
            .into_inner()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        Ok(results
            .into_iter()
            .enumerate()
            .map(|(index, value)| value.unwrap_or_else(|| texts[index].clone()))
            .collect())
    }
}

/// Concurrency used when the caller does not specify one.
pub fn default_concurrency() -> usize {
    SEMAPHORE_LIMIT
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An engine name nothing implements: a test that reaches the network is a
    /// test that fails on a build machine with no internet.
    const OFFLINE_ENGINE: &str = "Offline";

    fn offline_translator(glossary: Option<Glossary>) -> Translator {
        Translator::with_cache(
            OFFLINE_ENGINE,
            None,
            "en",
            2,
            glossary,
            TranslationCache::in_memory().expect("cache"),
        )
    }

    #[test]
    fn marker_only_values_are_returned_untouched() {
        let translator = offline_translator(None);
        assert_eq!(
            translator
                .translate_text("$STATE$", "ru", "f.yml", "KEY")
                .expect("ok"),
            "$STATE$"
        );
    }

    #[test]
    fn a_glossary_only_value_uses_the_pinned_translation() {
        let glossary = Glossary::from_pairs(vec![("Division", "дивизия")], "test");
        let translator = offline_translator(Some(glossary));
        assert_eq!(
            translator
                .translate_text("Division", "ru", "f.yml", "KEY")
                .expect("ok"),
            "дивизия"
        );
    }

    #[test]
    fn a_valid_cache_entry_answers_without_an_engine() {
        let translator = offline_translator(None);
        translator.cache().set(
            "Rearm $STATE$",
            "Перевооружите $STATE$",
            "en",
            "ru",
            OFFLINE_ENGINE,
        );
        assert_eq!(
            translator
                .translate_text("Rearm $STATE$", "ru", "f.yml", "KEY")
                .expect("cache hit"),
            "Перевооружите $STATE$"
        );
    }

    #[test]
    fn an_invalid_cache_entry_is_ignored() {
        let translator = offline_translator(None);
        // The cached value lost its marker, so it must not be served; with no
        // network the call then fails instead of returning broken text.
        translator
            .cache()
            .set("Rearm $STATE$", "Перевооружите", "en", "ru", OFFLINE_ENGINE);
        let error = translator
            .translate_text("Rearm $STATE$", "ru", "f.yml", "KEY")
            .expect_err("must not serve a broken cache entry");
        assert!(matches!(error, TranslationError::AllEnginesFailed { .. }));
    }

    #[test]
    fn batch_handles_an_empty_file() {
        let translator = offline_translator(None);
        let stop = AtomicBool::new(false);
        let values = translator
            .translate_batch(&[], "ru", None, "f.yml", None, &stop)
            .expect("empty batch");
        assert!(values.is_empty());
    }

    #[test]
    fn batch_stops_before_starting_when_already_cancelled() {
        let translator = offline_translator(None);
        let stop = AtomicBool::new(true);
        let values = vec!["$A$".to_string(), "$B$".to_string()];
        let error = translator
            .translate_batch(&values, "ru", None, "f.yml", None, &stop)
            .expect_err("cancelled");
        assert!(matches!(error, TranslationError::Cancelled));
    }

    #[test]
    fn google_responses_are_joined_from_their_segments() {
        let body = r#"[[["Перевооружите ","Rearm ",null,null,1],["$STATE$","$STATE$",null,null,1]],null,"en"]"#;
        assert_eq!(
            parse_google_response(body).expect("parsed"),
            "Перевооружите $STATE$"
        );
        assert!(parse_google_response("{}").is_err());
    }

    #[test]
    fn deepl_responses_are_read_from_translations() {
        assert_eq!(
            parse_deepl_response(r#"{"translations":[{"text":"дивизия"}]}"#).expect("parsed"),
            "дивизия"
        );
    }

    #[test]
    fn mymemory_errors_and_warnings_are_rejected() {
        assert!(
            parse_mymemory_response(r#"{"responseStatus":403,"responseDetails":"quota"}"#).is_err()
        );
        assert!(parse_mymemory_response(
            r#"{"responseStatus":200,"responseData":{"translatedText":"MYMEMORY WARNING: quota"}}"#
        )
        .is_err());
        assert_eq!(
            parse_mymemory_response(
                r#"{"responseStatus":200,"responseData":{"translatedText":"дивизия"}}"#
            )
            .expect("parsed"),
            "дивизия"
        );
    }
}
