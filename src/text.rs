//! Text helpers: marker protection, conservative typography and chunking.

use std::sync::OnceLock;

use regex::{Captures, Regex};

use crate::glossary::Glossary;
use crate::localisation::placeholder_re;

/// Providers reject very long queries, so values are split at this length.
pub const MAX_QUERY_CHARS: usize = 400;

fn cell_re(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("static regex must compile"))
}

fn double_space_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    cell_re(&CELL, r"  +")
}

fn space_before_punct_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    cell_re(&CELL, r" +([,;:!?])")
}

fn french_space_before_comma_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    cell_re(&CELL, r" +,")
}

fn french_space_before_punct_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    cell_re(&CELL, r" +([;:!?])")
}

fn quoted_phrase_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    cell_re(&CELL, r#""([^"\\]+)""#)
}

fn space_around_newline_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    // Only horizontal spaces: `\s` would also consume physical CR/LF and hide a
    // malformed translation.
    cell_re(&CELL, r"[ \t]*\\n[ \t]*")
}

fn leftover_placeholder_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    cell_re(&CELL, r"<<PH\d+>>")
}

fn adjacent_placeholder_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    cell_re(&CELL, r"(<<PH\d+>>)(<<PH\d+>>)")
}

fn placeholder_token_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    // Engines like to insert spaces or change the case of protected markers.
    cell_re(&CELL, r"(?i)<<\s*ph\s*\d+\s*>>")
}

// ---------------------------------------------------------------------------
// Post-processing
// ---------------------------------------------------------------------------

/// Apply conservative, target-language-aware typography fixes.
pub struct PostProcessor;

fn quote_style(language: &str) -> (&'static str, &'static str, &'static str) {
    match language {
        "fr" => ("«", "»", "\u{202F}"),
        "de" => ("„", "“", ""),
        "es" => ("«", "»", ""),
        "ja" | "zh-cn" | "zh" => ("「", "」", ""),
        "ko" | "en" | "pt" => ("“", "”", ""),
        "pl" => ("„", "”", ""),
        "ru" => ("«", "»", ""),
        _ => ("“", "”", ""),
    }
}

impl PostProcessor {
    /// Normalize basic punctuation without editing HOI4 tokens or casing.
    pub fn process(text: &str, target_lang: &str) -> String {
        if text.is_empty() {
            return String::new();
        }

        // HOI4 newline escapes are literal two-character sequences in a value.
        let text = space_around_newline_re().replace_all(text, "\\n").to_string();

        // Choose a marker prefix that cannot collide with the text itself.
        let mut marker_prefix = "\u{E000}HOI4PP_".to_string();
        while text.contains(&marker_prefix) {
            marker_prefix.push('_');
        }

        let mut tokens: Vec<String> = Vec::new();
        let protected = placeholder_re()
            .replace_all(&text, |captures: &Captures<'_>| {
                tokens.push(
                    captures
                        .get(0)
                        .map(|matched| matched.as_str().to_string())
                        .unwrap_or_default(),
                );
                format!("{marker_prefix}{}\u{E001}", tokens.len() - 1)
            })
            .to_string();

        let language = target_lang.replace('_', "-").to_lowercase();
        let (quote_open, quote_close, quote_space) = quote_style(&language);
        let protected = quoted_phrase_re()
            .replace_all(&protected, |captures: &Captures<'_>| {
                let inner = captures.get(1).map(|m| m.as_str()).unwrap_or("");
                format!("{quote_open}{quote_space}{inner}{quote_space}{quote_close}")
            })
            .to_string();
        let mut protected = double_space_re().replace_all(&protected, " ").to_string();

        if language == "fr" {
            protected = french_space_before_comma_re()
                .replace_all(&protected, ",")
                .to_string();
            protected = french_space_before_punct_re()
                .replace_all(&protected, |captures: &Captures<'_>| {
                    let punctuation = captures.get(1).map(|m| m.as_str()).unwrap_or("");
                    format!("\u{202F}{punctuation}")
                })
                .to_string();
        } else {
            protected = space_before_punct_re()
                .replace_all(&protected, "$1")
                .to_string();
        }
        let protected = protected.trim_matches(' ').to_string();

        let marker_pattern = format!("{}([0-9]+){}", regex::escape(&marker_prefix), '\u{E001}');
        let Ok(marker_re) = Regex::new(&marker_pattern) else {
            return leftover_placeholder_re().replace_all(&protected, "").to_string();
        };
        let restored = marker_re
            .replace_all(&protected, |captures: &Captures<'_>| {
                let index = captures
                    .get(1)
                    .and_then(|matched| matched.as_str().parse::<usize>().ok());
                match index {
                    Some(index) if index < tokens.len() => tokens[index].clone(),
                    _ => captures
                        .get(0)
                        .map(|matched| matched.as_str().to_string())
                        .unwrap_or_default(),
                }
            })
            .to_string();

        leftover_placeholder_re()
            .replace_all(&restored, "")
            .to_string()
    }
}

// ---------------------------------------------------------------------------
// Marker protection
// ---------------------------------------------------------------------------

/// Mask game markers (and glossary terms) before sending text to an engine.
///
/// Glossary terms are masked *after* the markers and restored with their pinned
/// translation, so an engine can neither translate nor reorder them. The
/// returned token list holds the text to write back for every mask, which is why
/// the glossary needs no separate restore step.
pub fn protect_placeholders(text: &str, glossary: Option<&Glossary>) -> (String, Vec<String>) {
    let mut tokens: Vec<String> = Vec::new();
    let mut protected = placeholder_re()
        .replace_all(text, |captures: &Captures<'_>| {
            tokens.push(
                captures
                    .get(0)
                    .map(|matched| matched.as_str().to_string())
                    .unwrap_or_default(),
            );
            format!("<<PH{}>>", tokens.len() - 1)
        })
        .to_string();

    if let Some(glossary) = glossary {
        protected = glossary.apply(&protected, &mut tokens);
    }

    // An invisible separator between adjacent markers stops an engine from
    // gluing them into one word and then translating the result.
    protected = adjacent_placeholder_re()
        .replace_all(&protected, |captures: &Captures<'_>| {
            format!("{}\u{200B}{}", &captures[1], &captures[2])
        })
        .to_string();

    (protected, tokens)
}

/// Put the protected text back, tolerating engines that reformat the markers.
pub fn restore_placeholders(text: &str, tokens: &[String]) -> String {
    let text = text.replace('\u{200B}', "");
    placeholder_token_re()
        .replace_all(&text, |captures: &Captures<'_>| {
            let raw = captures.get(0).map(|m| m.as_str()).unwrap_or("");
            let digits: String = raw.chars().filter(char::is_ascii_digit).collect();
            match digits.parse::<usize>() {
                Ok(index) if index < tokens.len() => tokens[index].clone(),
                _ => raw.to_string(),
            }
        })
        .to_string()
}

// ---------------------------------------------------------------------------
// Chunking
// ---------------------------------------------------------------------------

fn is_marker_start(characters: &[char], index: usize) -> Option<usize> {
    let len = characters.len();
    if index + 4 > len {
        return None;
    }
    if characters[index] != '<' || characters[index + 1] != '<' {
        return None;
    }
    if !characters[index + 2].eq_ignore_ascii_case(&'p') || !characters[index + 3].eq_ignore_ascii_case(&'h')
    {
        return None;
    }
    let mut cursor = index + 4;
    let digits_start = cursor;
    while cursor < len && characters[cursor].is_ascii_digit() {
        cursor += 1;
    }
    if cursor == digits_start || cursor + 1 >= len {
        return None;
    }
    if characters[cursor] == '>' && characters[cursor + 1] == '>' {
        Some(cursor + 2)
    } else {
        None
    }
}

fn marker_spans(characters: &[char]) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut index = 0usize;
    while index < characters.len() {
        match is_marker_start(characters, index) {
            Some(end) => {
                spans.push((index, end));
                index = end;
            }
            None => index += 1,
        }
    }
    spans
}

fn find_chunk_boundary(characters: &[char], start: usize, hard_end: usize) -> usize {
    let spans = marker_spans(characters);
    let mut safe_end = hard_end;
    for &(marker_start, marker_end) in &spans {
        if marker_start >= hard_end {
            break;
        }
        if marker_end > hard_end {
            safe_end = if marker_start > start {
                marker_start
            } else {
                marker_end
            };
            break;
        }
    }

    let sentence_terminators = ".!?。！？";
    let mut sentence_boundary: Option<usize> = None;
    let mut word_boundary: Option<usize> = None;
    let mut index = safe_end.saturating_sub(1);
    while index > start {
        if characters[index].is_whitespace() {
            if word_boundary.is_none() {
                word_boundary = Some(index + 1);
            }
            let mut previous = index as isize - 1;
            while previous >= start as isize && characters[previous as usize].is_whitespace() {
                previous -= 1;
            }
            if previous >= start as isize
                && sentence_terminators.contains(characters[previous as usize])
            {
                sentence_boundary = Some(index + 1);
                break;
            }
        }
        index -= 1;
    }

    let mut end = sentence_boundary
        .or(word_boundary)
        .unwrap_or(safe_end);

    // The chosen boundary must also stay outside every protection marker.
    for &(marker_start, marker_end) in &spans {
        if marker_start >= end {
            break;
        }
        if marker_end > end {
            end = if marker_start > start {
                marker_start
            } else {
                marker_end
            };
            break;
        }
    }

    if end <= start {
        end = if safe_end > start { safe_end } else { hard_end };
    }
    end
}

/// Split a long value into translatable chunks, never dropping characters.
pub fn split_long_text(text: &str, limit: usize) -> Vec<String> {
    let characters: Vec<char> = text.chars().collect();
    if characters.len() <= limit {
        return vec![text.to_string()];
    }

    let mut chunks: Vec<String> = Vec::new();
    let mut start = 0usize;
    while start < characters.len() {
        let hard_end = (start + limit).min(characters.len());
        if hard_end == characters.len() {
            chunks.push(characters[start..].iter().collect());
            break;
        }
        let end = find_chunk_boundary(&characters, start, hard_end);
        chunks.push(characters[start..end].iter().collect());
        start = end;
    }
    chunks
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markers_are_protected_and_restored() {
        let original = "Rearm $STATE$ with £army_experience|1 and [GetPlayerTag] backing.";
        let (protected, tokens) = protect_placeholders(original, None);
        assert_eq!(protected, "Rearm <<PH0>> with <<PH1>> and <<PH2>> backing.");
        assert_eq!(
            tokens,
            vec![
                "$STATE$".to_string(),
                "£army_experience|1".to_string(),
                "[GetPlayerTag]".to_string()
            ]
        );
        assert_eq!(
            restore_placeholders(&protected, &tokens),
            "Rearm $STATE$ with £army_experience|1 and [GetPlayerTag] backing."
        );
    }

    #[test]
    fn adjacent_markers_get_an_invisible_separator() {
        let (protected, tokens) = protect_placeholders("$A$$B$", None);
        assert_eq!(protected, "<<PH0>>\u{200B}<<PH1>>");
        assert_eq!(restore_placeholders(&protected, &tokens), "$A$$B$");
    }

    #[test]
    fn engines_that_reformat_markers_are_tolerated() {
        let tokens = vec!["$STATE$".to_string()];
        assert_eq!(restore_placeholders("<< ph 0 >>", &tokens), "$STATE$");
        assert_eq!(restore_placeholders("<<PH0>>", &tokens), "$STATE$");
        assert_eq!(restore_placeholders("<<PH9>>", &tokens), "<<PH9>>");
    }

    #[test]
    fn glossary_terms_are_replaced_with_their_pinned_translation() {
        let glossary = Glossary::from_pairs(vec![("Division", "дивизия")], "test");
        let (protected, tokens) = protect_placeholders("One Division, $STATE$", Some(&glossary));
        assert_eq!(protected, "One <<PH1>>, <<PH0>>");
        assert_eq!(restore_placeholders(&protected, &tokens), "One дивизия, $STATE$");
    }

    #[test]
    fn post_processing_fixes_spacing_but_keeps_tokens() {
        let processed = PostProcessor::process("Hello  world , $STATE$ !", "en");
        assert_eq!(processed, "Hello world, $STATE$!");
    }

    #[test]
    fn post_processing_uses_target_quotes() {
        assert_eq!(PostProcessor::process("call it \"war\"", "ru"), "call it «war»");
        assert_eq!(PostProcessor::process("call it \"war\"", "de"), "call it „war“");
        assert_eq!(PostProcessor::process("call it \"war\"", "en"), "call it “war”");
    }

    #[test]
    fn post_processing_keeps_hori4_newline_escapes_tight() {
        assert_eq!(
            PostProcessor::process("First  \\n  second", "en"),
            "First\\nsecond"
        );
    }

    #[test]
    fn leftover_markers_are_dropped() {
        assert_eq!(PostProcessor::process("Hello <<PH0>> world", "en"), "Hello  world");
    }

    #[test]
    fn long_values_are_split_without_losing_characters() {
        let text = format!("{}. {}", "word ".repeat(100), "Second sentence.");
        let chunks = split_long_text(&text, MAX_QUERY_CHARS);
        assert!(chunks.len() > 1);
        assert!(chunks.iter().all(|chunk| chunk.chars().count() <= MAX_QUERY_CHARS));
        assert_eq!(chunks.concat(), text);
    }

    #[test]
    fn splitting_never_cuts_a_marker() {
        let head = "x".repeat(395);
        let text = format!("{head}<<PH0>> tail of the sentence");
        let chunks = split_long_text(&text, MAX_QUERY_CHARS);
        assert_eq!(chunks.concat(), text);
        assert!(chunks
            .iter()
            .all(|chunk| !chunk.contains("<<PH") || chunk.contains("<<PH0>>")));
    }

    #[test]
    fn short_values_stay_in_one_chunk() {
        assert_eq!(
            split_long_text("short", MAX_QUERY_CHARS),
            vec!["short".to_string()]
        );
    }
}
