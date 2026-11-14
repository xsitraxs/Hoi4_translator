//! Validation of translated values and the per-run validation report.
//!
//! A machine translation that lost a `$STATE$`, gained a raw quote or broke the
//! one-line value format looks like a success and only fails later, in the game.
//! Everything that reaches a `.yml` file goes through these checks first.

use std::sync::OnceLock;

use anyhow::{bail, Result};
use regex::Regex;

use crate::localisation::placeholder_re;

fn colour_marker_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"§(?:\[[^\]]*\]|[A-Za-z0-9!])").expect("colour regex"))
}

/// Python-style `repr` of a placeholder, so log lines stay comparable.
fn quoted(text: &str) -> String {
    format!("'{text}'")
}

/// Extract every game marker of a value, in order.
pub fn extract_placeholders(text: &str) -> Vec<String> {
    placeholder_re()
        .find_iter(text)
        .map(|matched| matched.as_str().to_string())
        .collect()
}

/// Positions of `§` that are neither a colour code nor a literal `§§`.
///
/// Valid starts are `§X` (a colour letter), `§!` (close), `§[...]` (a dynamic
/// colour from a game expression) and `§§` (a doubled section sign written by
/// the mod author, as in `15 U.S.C. §§ 1`, which reads as plain text in game).
fn invalid_sections(text: &str) -> Vec<usize> {
    let characters: Vec<char> = text.chars().collect();
    let mut positions = Vec::new();
    let mut index = 0usize;
    while index < characters.len() {
        if characters[index] != '§' {
            index += 1;
            continue;
        }
        let following = characters.get(index + 1).copied();
        if following == Some('[') {
            if let Some(offset) = characters[index + 2..].iter().position(|c| *c == ']') {
                index = index + 2 + offset + 1;
                continue;
            }
        }
        let is_colour_code = matches!(following, Some(c) if c == '!' || c.is_ascii_alphanumeric());
        if following == Some('§') || is_colour_code {
            index += 2;
            continue;
        }
        positions.push(index);
        index += 1;
    }
    positions
}

fn colour_format_issues(text: &str, label: &str) -> Vec<String> {
    let mut issues = Vec::new();
    if !invalid_sections(text).is_empty() {
        issues.push(format!("{label}: некорректный знак §"));
    }
    let mut active_colour = false;
    for marker in colour_marker_re().find_iter(text) {
        active_colour = marker.as_str() != "§!";
    }
    if active_colour {
        issues.push(format!("{label}: цветовой маркер не закрыт через §!"));
    }
    issues
}

/// Count occurrences, keeping the order in which each placeholder first appears.
fn counts(items: &[String]) -> Vec<(String, usize)> {
    let mut counters: Vec<(String, usize)> = Vec::new();
    for item in items {
        match counters
            .iter_mut()
            .find(|(key, _)| key.as_str() == item.as_str())
        {
            Some((_, count)) => *count += 1,
            None => counters.push((item.clone(), 1)),
        }
    }
    counters
}

/// Every problem with a translated value; an empty list means it is valid.
pub fn validate(original: &str, translated: &str) -> Vec<String> {
    let mut issues = Vec::new();
    issues.extend(colour_format_issues(original, "исходник"));
    issues.extend(colour_format_issues(translated, "перевод"));

    let original_colours: Vec<String> = colour_marker_re()
        .find_iter(original)
        .map(|matched| matched.as_str().to_string())
        .collect();
    let translated_colours: Vec<String> = colour_marker_re()
        .find_iter(translated)
        .map(|matched| matched.as_str().to_string())
        .collect();
    if original_colours != translated_colours {
        let mut sorted_original = original_colours.clone();
        sorted_original.sort();
        let mut sorted_translated = translated_colours.clone();
        sorted_translated.sort();
        if sorted_original == sorted_translated {
            issues.push("изменён порядок цветовых маркеров §".to_string());
        }
    }

    let original_counts = counts(&extract_placeholders(original));
    let translated_counts = counts(&extract_placeholders(translated));

    for (placeholder, count) in &original_counts {
        let translated_count = translated_counts
            .iter()
            .find(|(key, _)| key.as_str() == placeholder.as_str())
            .map(|(_, value)| *value)
            .unwrap_or(0);
        if translated_count == 0 {
            issues.push(format!("потерян: {}", quoted(placeholder)));
        } else if translated_count < *count {
            let missing = count - translated_count;
            issues.push(format!("недостаёт {missing}x {}", quoted(placeholder)));
        } else if translated_count > *count {
            let duplicated = translated_count - count;
            issues.push(format!("дублирован {duplicated}x {}", quoted(placeholder)));
        }
    }

    for (placeholder, _) in &translated_counts {
        if !original_counts
            .iter()
            .any(|(key, _)| key.as_str() == placeholder.as_str())
        {
            issues.push(format!("лишний: {}", quoted(placeholder)));
        }
    }

    issues
}

/// Issues of a single row; an empty list means the row can be written.
pub fn validate_line(original: &str, translated: &str) -> Vec<String> {
    let mut issues = Vec::new();
    if translated.contains('\n') || translated.contains('\r') {
        issues.push("значение должно занимать одну физическую строку; используйте \\n".to_string());
    }
    if has_unescaped_quote(translated) {
        issues.push("внутренние кавычки нужно экранировать как \\\"".to_string());
    }
    issues.extend(validate(original, translated));
    issues
}

/// Issues of every row; an empty list means the row is valid.
pub fn validate_pairs(originals: &[String], translations: &[String]) -> Result<Vec<Vec<String>>> {
    if originals.len() != translations.len() {
        bail!(
            "Количество строк изменилось: ожидалось {}, получено {}",
            originals.len(),
            translations.len()
        );
    }
    Ok(originals
        .iter()
        .zip(translations.iter())
        .map(|(original, translated)| validate_line(original, translated))
        .collect())
}

/// Problems the mod's own text already has, before any translation.
///
/// A lone `§R` used as a colour prefix, or a colour the author closed through a
/// `$VARIABLE$`, are not translation errors: the engine copies the markup
/// exactly and the game reads it as before.
pub fn source_issues(original: &str) -> Vec<String> {
    validate(original, original)
}

/// Split a row's issues into those inherited from the source and those added.
pub fn split_row_issues(original: &str, issues: &[String]) -> (Vec<String>, Vec<String>) {
    let known = source_issues(original);
    let mut inherited = Vec::new();
    let mut introduced = Vec::new();
    for issue in issues {
        let normalised = issue.replacen("перевод:", "исходник:", 1);
        if known.iter().any(|candidate| candidate.as_str() == normalised.as_str()) {
            inherited.push(issue.clone());
        } else {
            introduced.push(issue.clone());
        }
    }
    (inherited, introduced)
}

/// Preview rows that break the one-line value format or game tokens.
pub fn validate_preview_translations(originals: &[String], translations: &[String]) -> Vec<String> {
    let rows = match validate_pairs(originals, translations) {
        Ok(rows) => rows,
        Err(error) => return vec![error.to_string()],
    };
    let mut problems = Vec::new();
    for (row_number, issues) in rows.iter().enumerate() {
        if !issues.is_empty() {
            problems.push(format!(
                "Строка {}: {}",
                row_number + 1,
                issues.join(", ")
            ));
        }
    }
    problems
}

/// True when a value contains a raw quote instead of an escaped `\"`.
fn has_unescaped_quote(text: &str) -> bool {
    let mut backslashes = 0usize;
    for character in text.chars() {
        if character == '\\' {
            backslashes += 1;
            continue;
        }
        if character == '"' && backslashes % 2 == 0 {
            return true;
        }
        backslashes = 0;
    }
    false
}

// ---------------------------------------------------------------------------
// Session report
// ---------------------------------------------------------------------------

/// One row that could not be written as a translation.
#[derive(Debug, Clone)]
pub struct BrokenRow {
    pub file: String,
    pub key: String,
    pub issues: Vec<String>,
}

/// Collects per-session validation statistics, bounded so a huge mod cannot
/// exhaust memory while still counting every broken row.
#[derive(Debug, Clone, Default)]
pub struct ValidationReport {
    pub total: usize,
    pub valid: usize,
    pub fixed: usize,
    pub broken: Vec<BrokenRow>,
    pub untracked_broken: usize,
}

impl ValidationReport {
    pub const MAX_TRACKED_BROKEN: usize = 500;

    pub fn new() -> Self {
        Self::default()
    }

    pub fn record(&mut self, file: &str, key: &str, issues: &[String], auto_fixed: bool) {
        self.total += 1;
        if issues.is_empty() {
            self.valid += 1;
        } else if auto_fixed {
            self.fixed += 1;
        } else if self.broken.len() < Self::MAX_TRACKED_BROKEN {
            self.broken.push(BrokenRow {
                file: file.to_string(),
                key: key.to_string(),
                issues: issues.to_vec(),
            });
        } else {
            self.untracked_broken += 1;
        }
    }

    pub fn broken_count(&self) -> usize {
        self.broken.len() + self.untracked_broken
    }

    pub fn broken_for_file(&self, file: &str) -> Vec<BrokenRow> {
        self.broken
            .iter()
            .filter(|entry| entry.file == file)
            .cloned()
            .collect()
    }

    pub fn summary(&self) -> String {
        format!(
            "Валидация: {}/{} OK | авто-фикс: {} | битых: {}",
            self.valid,
            self.total,
            self.fixed,
            self.broken_count()
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_faithful_translation_is_valid() {
        assert!(validate(
            "Rearm $STATE$ with £army_experience|1",
            "Перевооружите $STATE$ с помощью £army_experience|1"
        )
        .is_empty());
    }

    #[test]
    fn a_lost_marker_is_reported() {
        let issues = validate("Rearm $STATE$ now", "Перевооружитесь сейчас");
        assert_eq!(issues, vec!["потерян: '$STATE$'".to_string()]);
    }

    #[test]
    fn duplicated_and_extra_markers_are_reported() {
        assert_eq!(
            validate("$A$ and $B$", "$A$ and $A$"),
            vec![
                "дублирован 1x '$A$'".to_string(),
                "потерян: '$B$'".to_string()
            ]
        );
        assert_eq!(
            validate("$A$", "$A$ and $C$"),
            vec!["лишний: '$C$'".to_string()]
        );
    }

    #[test]
    fn unclosed_colour_is_reported_for_each_side() {
        let issues = validate("§RNever closed", "§RNever closed");
        assert!(issues.iter().any(|issue| issue.contains("исходник: цветовой маркер не закрыт")));
        assert!(issues.iter().any(|issue| issue.contains("перевод: цветовой маркер не закрыт")));
    }

    #[test]
    fn dynamic_colours_and_double_section_signs_are_legal() {
        assert!(validate("§[GetColour]Sinn Féin§!", "§[GetColour]Шинн Фейн§!").is_empty());
        assert!(validate("15 U.S.C. §§ 1", "15 U.S.C. §§ 1").is_empty());
    }

    #[test]
    fn reordered_colour_markers_are_reported() {
        let issues = validate("§GGood§! §RBad§!", "§RBad§! §GGood§!");
        assert!(issues.iter().any(|issue| issue.contains("изменён порядок")));
    }

    #[test]
    fn physical_newlines_and_raw_quotes_are_rejected() {
        let issues = validate_line("Line", "Line one\nLine two");
        assert!(issues.iter().any(|issue| issue.contains("одну физическую строку")));
        let issues = validate_line("Say \"hi\"", "Скажи \"привет\"");
        assert!(issues.iter().any(|issue| issue.contains("экранировать")));
        // Typographic quotes are fine; `\"` is itself a protected marker, so
        // swapping raw quotes for escaped ones changes the marker set.
        assert!(validate_line("Say \"hi\"", "Скажи «привет»").is_empty());
    }

    #[test]
    fn source_problems_are_not_blamed_on_the_translation() {
        let original = "§RNever closed";
        let issues = validate(original, "§Rникогда не закрыт");
        let (inherited, introduced) = split_row_issues(original, &issues);
        assert!(!inherited.is_empty());
        assert!(introduced.is_empty(), "introduced: {introduced:?}");
    }

    #[test]
    fn rows_report_their_number_in_preview_problems() {
        let problems = validate_preview_translations(
            &["ok".to_string(), "Rearm $STATE$".to_string()],
            &["ok".to_string(), "Перевооружить".to_string()],
        );
        assert_eq!(problems.len(), 1);
        assert!(problems[0].starts_with("Строка 2:"));
    }

    #[test]
    fn report_counts_are_summarised() {
        let mut report = ValidationReport::new();
        report.record("a.yml", "A", &[], false);
        report.record("a.yml", "B", &["потерян: '$X$'".to_string()], true);
        report.record("a.yml", "C", &["лишний: '$Y$'".to_string()], false);
        assert_eq!(report.valid, 1);
        assert_eq!(report.fixed, 1);
        assert_eq!(report.broken_count(), 1);
        assert_eq!(report.broken_for_file("a.yml").len(), 1);
        assert_eq!(report.summary(), "Валидация: 1/3 OK | авто-фикс: 1 | битых: 1");
    }
}
