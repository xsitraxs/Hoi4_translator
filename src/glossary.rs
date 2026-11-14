//! Term glossary: force the same translation for chosen words.
//!
//! Machine engines translate the same term differently in different sentences —
//! "Division" becomes «дивизия», «подразделение», «отдел» depending on context.
//! A glossary pins the terms that matter (country names, unit types, ideologies)
//! so the mod reads consistently.
//!
//! The glossary does not ask an engine to respect the terms: every occurrence is
//! replaced by a placeholder before the request and written back afterwards, so
//! an engine cannot translate, reorder or decline a pinned term.

use std::collections::HashMap;
use std::fs;
use std::path::Path;

use anyhow::{anyhow, bail, Context, Result};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone)]
pub struct Term {
    /// The term as the author wrote it, kept for diagnostics.
    pub source: String,
    /// The term case-folded and split into characters, used for matching.
    folded: Vec<char>,
    /// The pinned translation, written back verbatim.
    pub target: String,
}

/// A source → target term map, matched case-insensitively on word boundaries.
#[derive(Debug, Default, Clone)]
pub struct Glossary {
    /// Longest term first, so "Soviet Union" wins over "Soviet".
    terms: Vec<Term>,
    by_folded: HashMap<String, String>,
    /// File name the glossary came from, used in logs and reports.
    pub origin: String,
}

fn fold_char(character: char) -> char {
    character.to_lowercase().next().unwrap_or(character)
}

fn fold_text(text: &str) -> String {
    text.chars().map(fold_char).collect()
}

fn is_word_char(character: char) -> bool {
    character.is_alphanumeric() || character == '_'
}

fn clean_pair(source: &str, target: &str) -> Option<(String, String)> {
    let source = source.trim();
    let target = target.trim();
    if source.is_empty() || target.is_empty() || source.starts_with('#') {
        return None;
    }
    Some((source.to_string(), target.to_string()))
}

impl Glossary {
    /// Build a glossary from `(source, target)` pairs; invalid pairs are dropped.
    pub fn from_pairs<I, S1, S2>(pairs: I, origin: impl Into<String>) -> Self
    where
        I: IntoIterator<Item = (S1, S2)>,
        S1: AsRef<str>,
        S2: AsRef<str>,
    {
        let mut folded: HashMap<String, (String, String)> = HashMap::new();
        let mut order: Vec<String> = Vec::new();
        for (source, target) in pairs {
            let Some((source, target)) = clean_pair(source.as_ref(), target.as_ref()) else {
                continue;
            };
            let key = fold_text(&source);
            if !folded.contains_key(&key) {
                order.push(key.clone());
            }
            folded.insert(key, (source, target));
        }

        let mut terms: Vec<Term> = order
            .iter()
            .filter_map(|key| {
                folded.get(key).map(|(source, target)| Term {
                    source: source.clone(),
                    folded: key.chars().collect(),
                    target: target.clone(),
                })
            })
            .collect();
        terms.sort_by(|left, right| right.folded.len().cmp(&left.folded.len()));

        let by_folded = folded
            .into_iter()
            .map(|(key, (_, target))| (key, target))
            .collect();

        Self {
            terms,
            by_folded,
            origin: origin.into(),
        }
    }

    /// Read a glossary from TSV/CSV/`key=value`/JSON.
    ///
    /// Accepted layouts (one pair per line, `#` starts a comment):
    /// `Term<TAB>Перевод`, `Term;Перевод`, `Term=Перевод` or a JSON object
    /// `{"Term": "Перевод"}`.
    pub fn load(path: &Path) -> Result<Self> {
        if path.as_os_str().is_empty() {
            bail!("Не указан файл глоссария");
        }
        if !path.is_file() {
            bail!("Файл глоссария не найден: {}", path.display());
        }
        let bytes = fs::read(path)
            .with_context(|| format!("Не удалось прочитать глоссарий: {}", path.display()))?;
        let bytes = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(&bytes);
        let content = std::str::from_utf8(bytes)
            .map_err(|_| anyhow!("Глоссарий должен быть в UTF-8 (пересохраните файл в UTF-8)"))?;

        let origin = path
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_default();
        let glossary = Self::from_pairs(Self::parse(content, path)?, origin);

        if glossary.is_empty() {
            let name = path
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default();
            bail!("В глоссарии нет ни одной пары «термин → перевод»: {name}");
        }
        Ok(glossary)
    }

    fn parse(content: &str, path: &Path) -> Result<Vec<(String, String)>> {
        let is_json = path
            .to_string_lossy()
            .to_lowercase()
            .ends_with(".json");
        if is_json {
            let value: serde_json::Value = serde_json::from_str(content)
                .map_err(|error| anyhow!("Некорректный JSON в глоссарии: {error}"))?;
            let serde_json::Value::Object(map) = value else {
                bail!("JSON-глоссарий должен быть объектом {{\"термин\": \"перевод\"}}");
            };
            return Ok(map
                .into_iter()
                .map(|(source, target)| (source, json_scalar(&target)))
                .collect());
        }

        let sample = content
            .lines()
            .find(|line| !line.trim().is_empty())
            .unwrap_or("");
        let delimiter = sniff_delimiter(sample);

        let mut pairs = Vec::new();
        for row in parse_csv(content, delimiter) {
            if row.is_empty() {
                continue;
            }
            if row.len() < 2 {
                let first = row[0].trim();
                // A bare line without a delimiter is a typo, not a pair.
                if !first.is_empty() && !first.starts_with('#') {
                    let head: String = first.chars().take(60).collect();
                    bail!("Строка глоссария без разделителя (нужен таб, «;» или «=»): {head}");
                }
                continue;
            }
            pairs.push((row[0].clone(), row[1].clone()));
        }
        Ok(pairs)
    }

    pub fn len(&self) -> usize {
        self.terms.len()
    }

    pub fn is_empty(&self) -> bool {
        self.terms.is_empty()
    }

    /// Pinned translation for an already matched term.
    pub fn lookup(&self, matched_text: &str) -> String {
        self.by_folded
            .get(&fold_text(matched_text))
            .cloned()
            .unwrap_or_else(|| matched_text.to_string())
    }

    /// Short stable id of the glossary contents, used in logs and reports.
    pub fn fingerprint(&self) -> String {
        let mut entries: Vec<String> = self
            .by_folded
            .iter()
            .map(|(term, value)| format!("{term}\t{value}"))
            .collect();
        entries.sort();
        let mut hasher = Sha256::new();
        hasher.update(entries.join("\n").as_bytes());
        let digest = format!("{:x}", hasher.finalize());
        digest.chars().take(12).collect()
    }

    /// Replace every whole-word term with a placeholder, collecting the text
    /// that has to be written back for each one.
    pub fn apply(&self, text: &str, tokens: &mut Vec<String>) -> String {
        if self.terms.is_empty() {
            return text.to_string();
        }
        let characters: Vec<char> = text.chars().collect();
        let mut output = String::with_capacity(text.len());
        let mut index = 0usize;
        while index < characters.len() {
            match self.match_at(&characters, index) {
                Some(term) => {
                    tokens.push(term.target.clone());
                    output.push_str(&format!("<<PH{}>>", tokens.len() - 1));
                    index += term.folded.len();
                }
                None => {
                    output.push(characters[index]);
                    index += 1;
                }
            }
        }
        output
    }

    fn match_at(&self, characters: &[char], index: usize) -> Option<&Term> {
        if index > 0 && is_word_char(characters[index - 1]) {
            return None;
        }
        for term in &self.terms {
            let length = term.folded.len();
            if index + length > characters.len() {
                continue;
            }
            if (0..length).any(|offset| fold_char(characters[index + offset]) != term.folded[offset])
            {
                continue;
            }
            if characters
                .get(index + length)
                .map(|character| is_word_char(*character))
                .unwrap_or(false)
            {
                continue;
            }
            return Some(term);
        }
        None
    }
}

fn json_scalar(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Pick the delimiter a term file uses; tab wins when several appear.
fn sniff_delimiter(sample: &str) -> char {
    let mut best = '\t';
    let mut best_count = sample.matches('\t').count();
    for candidate in [';', '='] {
        let count = sample.matches(candidate).count();
        if count > best_count {
            best = candidate;
            best_count = count;
        }
    }
    best
}

/// Minimal CSV reader: quoted fields, doubled quotes, CRLF and LF row breaks.
fn parse_csv(content: &str, delimiter: char) -> Vec<Vec<String>> {
    let mut rows: Vec<Vec<String>> = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut in_quotes = false;
    let mut characters = content.chars().peekable();

    while let Some(character) = characters.next() {
        if in_quotes {
            if character == '"' {
                if characters.peek() == Some(&'"') {
                    field.push('"');
                    characters.next();
                } else {
                    in_quotes = false;
                }
            } else {
                field.push(character);
            }
            continue;
        }
        match character {
            '"' => in_quotes = true,
            '\r' => {
                if characters.peek() == Some(&'\n') {
                    characters.next();
                }
                row.push(std::mem::take(&mut field));
                rows.push(std::mem::take(&mut row));
            }
            '\n' => {
                row.push(std::mem::take(&mut field));
                rows.push(std::mem::take(&mut row));
            }
            _ if character == delimiter => row.push(std::mem::take(&mut field)),
            _ => field.push(character),
        }
    }
    if !field.is_empty() || !row.is_empty() {
        row.push(field);
        rows.push(row);
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;

    fn glossary(pairs: &[(&str, &str)]) -> Glossary {
        Glossary::from_pairs(pairs.to_vec(), "test")
    }

    #[test]
    fn terms_are_matched_case_insensitively_on_word_boundaries() {
        let glossary = glossary(&[("Division", "дивизия")]);
        let mut tokens = Vec::new();
        let masked = glossary.apply("A Division, divisions and Division.", &mut tokens);
        // Each occurrence gets its own token so a word can be restored in a
        // different grammatical form later; `divisions` is untouched because
        // the term ends mid-word.
        assert_eq!(masked, "A <<PH0>>, divisions and <<PH1>>.");
        assert_eq!(tokens, vec!["дивизия".to_string(), "дивизия".to_string()]);
    }

    #[test]
    fn the_longest_term_wins() {
        let glossary = glossary(&[("Soviet", "советский"), ("Soviet Union", "СССР")]);
        let mut tokens = Vec::new();
        let masked = glossary.apply("The Soviet Union and Soviet ships", &mut tokens);
        assert_eq!(masked, "The <<PH0>> and <<PH1>> ships");
        assert_eq!(tokens, vec!["СССР".to_string(), "советский".to_string()]);
    }

    #[test]
    fn cyrillic_terms_are_matched_too() {
        let glossary = glossary(&[("дивизия", "division")]);
        let mut tokens = Vec::new();
        let masked = glossary.apply("Дивизия на марше", &mut tokens);
        assert_eq!(masked, "<<PH0>> на марше");
        assert_eq!(tokens, vec!["division".to_string()]);
    }

    #[test]
    fn tsv_glossaries_are_parsed_with_comments_and_quotes() {
        let pairs = Glossary::parse("# comment\nDivision\tдивизия\n\"Tank; heavy\"\tтяжёлый танк\n", Path::new("g.tsv"))
            .expect("parsed");
        assert_eq!(
            pairs,
            vec![
                ("Division".to_string(), "дивизия".to_string()),
                ("Tank; heavy".to_string(), "тяжёлый танк".to_string()),
            ]
        );
    }

    #[test]
    fn semicolon_glossaries_use_the_semicolon_delimiter() {
        let pairs = Glossary::parse("Army;армия\nNavy;флот\n", Path::new("g.csv")).expect("parsed");
        assert_eq!(pairs[1], ("Navy".to_string(), "флот".to_string()));
    }

    #[test]
    fn json_glossaries_are_objects() {
        let pairs = Glossary::parse(r#"{"Army": "армия"}"#, Path::new("g.json")).expect("parsed");
        assert_eq!(pairs, vec![("Army".to_string(), "армия".to_string())]);
        assert!(Glossary::parse("[]", Path::new("g.json")).is_err());
    }

    #[test]
    fn a_line_without_a_delimiter_is_an_error() {
        assert!(Glossary::parse("Army\nNavy;флот\n", Path::new("g.csv")).is_err());
    }

    #[test]
    fn the_fingerprint_is_stable_and_short() {
        let first = glossary(&[("Army", "армия"), ("Navy", "флот")]).fingerprint();
        let second = glossary(&[("Navy", "флот"), ("Army", "армия")]).fingerprint();
        assert_eq!(first, second);
        assert_eq!(first.len(), 12);
    }
}
