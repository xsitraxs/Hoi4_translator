//! Inspect localisation files without translating anything.
//!
//! Not everyone has the game installed, and nobody wants to spend DeepL quota to
//! find out that one key is malformed. This module reads a mod's localisation the
//! same way the translator does and reports what the translator would trip over,
//! answering "will this mod translate cleanly?" without a single network request.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::config::LogTag;
use crate::localisation::{
    load_existing_translations, normalize_key, read_localisation_lines, split_value_line,
};
use crate::validation::validate;

/// Everything worth knowing about one localisation file.
#[derive(Debug, Clone, Default)]
pub struct FileInspection {
    pub path: PathBuf,
    pub encoding: String,
    pub keys: usize,
    pub values: usize,
    pub comments: usize,
    pub malformed: Vec<usize>,
    pub duplicates: Vec<(String, Vec<usize>)>,
    pub broken_markers: Vec<(usize, String, Vec<String>)>,
    pub translated: usize,
    pub error: String,
}

impl FileInspection {
    /// Problems that make the translator skip the file: a malformed line or a
    /// duplicate key means the file cannot be written. An unbalanced colour
    /// marker in the mod's own English does not.
    pub fn has_blocking_problems(&self) -> bool {
        !self.error.is_empty() || !self.malformed.is_empty() || !self.duplicates.is_empty()
    }

    /// Worth reading, but the file still translates.
    pub fn has_notes(&self) -> bool {
        !self.broken_markers.is_empty()
    }

    pub fn has_problems(&self) -> bool {
        self.has_blocking_problems() || self.has_notes()
    }
}

fn quiet_log() -> impl FnMut(&str, LogTag) {
    |message: &str, tag: LogTag| match tag {
        LogTag::Error => log::error!("{message}"),
        LogTag::Warning => log::warn!("{message}"),
        LogTag::Info => log::info!("{message}"),
    }
}

/// Parse `path` and collect its problems; never panics on bad content.
pub fn inspect_file(path: &Path, target_path: Option<&Path>) -> FileInspection {
    let mut inspection = FileInspection {
        path: path.to_path_buf(),
        encoding: "utf-8".to_string(),
        ..Default::default()
    };

    let mut log = quiet_log();
    let (lines, encoding) = match read_localisation_lines(path, &mut log) {
        Ok(result) => result,
        Err(error) => {
            inspection.error = error.to_string();
            return inspection;
        }
    };
    inspection.encoding = encoding.to_string();

    let mut seen: Vec<(String, Vec<usize>)> = Vec::new();
    for (offset, line) in lines.iter().enumerate() {
        let number = offset + 1;
        let content = line.trim_end_matches(['\r', '\n']);
        let stripped = content.trim();
        if stripped.is_empty() {
            continue;
        }
        if stripped.starts_with('#') {
            inspection.comments += 1;
            continue;
        }
        // The language header is not a key/value pair.
        if stripped.to_lowercase().starts_with("l_") && stripped.ends_with(':') {
            continue;
        }

        let Some(parts) = split_value_line(content) else {
            if stripped.contains(':') {
                inspection.malformed.push(number);
            }
            continue;
        };

        let key = normalize_key(&parts.prefix);
        inspection.keys += 1;
        inspection.values += 1;
        match seen.iter_mut().find(|(candidate, _)| *candidate == key) {
            Some((_, rows)) => rows.push(number),
            None => seen.push((key.clone(), vec![number])),
        }

        // An unclosed § in the source is a real bug that survives translation.
        let issues = validate(&parts.value, &parts.value);
        if !issues.is_empty() {
            inspection.broken_markers.push((number, key, issues));
        }
    }

    inspection.duplicates = seen
        .into_iter()
        .filter(|(_, rows)| rows.len() > 1)
        .collect();

    if let Some(target_path) = target_path {
        if target_path.is_file() {
            let mut log = quiet_log();
            inspection.translated = load_existing_translations(target_path, &mut log).len();
        }
    }
    inspection
}

/// Inspect every file, keeping the given order.
pub fn inspect_files(
    paths: &[PathBuf],
    target_paths: Option<&HashMap<PathBuf, PathBuf>>,
) -> Vec<FileInspection> {
    paths
        .iter()
        .map(|path| {
            let target = target_paths.and_then(|map| map.get(path));
            inspect_file(path, target.map(|path| path.as_path()))
        })
        .collect()
}

fn relative_display(path: &Path, src_root: &Path) -> String {
    path.strip_prefix(src_root)
        .unwrap_or(path)
        .display()
        .to_string()
}

/// Human-readable summary: one line per file, then the problems.
pub fn format_report(inspections: &[FileInspection], src_root: &Path) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut total_keys = 0usize;
    let mut total_malformed = 0usize;
    let mut bad_files = 0usize;

    for inspection in inspections {
        let relative = relative_display(&inspection.path, src_root);
        total_keys += inspection.keys;
        total_malformed += inspection.malformed.len();
        if !inspection.error.is_empty() {
            bad_files += 1;
            lines.push(format!("! {relative}: {}", inspection.error));
            continue;
        }
        if inspection.has_blocking_problems() {
            bad_files += 1;
        }

        let mut parts = vec![format!("{} ключей", inspection.keys)];
        if inspection.translated > 0 {
            parts.push(format!("{} уже переведено", inspection.translated));
        }
        if !inspection.malformed.is_empty() {
            let rows: Vec<String> = inspection
                .malformed
                .iter()
                .take(5)
                .map(|row| row.to_string())
                .collect();
            let more = if inspection.malformed.len() > 5 { "…" } else { "" };
            parts.push(format!(
                "НЕ ПАРСИТСЯ строк: {} (строки {}{more})",
                inspection.malformed.len(),
                rows.join(", ")
            ));
        }
        if !inspection.duplicates.is_empty() {
            let keys: Vec<&str> = inspection
                .duplicates
                .iter()
                .take(3)
                .map(|(key, _)| key.as_str())
                .collect();
            let more = if inspection.duplicates.len() > 3 {
                "…"
            } else {
                ""
            };
            parts.push(format!(
                "дубликаты ключей: {} ({}{more})",
                inspection.duplicates.len(),
                keys.join(", ")
            ));
        }
        if !inspection.broken_markers.is_empty() {
            parts.push(format!(
                "замечания по § в исходнике: {}",
                inspection.broken_markers.len()
            ));
        }

        let prefix = if inspection.has_blocking_problems() {
            "! "
        } else if inspection.has_notes() {
            "~ "
        } else {
            "  "
        };
        lines.push(format!("{prefix}{relative}: {}", parts.join(", ")));

        for (number, key, issues) in inspection.broken_markers.iter().take(5) {
            lines.push(format!(
                "      строка {number} [{key}]: {}",
                issues.join(", ")
            ));
        }
    }

    lines.push(String::new());
    let mut summary = format!("Итого: файлов {}, ключей {total_keys}", inspections.len());
    if bad_files > 0 {
        summary.push_str(&format!(", с проблемами {bad_files}"));
    }
    lines.push(summary);
    if total_malformed > 0 {
        lines.push(
            "Файлы со строками, которые не парсятся, переводчик пропустит целиком — исправьте \
             их до запуска."
                .to_string(),
        );
    }
    let notes: usize = inspections
        .iter()
        .map(|inspection| inspection.broken_markers.len())
        .sum();
    if notes > 0 && total_malformed == 0 {
        lines.push(
            "Замечания по § — это разметка самого мода (например, знак, закрытый через \
             $ПЕРЕМЕННУЮ$): такие строки записываются как есть, файл переводится."
                .to_string(),
        );
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(path: &Path, content: &str) {
        std::fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
        std::fs::write(path, content).expect("write");
    }

    #[test]
    fn a_clean_file_has_no_problems() {
        let dir = std::env::temp_dir().join(format!("hoi4-inspect-ok-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let file = dir.join("events_l_english.yml");
        write(&file, "l_english:\n # comment\n A:0 \"One\"\n B:0 \"Two\"\n");

        let inspection = inspect_file(&file, None);
        assert_eq!(inspection.keys, 2);
        assert_eq!(inspection.comments, 1);
        assert!(!inspection.has_blocking_problems());
        assert!(!inspection.has_problems());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn malformed_lines_and_duplicates_block_a_file() {
        let dir = std::env::temp_dir().join(format!("hoi4-inspect-bad-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let file = dir.join("broken_l_english.yml");
        write(
            &file,
            "l_english:\n A:0 \"One\"\n A:1 \"Again\"\n B:0 \"never closed\n",
        );

        let inspection = inspect_file(&file, None);
        assert_eq!(inspection.malformed, vec![4]);
        assert_eq!(inspection.duplicates.len(), 1);
        assert!(inspection.has_blocking_problems());

        let report = format_report(&[inspection], &dir);
        assert!(report.iter().any(|line| line.starts_with('!')));
        assert!(report.iter().any(|line| line.contains("НЕ ПАРСИТСЯ")));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unclosed_colour_is_only_a_note() {
        let dir = std::env::temp_dir().join(format!("hoi4-inspect-note-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let file = dir.join("notes_l_english.yml");
        write(&file, "l_english:\n A:0 \"§Rnever closed\"\n");

        let inspection = inspect_file(&file, None);
        assert!(!inspection.has_blocking_problems());
        assert!(inspection.has_notes());
        let report = format_report(&[inspection], &dir);
        assert!(report.iter().any(|line| line.starts_with('~')));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_file_reports_an_error_instead_of_panicking() {
        let inspection = inspect_file(Path::new("/definitely/not/here_l_english.yml"), None);
        assert!(!inspection.error.is_empty());
        assert!(inspection.has_blocking_problems());
    }
}
