//! Per-file processing pipeline: parse, reuse, translate, validate, write.
//!
//! The pipeline is the only place that writes a translated `.yml`. It decides,
//! per file, whether the result may be written at all: duplicate keys, malformed
//! lines or a translation that broke the markup mean the file is left alone,
//! because a broken file can keep the game from starting.

use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use crate::config::{atomic_write_localisation, LogTag};
use crate::localisation::{
    get_hoi4_language_name, load_existing_translations, normalize_key,
    prepare_target_localisation_lines, read_localisation_lines, source_text_hash,
    split_line_ending, split_value_line,
};
use crate::translation::{TranslationError, Translator};
use crate::validation::{split_row_issues, validate_pairs, validate_preview_translations};
use crate::vanilla::VanillaIndex;

/// What to do with a translated value that breaks the HOI4 format.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BrokenLineMode {
    /// Refuse to write the file: never ship a file that can crash the game.
    RejectFile,
    /// Write the file but keep the English text for the offending line.
    KeepOriginal,
}

pub const BROKEN_LINE_MODES: [(BrokenLineMode, &str); 2] = [
    (BrokenLineMode::RejectFile, "Не записывать файл"),
    (
        BrokenLineMode::KeepOriginal,
        "Заменить битую строку оригиналом",
    ),
];

pub fn broken_line_mode_from_label(label: &str) -> BrokenLineMode {
    BROKEN_LINE_MODES
        .iter()
        .find(|(_, mode_label)| *mode_label == label)
        .map(|(mode, _)| *mode)
        .unwrap_or(BrokenLineMode::RejectFile)
}

pub fn broken_line_label(mode: BrokenLineMode) -> &'static str {
    BROKEN_LINE_MODES
        .iter()
        .find(|(candidate, _)| *candidate == mode)
        .map(|(_, label)| *label)
        .unwrap_or(BROKEN_LINE_MODES[0].1)
}

/// A value that had to be repaired by writing the English text back is stored
/// with an empty fingerprint: the stored text is knowingly not a translation, so
/// the next run must ask the engine again instead of skipping it.
pub const RECOVERED_HASH_MARKER: &str = "";

/// Outcome of one file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FileResult {
    Written(usize),
    PreviewCancelled,
    Cancelled,
    Rejected,
    Error,
}

impl FileResult {
    pub fn is_written(self) -> bool {
        matches!(self, FileResult::Written(_))
    }
}

/// Everything the run report needs to know about one file.
#[derive(Debug, Default, Clone)]
pub struct FileOutcome {
    pub result: Option<FileResult>,
    pub status: &'static str,
    pub message: String,
    pub written: usize,
    pub skipped: usize,
    pub stale: usize,
    pub recovered: usize,
    pub broken: usize,
    pub reused: usize,
    /// `(key, issues, recovered)` for every row the report should mention.
    pub broken_rows: Vec<(String, Vec<String>, bool)>,
    pub repaired: usize,
}

/// Scale the per-file budget with the number of lines to translate.
///
/// A fixed cap silently dropped big files: retries with exponential backoff can
/// legitimately cost tens of seconds for a single line.
pub fn file_translation_timeout(line_count: usize) -> f64 {
    if let Some(Some(seconds)) = file_timeout_override().get() {
        return *seconds;
    }
    120.0f64.max(60.0 + 0.35 * line_count as f64)
}

/// `--file-timeout`, set once by the CLI; `None` keeps the formula above.
fn file_timeout_override() -> &'static OnceLock<Option<f64>> {
    static CELL: OnceLock<Option<f64>> = OnceLock::new();
    &CELL
}

/// Replace the per-file budget. `None` restores the calculated default.
pub fn set_file_timeout(seconds: Option<f64>) {
    let _ = file_timeout_override().set(seconds);
}

fn finish(
    outcome: &mut FileOutcome,
    status: &'static str,
    message: &str,
    result: FileResult,
) -> FileResult {
    outcome.status = status;
    outcome.message = message.to_string();
    outcome.result = Some(result);
    result
}

/// One `(prefix, suffix, line ending)` triple per translated row.
struct RowMeta {
    prefix: String,
    suffix: String,
    line_ending: String,
}

/// Translate one file. The caller records the returned statistics in the report.
#[allow(clippy::too_many_arguments)]
pub fn process_file(
    src_path: &Path,
    dst_path: &Path,
    translator: &Translator,
    log: &mut dyn FnMut(&str, LogTag),
    skip_translated: bool,
    stop: &AtomicBool,
    target_lang: &str,
    line_progress: Option<&(dyn Fn(usize, usize) + Sync)>,
    broken_line_mode: BrokenLineMode,
    vanilla: Option<&VanillaIndex>,
) -> FileOutcome {
    let mut outcome = FileOutcome::default();
    process_file_inner(
        src_path,
        dst_path,
        translator,
        log,
        skip_translated,
        stop,
        target_lang,
        line_progress,
        broken_line_mode,
        vanilla,
        &mut outcome,
    );
    outcome
}

#[allow(clippy::too_many_arguments)]
fn process_file_inner(
    src_path: &Path,
    dst_path: &Path,
    translator: &Translator,
    log: &mut dyn FnMut(&str, LogTag),
    skip_translated: bool,
    stop: &AtomicBool,
    target_lang: &str,
    line_progress: Option<&(dyn Fn(usize, usize) + Sync)>,
    broken_line_mode: BrokenLineMode,
    vanilla: Option<&VanillaIndex>,
    outcome: &mut FileOutcome,
) -> FileResult {
    let rel_path = src_path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();

    let target_name = match get_hoi4_language_name(target_lang) {
        Ok(name) => name,
        Err(error) => {
            log(&format!("  {error}"), LogTag::Error);
            return finish(
                outcome,
                "rejected",
                "неверный целевой язык",
                FileResult::Rejected,
            );
        }
    };

    let required_suffix = format!("_l_{target_name}.yml");
    let dst_name = dst_path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    if !dst_name
        .to_lowercase()
        .ends_with(&required_suffix.to_lowercase())
    {
        log(
            &format!("  Файл пропущен: имя результата должно заканчиваться на {required_suffix}"),
            LogTag::Error,
        );
        return finish(
            outcome,
            "rejected",
            "неверное имя результата",
            FileResult::Rejected,
        );
    }

    if let Some(parent) = dst_path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }

    let existing = if skip_translated {
        load_existing_translations(dst_path, &mut *log)
    } else {
        HashMap::new()
    };
    if !existing.is_empty() {
        log(
            &format!("  Найдено переведённых строк в файле: {}", existing.len()),
            LogTag::Info,
        );
    }

    let source_lines = match read_localisation_lines(src_path, &mut *log) {
        Ok((lines, _encoding)) => lines,
        Err(error) => {
            log(
                &format!("  Ошибка чтения исходника: {error}"),
                LogTag::Error,
            );
            return finish(
                outcome,
                "error",
                "не удалось прочитать исходник",
                FileResult::Error,
            );
        }
    };

    let Some(prepared) = prepare_target_localisation_lines(
        &source_lines,
        target_name,
        Some(translator.source_lang()),
        &mut *log,
    ) else {
        return finish(
            outcome,
            "rejected",
            "не совпал исходный язык",
            FileResult::Rejected,
        );
    };
    let lines = prepared.lines;
    let header_indexes = prepared.header_indexes;
    let structure_changed = prepared.structure_changed;

    let mut out_lines = lines.clone();
    let mut to_translate_idx: Vec<usize> = Vec::new();
    let mut to_translate_val: Vec<String> = Vec::new();
    let mut to_translate_keys: Vec<String> = Vec::new();
    let mut meta: Vec<RowMeta> = Vec::new();
    let mut skipped = 0usize;
    let mut stale_source_keys: Vec<String> = Vec::new();
    let mut unverifiable_keys: Vec<String> = Vec::new();
    let mut malformed_lines: Vec<usize> = Vec::new();
    let mut key_line_numbers: HashMap<String, Vec<usize>> = HashMap::new();
    let mut key_order: Vec<String> = Vec::new();

    // Remember which source text produced every written value, so an edited
    // source line is re-translated instead of answered with a stale translation.
    let cache = translator.cache();
    let recorded_hashes: BTreeMap<String, String> = if skip_translated {
        cache.get_source_hashes(dst_path)
    } else {
        BTreeMap::new()
    };
    let mut file_key_hashes: BTreeMap<String, String> = BTreeMap::new();
    let mut repaired_keys: Vec<String> = Vec::new();

    for (index, line) in lines.iter().enumerate() {
        if stop.load(Ordering::SeqCst) {
            return finish(
                outcome,
                "cancelled",
                "остановлено пользователем",
                FileResult::Cancelled,
            );
        }
        if header_indexes.contains(&index) {
            continue;
        }

        let (line_content, line_ending) = split_line_ending(line);
        let Some(parts) = split_value_line(line_content) else {
            let stripped = line_content.trim();
            if !stripped.is_empty() && !stripped.starts_with('#') && stripped.contains(':') {
                malformed_lines.push(index + 1);
            }
            continue;
        };

        // KEY:0 and KEY:1 are the same localisation key (the number is only a
        // revision marker), so comparisons and duplicate detection use the
        // version-less form.
        let raw_key = parts.prefix.trim().to_string();
        let key = normalize_key(&parts.prefix);
        if !key_line_numbers.contains_key(&key) {
            key_order.push(key.clone());
        }
        key_line_numbers
            .entry(key.clone())
            .or_default()
            .push(index + 1);
        file_key_hashes.insert(key.clone(), source_text_hash(&parts.value));

        if skip_translated {
            if let Some(existing_value) = existing.get(&key) {
                // Hashes written by older versions are keyed by "KEY:0".
                let known_hash = recorded_hashes
                    .get(&key)
                    .or_else(|| recorded_hashes.get(&raw_key));
                let source_changed = known_hash
                    .map(|hash| {
                        hash.as_str() != RECOVERED_HASH_MARKER && *hash != file_key_hashes[&key]
                    })
                    .unwrap_or(false);

                if known_hash
                    .map(|hash| hash.as_str() == RECOVERED_HASH_MARKER)
                    .unwrap_or(false)
                {
                    // The previous run repaired this line with English text; it
                    // is not a translation and must be translated again.
                    repaired_keys.push(key.clone());
                } else if source_changed {
                    // The mod author edited the English line: the stored value
                    // describes the previous text and must not be reused.
                    stale_source_keys.push(key.clone());
                } else {
                    if known_hash.is_none() {
                        // Written before fingerprints existed, or translated by
                        // hand: reusing it is safe, but it can never be proven
                        // up to date, so the file-level log says so.
                        unverifiable_keys.push(key.clone());
                    }
                    let existing_issues = validate_preview_translations(
                        &[parts.value.clone()],
                        &[existing_value.clone()],
                    );
                    if existing_issues.is_empty() {
                        out_lines[index] = format!(
                            "{}\"{}\"{}{}",
                            parts.prefix, existing_value, parts.suffix, line_ending
                        );
                        skipped += 1;
                        continue;
                    }
                    log(
                        &format!(
                            "  Сохранённый перевод [{key}] отклонён: {}; строка будет \
                             переведена заново",
                            existing_issues.join(", ")
                        ),
                        LogTag::Warning,
                    );
                }
            }
        }

        // The game already translates many strings the mod copies verbatim.
        if let Some(vanilla) = vanilla {
            if let Some(reused_value) = vanilla.lookup(&parts.value) {
                let reused_issues =
                    validate_preview_translations(&[parts.value.clone()], &[reused_value.clone()]);
                if reused_issues.is_empty() {
                    out_lines[index] = format!(
                        "{}\"{}\"{}{}",
                        parts.prefix, reused_value, parts.suffix, line_ending
                    );
                    outcome.reused += 1;
                    continue;
                }
                log(
                    &format!(
                        "  Ванильный перевод [{key}] отклонён проверкой формата: {}; строка \
                         будет переведена движком",
                        reused_issues.join(", ")
                    ),
                    LogTag::Warning,
                );
            }
        }

        to_translate_idx.push(index);
        to_translate_val.push(parts.value);
        to_translate_keys.push(key);
        meta.push(RowMeta {
            prefix: parts.prefix,
            suffix: parts.suffix,
            line_ending: line_ending.to_string(),
        });
    }

    outcome.skipped = skipped;
    outcome.stale = stale_source_keys.len() + repaired_keys.len();
    outcome.repaired = repaired_keys.len();

    if !unverifiable_keys.is_empty() {
        log(
            &format!(
                "  {} строк из предыдущего результата приняты без сверки с исходником \
                 (нет контрольных сумм); при изменении исходника они не обновятся — \
                 снимите «Пропускать переведённое», чтобы перевести файл заново",
                unverifiable_keys.len()
            ),
            LogTag::Warning,
        );
    }

    if !malformed_lines.is_empty() {
        let rows: Vec<String> = malformed_lines
            .iter()
            .take(10)
            .map(|row| row.to_string())
            .collect();
        let remaining = malformed_lines.len().saturating_sub(10);
        let more = if remaining > 0 {
            format!(" и ещё {remaining}")
        } else {
            String::new()
        };
        log(
            &format!(
                "  Файл пропущен: строки {}{more} не соответствуют формату локализации \
                 (ключ:\"значение\"); проверьте ключи, кавычки и версию",
                rows.join(", ")
            ),
            LogTag::Error,
        );
        return finish(
            outcome,
            "rejected",
            "строки не соответствуют формату",
            FileResult::Rejected,
        );
    }

    let duplicates: Vec<(&String, &Vec<usize>)> = key_order
        .iter()
        .filter_map(|key| key_line_numbers.get(key).map(|rows| (key, rows)))
        .filter(|(_, rows)| rows.len() > 1)
        .collect();
    if !duplicates.is_empty() {
        let details = duplicates
            .iter()
            .take(10)
            .map(|(key, rows)| {
                let rows: Vec<String> = rows.iter().map(|row| row.to_string()).collect();
                format!("{key} (строки {})", rows.join(", "))
            })
            .collect::<Vec<_>>()
            .join("; ");
        let remaining = duplicates.len().saturating_sub(10);
        let more = if remaining > 0 {
            format!("; и ещё {remaining} дубликатов")
        } else {
            String::new()
        };
        log(
            &format!("  Файл не записан: повторяющиеся localization keys: {details}{more}"),
            LogTag::Error,
        );
        return finish(
            outcome,
            "rejected",
            "повторяющиеся ключи",
            FileResult::Rejected,
        );
    }

    let total = to_translate_val.len();
    if total == 0 {
        if structure_changed {
            if let Err(error) = atomic_write_localisation(dst_path, &out_lines) {
                log(
                    &format!("  Не удалось записать файл: {error}"),
                    LogTag::Error,
                );
                return finish(outcome, "error", "ошибка записи", FileResult::Error);
            }
        }
        cache.set_source_hashes(dst_path, &file_key_hashes);
        if !stale_source_keys.is_empty() {
            log(
                &format!(
                    "  Исходник изменился у {} строк; перевод обновлён",
                    stale_source_keys.len()
                ),
                LogTag::Info,
            );
        }
        if skipped > 0 {
            log(
                &format!("  Пропущено (уже переведено): {skipped}"),
                LogTag::Info,
            );
        }
        if outcome.reused > 0 {
            log(
                &format!("  Из ванильного перевода: {}", outcome.reused),
                LogTag::Info,
            );
        }
        outcome.written = 0;
        return finish(outcome, "written", "", FileResult::Written(0));
    }

    // ---- translation, with a per-file budget ------------------------------
    let file_stop = AtomicBool::new(false);
    let finished = AtomicBool::new(false);
    let timed_out = AtomicBool::new(false);
    let budget = Duration::from_secs_f64(file_translation_timeout(total));
    let deadline = Instant::now() + budget;

    let batch_result = std::thread::scope(|scope| {
        let watcher = scope.spawn(|| loop {
            if finished.load(Ordering::SeqCst) {
                return;
            }
            if stop.load(Ordering::SeqCst) {
                file_stop.store(true, Ordering::SeqCst);
                return;
            }
            if Instant::now() >= deadline {
                timed_out.store(true, Ordering::SeqCst);
                file_stop.store(true, Ordering::SeqCst);
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        });

        let result = translator.translate_batch(
            &to_translate_val,
            target_lang,
            Some(&to_translate_keys),
            &rel_path,
            line_progress,
            &file_stop,
        );
        finished.store(true, Ordering::SeqCst);
        let _ = watcher.join();
        result
    });

    let translated_values = match batch_result {
        Ok(values) => values,
        Err(TranslationError::Cancelled) => {
            if timed_out.load(Ordering::SeqCst) {
                log(
                    &format!(
                        "  Превышено время перевода файла ({:.0} с на {total} строк); файл \
                         не записан",
                        budget.as_secs_f64()
                    ),
                    LogTag::Error,
                );
                return finish(
                    outcome,
                    "error",
                    "превышено время перевода",
                    FileResult::Error,
                );
            }
            log(
                "  Перевод файла остановлен; файл не записан",
                LogTag::Warning,
            );
            return finish(
                outcome,
                "cancelled",
                "остановлено пользователем",
                FileResult::Cancelled,
            );
        }
        Err(error) => {
            log(&format!("  Ошибка перевода: {error}"), LogTag::Error);
            let message = error.to_string();
            return finish(outcome, "error", &message, FileResult::Error);
        }
    };

    if stop.load(Ordering::SeqCst) {
        return finish(
            outcome,
            "cancelled",
            "остановлено пользователем",
            FileResult::Cancelled,
        );
    }

    let multiline_rows: Vec<usize> = translated_values
        .iter()
        .enumerate()
        .filter(|(_, value)| value.contains('\n') || value.contains('\r'))
        .map(|(index, _)| index + 1)
        .collect();
    if !multiline_rows.is_empty() {
        let rows: Vec<String> = multiline_rows
            .iter()
            .take(10)
            .map(|row| row.to_string())
            .collect();
        log(
            &format!(
                "  Файл не записан: перевод вернул физические переносы строк в значениях \
                 (строки {}); используйте литеральный \\n",
                rows.join(", ")
            ),
            LogTag::Error,
        );
        return finish(
            outcome,
            "rejected",
            "переносы строк в значениях",
            FileResult::Rejected,
        );
    }

    // A single machine-translated line can break the format. Per-line recovery
    // keeps the other 2999 lines of the file instead of losing them.
    let row_issues = match validate_pairs(&to_translate_val, &translated_values) {
        Ok(rows) => rows,
        Err(error) => {
            log(&format!("  Файл не записан: {error}"), LogTag::Error);
            return finish(
                outcome,
                "rejected",
                "перевод нарушает формат",
                FileResult::Rejected,
            );
        }
    };

    let mut approved_values = translated_values.clone();
    let mut introduced_rows: Vec<(usize, Vec<String>)> = Vec::new();
    let mut inherited_rows: Vec<(usize, Vec<String>)> = Vec::new();
    for (index, issues) in row_issues.iter().enumerate() {
        if issues.is_empty() {
            continue;
        }
        let (inherited, introduced) = split_row_issues(&to_translate_val[index], issues);
        if introduced.is_empty() {
            inherited_rows.push((index, inherited));
        } else {
            introduced_rows.push((index, introduced));
        }
    }

    // The mod's own markup problems are written as they are and only reported.
    for (index, issues) in &inherited_rows {
        let key = &to_translate_keys[*index];
        log(
            &format!(
                "  Строка [{key}] записана как есть, проблема в исходнике: {}",
                issues.join(", ")
            ),
            LogTag::Warning,
        );
        outcome
            .broken_rows
            .push((key.clone(), issues.clone(), false));
    }

    if !introduced_rows.is_empty() {
        if broken_line_mode != BrokenLineMode::KeepOriginal {
            let problems = introduced_rows
                .iter()
                .take(5)
                .map(|(index, issues)| format!("Строка {}: {}", index + 1, issues.join(", ")))
                .collect::<Vec<_>>()
                .join("; ");
            log(
                &format!("  Файл не записан: перевод нарушает формат локализации: {problems}"),
                LogTag::Error,
            );
            return finish(
                outcome,
                "rejected",
                "перевод нарушает формат",
                FileResult::Rejected,
            );
        }

        for (index, issues) in &introduced_rows {
            let key = to_translate_keys[*index].clone();
            log(
                &format!(
                    "  Строка [{key}] заменена оригиналом: {}",
                    issues.join(", ")
                ),
                LogTag::Warning,
            );
            outcome.recovered += 1;
            outcome
                .broken_rows
                .push((key.clone(), issues.clone(), true));
            approved_values[*index] = to_translate_val[*index].clone();
            // The English text is now in the target file; without the marker the
            // next run would treat it as a finished translation and keep it.
            file_key_hashes.insert(key, RECOVERED_HASH_MARKER.to_string());
        }
        log(
            &format!(
                "  Битых строк заменено оригиналом: {} (исправьте их вручную или переведите \
                 файл заново)",
                introduced_rows.len()
            ),
            LogTag::Warning,
        );
    }

    let broken_in_file = translator.broken_for_file(&rel_path);
    for entry in &broken_in_file {
        if !outcome
            .broken_rows
            .iter()
            .any(|(key, _, recovered)| *recovered && key == &entry.key)
        {
            outcome.broken += 1;
            outcome
                .broken_rows
                .push((entry.key.clone(), entry.issues.clone(), false));
        }
    }

    let mut output = out_lines.clone();
    for ((index, translated), row_meta) in to_translate_idx
        .iter()
        .zip(approved_values.iter())
        .zip(meta.iter())
    {
        output[*index] = format!(
            "{}\"{}\"{}{}",
            row_meta.prefix, translated, row_meta.suffix, row_meta.line_ending
        );
    }

    if let Err(error) = atomic_write_localisation(dst_path, &output) {
        log(
            &format!("  Не удалось записать файл: {error}"),
            LogTag::Error,
        );
        return finish(outcome, "error", "ошибка записи", FileResult::Error);
    }
    cache.set_source_hashes(dst_path, &file_key_hashes);
    outcome.written = total;

    if !stale_source_keys.is_empty() {
        log(
            &format!(
                "  Исходник изменился у {} строк; перевод обновлён",
                stale_source_keys.len()
            ),
            LogTag::Info,
        );
    }
    if !repaired_keys.is_empty() {
        log(
            &format!(
                "  {} строк, восстановленных в прошлом прогоне, переведены заново",
                repaired_keys.len()
            ),
            LogTag::Info,
        );
    }
    if skipped > 0 {
        log(
            &format!("  Пропущено (уже переведено): {skipped}"),
            LogTag::Info,
        );
    }
    if outcome.reused > 0 {
        log(
            &format!("  Из ванильного перевода: {}", outcome.reused),
            LogTag::Info,
        );
    }

    if !broken_in_file.is_empty() {
        log(
            &format!("  ⚠ Битых строк: {}", broken_in_file.len()),
            LogTag::Warning,
        );
        for entry in broken_in_file.iter().take(5) {
            log(
                &format!("    [{}]: {}", entry.key, entry.issues.join(", ")),
                LogTag::Warning,
            );
        }
    }

    finish(outcome, "written", "", FileResult::Written(total))
}
