//! One translation run as a value: settings in, events out.
//!
//! The command line drives the pipeline directly, because it only has a console
//! to talk to. A window needs more: per-file progress, a log it can scroll, a
//! stop button, and an answer at the end. [`run`] is that path — the same
//! modules, in the same order, with every step reported as a [`JobEvent`].
//!
//! The two entry points are kept honest by the tests: a job and a CLI run over
//! the same folders must produce the same files.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::cache::TranslationCache;
use crate::config::{self, LogTag, MAX_CONCURRENCY};
use crate::glossary::Glossary;
use crate::inspect::{format_report, inspect_files};
use crate::localisation::{
    collect_yml_files, is_forced_encoding, is_supported_target, rename_dst,
    validate_directory_relationships, validate_output_path_collisions, FORCED_ENCODINGS,
    HOI4_TARGET_LANGS,
};
use crate::modpack;
use crate::pipeline::{process_file, BrokenLineMode, FileResult, BROKEN_LINE_MODES};
use crate::providers::ENGINES;
use crate::report::RunReport;
use crate::translation::Translator;
use crate::vanilla::build_vanilla_index;

fn default_concurrency() -> usize {
    config::DEFAULT_CONCURRENCY
}

fn default_target() -> String {
    "ru".to_string()
}

fn default_source_lang() -> String {
    "en".to_string()
}

fn default_engine() -> String {
    "Google".to_string()
}

fn default_broken_lines() -> String {
    "reject".to_string()
}

fn default_source_encoding() -> String {
    "auto".to_string()
}

/// The mod to build at the end, when the user asked for one.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ModSpec {
    /// Name shown in the launcher; enables the build.
    pub display_name: String,
    #[serde(default)]
    pub folder: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub supported_version: Option<String>,
    /// `descriptor.mod` of the mod being translated.
    #[serde(default)]
    pub target: Option<PathBuf>,
    #[serde(default)]
    pub install: bool,
    #[serde(default)]
    pub install_dir: Option<PathBuf>,
}

/// Everything a run needs; mirrors the command line one for one.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobSettings {
    /// Mod folder (or its `localisation` folder) with the sources.
    pub src: PathBuf,
    /// Where the translation is written; a separate folder.
    pub dst: PathBuf,
    #[serde(default = "default_target")]
    pub target: String,
    #[serde(default = "default_source_lang")]
    pub source_lang: String,
    #[serde(default = "default_engine")]
    pub engine: String,
    /// Source encoding: `auto`, `utf-8`, `cp1251`, `cp1252` or `latin-1`.
    #[serde(default = "default_source_encoding")]
    pub source_encoding: String,
    #[serde(default)]
    pub api_key: Option<String>,
    #[serde(default)]
    pub glossary: Option<PathBuf>,
    #[serde(default)]
    pub vanilla: Option<PathBuf>,
    #[serde(default = "default_concurrency")]
    pub concurrency: usize,
    #[serde(default)]
    pub skip_translated: bool,
    #[serde(default = "default_broken_lines")]
    pub broken_lines: String,
    /// Parse and report only: nothing is translated or written.
    #[serde(default)]
    pub dry_run: bool,
    #[serde(default)]
    pub no_report: bool,
    /// Per-file budget in seconds; `0` means no limit.
    #[serde(default)]
    pub file_timeout: Option<u64>,
    #[serde(default)]
    pub make_mod: Option<ModSpec>,
}

impl Default for JobSettings {
    fn default() -> Self {
        Self {
            src: PathBuf::new(),
            dst: PathBuf::new(),
            target: default_target(),
            source_lang: default_source_lang(),
            engine: default_engine(),
            source_encoding: default_source_encoding(),
            api_key: None,
            glossary: None,
            vanilla: None,
            concurrency: config::DEFAULT_CONCURRENCY,
            skip_translated: false,
            broken_lines: default_broken_lines(),
            dry_run: false,
            no_report: false,
            file_timeout: None,
            make_mod: None,
        }
    }
}

/// What a run reports while it works.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum JobEvent {
    /// A line for the log panel: `level` is `info`, `warning` or `error`.
    Log { level: String, message: String },
    /// A file was found and queued (`index` is one-based).
    FileStart {
        index: usize,
        total: usize,
        path: String,
    },
    /// Lines translated inside the current file.
    Lines { done: usize, total: usize },
    /// The file is finished: `status` is `written`, `skipped`, `rejected`, …
    FileDone {
        path: String,
        status: String,
        message: String,
    },
    /// The run is over. `report` holds the report paths that were written.
    Done {
        written: usize,
        failed: usize,
        cancelled: bool,
        report: Vec<String>,
        module: Option<String>,
        code: i32,
    },
    /// The run never started, or died: `code` is the exit code to show.
    Failed { message: String, code: i32 },
}

/// Where the events go. The GUI collects them; the tests count them.
pub type Sink = Arc<dyn Fn(JobEvent) + Send + Sync>;

/// What the caller needs after the run.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct JobOutcome {
    pub written: usize,
    pub failed: usize,
    pub cancelled: bool,
    /// The process exit code the same run would have produced on the command line.
    pub code: i32,
}

fn level_name(tag: LogTag) -> &'static str {
    match tag {
        LogTag::Info => "info",
        LogTag::Warning => "warning",
        LogTag::Error => "error",
    }
}

/// A logger in the shape `process_file` and the vanilla index want.
fn logger(sink: &Sink) -> impl FnMut(&str, LogTag) + '_ {
    move |message: &str, tag: LogTag| {
        // The log file is still written: a window that is closed must not lose
        // the record of what happened.
        config::log_line(tag, message);
        sink(JobEvent::Log {
            level: level_name(tag).to_string(),
            message: message.to_string(),
        });
    }
}

/// Machine name of a broken-line mode, the value [`JobSettings::broken_lines`] takes.
pub fn broken_lines_value(mode: BrokenLineMode) -> &'static str {
    match mode {
        BrokenLineMode::RejectFile => "reject",
        BrokenLineMode::KeepOriginal => "keep-original",
    }
}

/// Run one job to completion. Never panics: a failure becomes an event.
pub fn run(settings: &JobSettings, sink: &Sink, stop: &AtomicBool) -> JobOutcome {
    match run_inner(settings, sink, stop) {
        Ok(outcome) => outcome,
        Err(error) => {
            sink(JobEvent::Failed {
                message: error.to_string(),
                code: 2,
            });
            JobOutcome {
                code: 2,
                ..JobOutcome::default()
            }
        }
    }
}

fn run_inner(settings: &JobSettings, sink: &Sink, stop: &AtomicBool) -> Result<JobOutcome> {
    // Argument checks come first, in the order the console makes them: a typo
    // must cost a second, not a full run.
    let source_encoding = settings.source_encoding.trim().to_lowercase();
    if source_encoding != "auto" && !is_forced_encoding(&source_encoding) {
        bail!(
            "Неизвестная кодировка: {}. Доступны: auto, {}",
            settings.source_encoding,
            FORCED_ENCODINGS.join(", ")
        );
    }
    // Always applied: `auto` clears an override left by the previous run.
    crate::localisation::set_source_encoding(&source_encoding);
    // Always set, `None` included: a job without a budget must go back to the
    // calculated one instead of inheriting the previous job's limit.
    crate::pipeline::set_file_timeout(settings.file_timeout.map(|seconds| {
        if seconds == 0 {
            1_000_000_000.0
        } else {
            seconds as f64
        }
    }));

    if !is_supported_target(&settings.target) {
        let available: Vec<&str> = HOI4_TARGET_LANGS.iter().map(|(code, _)| *code).collect();
        bail!(
            "Неизвестный язык перевода: {}. Доступны: {}",
            settings.target,
            available.join(", ")
        );
    }
    if !settings.src.is_dir() {
        bail!("Папка исходников не найдена: {}", settings.src.display());
    }
    validate_directory_relationships(&settings.src, &settings.dst)?;
    if settings.concurrency == 0 || settings.concurrency > MAX_CONCURRENCY {
        bail!("Параллельных запросов должно быть от 1 до {MAX_CONCURRENCY}");
    }
    let mode = BROKEN_LINE_MODES
        .iter()
        .map(|(mode, _)| *mode)
        .find(|mode| broken_lines_value(*mode) == settings.broken_lines.as_str())
        .ok_or_else(|| {
            anyhow::anyhow!(
                "Неизвестный режим битых строк: {}. Доступны: reject, keep-original",
                settings.broken_lines
            )
        })?;
    if settings.make_mod.is_some() && settings.dry_run {
        bail!("Сборку мода нельзя совместить с проверкой: пробный прогон ничего не пишет");
    }
    if let Some(spec) = &settings.make_mod {
        if spec.install_dir.is_some() && !spec.install {
            bail!("Папка установки без установки: мод некуда и незачем копировать");
        }
        if spec.install && spec.display_name.trim().is_empty() {
            bail!("Имя мода для лаунчера не может быть пустым");
        }
    }

    // Everything about the mod is checked here, before a single line is
    // translated: a typo in a folder name must not cost a full run.
    let pack = match &settings.make_mod {
        Some(spec) => {
            let options = pack_options(settings, spec)?;
            modpack::preflight(&options)?;
            Some(options)
        }
        None => None,
    };

    let files = collect_yml_files(&settings.src);
    if files.is_empty() {
        bail!("В папке нет файлов .yml: {}", settings.src.display());
    }
    let mut pairs: Vec<(PathBuf, PathBuf)> = Vec::with_capacity(files.len());
    for source in &files {
        pairs.push((
            source.clone(),
            rename_dst(source, &settings.src, &settings.dst, &settings.target)?,
        ));
    }
    validate_output_path_collisions(&pairs)?;
    let destinations: HashMap<PathBuf, PathBuf> = pairs.iter().cloned().collect();

    let glossary: Option<Glossary> = match settings.glossary.as_deref() {
        Some(path) => Some(Glossary::load(path)?),
        None => None,
    };

    if settings.dry_run {
        let inspections = inspect_files(&files, Some(&destinations));
        for line in format_report(&inspections, &settings.src) {
            sink(JobEvent::Log {
                level: "info".to_string(),
                message: line,
            });
        }
        let blocking = inspections
            .iter()
            .any(|inspection| inspection.has_blocking_problems());
        sink(JobEvent::Done {
            written: 0,
            failed: if blocking { inspections.len() } else { 0 },
            cancelled: false,
            report: Vec::new(),
            module: None,
            code: if blocking { 1 } else { 0 },
        });
        return Ok(JobOutcome {
            code: if blocking { 1 } else { 0 },
            failed: if blocking { inspections.len() } else { 0 },
            ..JobOutcome::default()
        });
    }

    // The JAR-based engine of 1.4 is gone; an old script or a saved setting that
    // still says JavaSim must keep working instead of failing on a name.
    let engine = if settings.engine.trim() == "JavaSim" {
        sink(JobEvent::Log {
            level: "warning".to_string(),
            message: "Движок JavaSim заменён встроенным HTTP-движком Google: \
                      отдельный JAR больше не нужен."
                .to_string(),
        });
        "Google".to_string()
    } else {
        settings.engine.trim().to_string()
    };
    if !ENGINES.contains(&engine.as_str()) {
        bail!(
            "Неизвестный движок перевода: {engine}. Доступны: {}",
            ENGINES.join(", ")
        );
    }
    let api_key = settings
        .api_key
        .clone()
        .or_else(|| std::env::var("DEEPL_API_KEY").ok())
        .filter(|key| !key.is_empty());
    if engine == "DeepL" && api_key.is_none() {
        bail!("Для движка DeepL нужен ключ: укажите его в настройках или в DEEPL_API_KEY");
    }

    let mut log = logger(sink);
    let vanilla = build_vanilla_index(
        settings.vanilla.as_deref(),
        &settings.source_lang,
        &settings.target,
        &mut log,
    )?;
    drop(log);

    let translator = Translator::new(
        &engine,
        api_key,
        &settings.source_lang,
        settings.concurrency,
        glossary,
    )
    .context("не удалось открыть кэш переводов")?;

    let mut report = if settings.no_report {
        None
    } else {
        Some(RunReport::new(
            &engine,
            &settings.source_lang,
            &settings.target,
            &settings.src,
            &settings.dst,
        ))
    };

    sink(JobEvent::Log {
        level: "info".to_string(),
        message: format!(
            "Hoi4 Translator {} | {engine} | {} → {} | файлов: {}",
            config::VERSION,
            settings.source_lang,
            settings.target,
            files.len()
        ),
    });

    let mut written = 0usize;
    let mut failures = 0usize;
    let mut cancelled = false;

    for (index, source) in files.iter().enumerate() {
        let relative = relative_display(source, &settings.src);
        sink(JobEvent::FileStart {
            index: index + 1,
            total: files.len(),
            path: relative.clone(),
        });

        if stop.load(Ordering::SeqCst) {
            cancelled = true;
            break;
        }

        let Some(destination) = destinations.get(source) else {
            failures += 1;
            continue;
        };

        let events = Arc::clone(sink);
        let progress = move |done: usize, total: usize| {
            events(JobEvent::Lines { done, total });
        };
        let progress_ref: &(dyn Fn(usize, usize) + Sync) = &progress;
        let mut log = logger(sink);

        let outcome = process_file(
            source,
            destination,
            &translator,
            &mut log,
            settings.skip_translated,
            stop,
            &settings.target,
            Some(progress_ref),
            mode,
            vanilla.as_ref(),
        );
        drop(log);

        if let Some(report) = report.as_mut() {
            for (key, issues, recovered) in &outcome.broken_rows {
                report.record_broken(&relative, key, issues, *recovered);
            }
            report.record_file(
                &relative,
                destination,
                outcome.status,
                outcome.written,
                outcome.skipped,
                outcome.stale,
                outcome.recovered,
                outcome.broken,
                outcome.reused,
                &outcome.message,
            );
        }

        sink(JobEvent::FileDone {
            path: relative.clone(),
            status: outcome.status.to_string(),
            message: outcome.message.clone(),
        });

        let file_cancelled = matches!(outcome.result, Some(FileResult::Cancelled));
        match outcome.result {
            Some(FileResult::Written(_)) => written += 1,
            Some(FileResult::PreviewCancelled) => {}
            _ => failures += 1,
        }
        if file_cancelled && stop.load(Ordering::SeqCst) {
            cancelled = true;
            sink(JobEvent::Log {
                level: "warning".to_string(),
                message: "Остановлено пользователем: остальные файлы не тронуты".to_string(),
            });
            break;
        }
    }

    // Collected whether or not a report was asked for: the console prints the
    // same numbers, and the log is the only place a user sees them in the window.
    let fallbacks = translator.fallback_counts();
    let mut written_reports: Vec<String> = Vec::new();
    if let Some(report) = report.as_mut() {
        report.set_fallbacks(fallbacks.clone());
        for path in report.write(&settings.dst) {
            written_reports.push(path.display().to_string());
        }
    }
    for (pair, count) in &fallbacks {
        sink(JobEvent::Log {
            level: "info".to_string(),
            message: format!("Фолбэк {pair}: {count} раз"),
        });
    }
    sink(JobEvent::Log {
        level: "info".to_string(),
        message: translator.validation_summary(),
    });

    let mut module = None;
    let mut pack_failed = false;
    if let Some(options) = &pack {
        if failures > 0 {
            // A mod with missing files is worse than no mod: the launcher shows
            // it as a finished translation with holes in it.
            sink(JobEvent::Log {
                level: "warning".to_string(),
                message: format!(
                    "Мод не собран: файлов с ошибками — {failures}. Исправьте их и соберите снова"
                ),
            });
            pack_failed = true;
        } else {
            let mut log = logger(sink);
            let built = modpack::build(options, &mut log);
            drop(log);
            match built {
                Ok(result) => {
                    module = Some(result.folder.display().to_string());
                    sink(JobEvent::Log {
                        level: "info".to_string(),
                        message: format!(
                            "Мод собран: {} (файлов перевода: {})",
                            result.folder.display(),
                            result.files
                        ),
                    });
                    if let Some(installed) = &result.installed {
                        sink(JobEvent::Log {
                            level: "info".to_string(),
                            message: format!("Установлен в папку игры: {}", installed.display()),
                        });
                    }
                }
                Err(error) => {
                    sink(JobEvent::Log {
                        level: "error".to_string(),
                        message: format!("Не удалось собрать мод: {error}"),
                    });
                    pack_failed = true;
                }
            }
        }
    }

    let code = if failures == 0 && !pack_failed { 0 } else { 1 };
    sink(JobEvent::Done {
        written,
        failed: failures,
        cancelled,
        report: written_reports,
        module,
        code,
    });
    Ok(JobOutcome {
        written,
        failed: failures,
        cancelled,
        code,
    })
}

/// Turn the window's mod form into the options the packer takes.
fn pack_options(settings: &JobSettings, spec: &ModSpec) -> Result<modpack::ModPackOptions> {
    let folder = spec
        .folder
        .clone()
        .unwrap_or_else(|| modpack::mod_folder_name(&spec.display_name, &settings.target));
    let target_mod = match spec.target.as_deref() {
        Some(path) => Some(modpack::read_target_descriptor(path)?),
        None => None,
    };
    Ok(modpack::ModPackOptions {
        display_name: spec.display_name.clone(),
        folder,
        version: spec
            .version
            .clone()
            .unwrap_or_else(|| modpack::DEFAULT_MOD_VERSION.to_string()),
        supported_version: spec
            .supported_version
            .clone()
            .or_else(|| {
                target_mod
                    .as_ref()
                    .and_then(|target| target.supported_version.clone())
            })
            .unwrap_or_else(|| modpack::DEFAULT_SUPPORTED_VERSION.to_string()),
        dependency: target_mod.as_ref().and_then(|target| target.name.clone()),
        tags: vec![modpack::DEFAULT_TAG.to_string()],
        target_lang_code: settings.target.clone(),
        source_dir: settings.dst.clone(),
        output_dir: settings.dst.clone(),
        install: spec.install,
        install_dir: spec.install_dir.clone(),
    })
}

/// What `--cache-stats` shows, as lines the window can print verbatim.
pub fn cache_stats() -> Result<Vec<String>> {
    let path = config::cache_file();
    let cache = TranslationCache::open(&path).context("не удалось открыть кэш переводов")?;
    Ok(TranslationCache::describe(&path, &cache.stats()))
}

/// Empty the translation cache; returns how many translations were dropped.
pub fn clear_cache() -> Result<usize> {
    let path = config::cache_file();
    let cache = TranslationCache::open(&path).context("не удалось открыть кэш переводов")?;
    Ok(cache.clear(None, None, None))
}

fn relative_display(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    fn collect_events() -> (Sink, Arc<Mutex<Vec<JobEvent>>>) {
        let events = Arc::new(Mutex::new(Vec::new()));
        let store = Arc::clone(&events);
        let sink: Sink = Arc::new(move |event| {
            store
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .push(event);
        });
        (sink, events)
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "hoi4-job-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).expect("temp dir");
        path
    }

    #[test]
    fn an_unknown_target_language_fails_before_anything_happens() {
        let (sink, events) = collect_events();
        let stop = AtomicBool::new(false);
        let settings = JobSettings {
            src: PathBuf::from("."),
            target: "xx".to_string(),
            ..JobSettings::default()
        };
        let outcome = run(&settings, &sink, &stop);
        assert_eq!(outcome.code, 2);
        let events = events.lock().expect("events");
        assert!(matches!(events.first(), Some(JobEvent::Failed { .. })));
    }

    #[test]
    fn a_dry_run_reports_files_and_writes_nothing() {
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let dir = temp_dir("dry-run");
        let (sink, events) = collect_events();
        let stop = AtomicBool::new(false);
        let settings = JobSettings {
            src: repo.join("examples").join("demo-mod"),
            dst: dir.join("out"),
            dry_run: true,
            ..JobSettings::default()
        };

        let outcome = run(&settings, &sink, &stop);
        assert!(outcome.code == 0 || outcome.code == 1, "{outcome:?}");
        assert!(!dir.join("out").exists(), "a dry run must not write");
        let events = events.lock().expect("events");
        assert!(
            events
                .iter()
                .any(|event| matches!(event, JobEvent::Log { .. })),
            "the report lines are events"
        );
        assert!(matches!(events.last(), Some(JobEvent::Done { .. })));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_page_json_parses_into_the_settings() {
        // Byte for byte what `settingsFromForm` in `assets/gui/index.html`
        // sends, so a renamed field fails here instead of in the window.
        let plain = r#"{"src":"C:\\mods\\MyMod","dst":"C:\\mods\\MyMod-ru","target":"ru",
            "source_lang":"en","engine":"Google","source_encoding":"auto",
            "api_key":null,"glossary":null,
            "vanilla":null,"concurrency":15,"skip_translated":false,
            "broken_lines":"reject","dry_run":false,"no_report":false,
            "file_timeout":null,"make_mod":null}"#;
        let settings: JobSettings = serde_json::from_str(plain).expect("plain settings");
        assert_eq!(settings.target, "ru");
        assert_eq!(settings.concurrency, 15);
        assert_eq!(settings.broken_lines, "reject");
        assert_eq!(settings.source_encoding, "auto");
        assert!(settings.api_key.is_none());
        assert!(settings.make_mod.is_none());

        let with_mod = r#"{"src":"/mods/MyMod","dst":"/mods/MyMod-ru","target":"ru",
            "source_lang":"en","engine":"DeepL","source_encoding":"cp1251",
            "api_key":"key","glossary":null,
            "vanilla":null,"concurrency":4,"skip_translated":true,
            "broken_lines":"keep-original","dry_run":false,"no_report":true,
            "file_timeout":0,"make_mod":{"display_name":"Мой мод","folder":null,
            "version":"2.0.0","supported_version":null,
            "target":"/mods/MyMod/descriptor.mod","install":true,"install_dir":null}}"#;
        let settings: JobSettings = serde_json::from_str(with_mod).expect("mod settings");
        let spec = settings.make_mod.expect("mod spec");
        assert_eq!(spec.display_name, "Мой мод");
        assert_eq!(spec.version.as_deref(), Some("2.0.0"));
        assert_eq!(settings.source_encoding, "cp1251");
        assert!(spec.install);
        assert_eq!(settings.file_timeout, Some(0));
    }

    #[test]
    fn an_unknown_source_encoding_is_a_usage_error() {
        // Only the refusal is tested: a valid value would leak into every other
        // test through the process-wide override.
        let (sink, events) = collect_events();
        let stop = AtomicBool::new(false);
        let settings = JobSettings {
            src: PathBuf::from("."),
            source_encoding: "koi8-r".to_string(),
            ..JobSettings::default()
        };
        assert_eq!(run(&settings, &sink, &stop).code, 2);
        let events = events.lock().expect("events");
        match events.first() {
            Some(JobEvent::Failed { message, code }) => {
                assert_eq!(*code, 2);
                assert!(message.contains("кодировк"), "{message}");
            }
            other => panic!("expected a failure event, got {other:?}"),
        }
    }

    #[test]
    fn a_mod_build_and_a_dry_run_are_refused_together() {
        let (sink, _events) = collect_events();
        let stop = AtomicBool::new(false);
        let settings = JobSettings {
            src: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("examples/demo-mod"),
            dst: std::env::temp_dir().join("hoi4-job-pack-dry"),
            dry_run: true,
            make_mod: Some(ModSpec {
                display_name: "Мод".to_string(),
                ..ModSpec::default()
            }),
            ..JobSettings::default()
        };
        assert_eq!(run(&settings, &sink, &stop).code, 2);
    }

    #[test]
    fn the_mod_is_checked_before_a_single_file_is_translated() {
        // The console checks the mod first so that a typo in a folder name does
        // not cost a full run; the window must behave the same way.
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let (sink, events) = collect_events();
        let stop = AtomicBool::new(false);
        let settings = JobSettings {
            src: repo.join("examples").join("demo-mod"),
            dst: std::env::temp_dir().join("hoi4-job-pack-order"),
            make_mod: Some(ModSpec {
                display_name: "   ".to_string(),
                ..ModSpec::default()
            }),
            ..JobSettings::default()
        };
        assert_eq!(run(&settings, &sink, &stop).code, 2);
        let events = events.lock().expect("events");
        assert!(
            !events
                .iter()
                .any(|event| matches!(event, JobEvent::FileStart { .. })),
            "no file may be started when the mod settings are wrong"
        );
    }

    #[test]
    fn a_missing_source_folder_is_a_usage_error() {
        let (sink, _events) = collect_events();
        let stop = AtomicBool::new(false);
        let settings = JobSettings {
            src: PathBuf::from("/definitely/missing/mod"),
            dst: PathBuf::from("/tmp/whatever"),
            ..JobSettings::default()
        };
        assert_eq!(run(&settings, &sink, &stop).code, 2);
    }

    #[test]
    fn the_engine_and_its_key_are_checked_up_front() {
        let (sink, _events) = collect_events();
        let stop = AtomicBool::new(false);
        let settings = JobSettings {
            src: PathBuf::from(env!("CARGO_MANIFEST_DIR")),
            dst: std::env::temp_dir().join("hoi4-job-deepl"),
            engine: "DeepL".to_string(),
            api_key: None,
            ..JobSettings::default()
        };
        // The environment may carry a key for other tests; only the refusal
        // without one is interesting.
        if std::env::var("DEEPL_API_KEY").is_err() {
            assert_eq!(run(&settings, &sink, &stop).code, 2);
        }
    }
}
