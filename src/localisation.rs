//! The HOI4 localisation format: parsing, language databases and output paths.
//!
//! A localisation file is a `l_<database>:` header followed by lines of
//! `KEY:0 "value"`. Real files break that shape constantly — unescaped quotes
//! inside values, trailing comments, BOMs, Windows-1251 encodings — and every
//! helper in this module is written to survive them instead of losing a whole
//! file to one odd line.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::OnceLock;

use anyhow::{bail, Context, Result};
use regex::Regex;

use crate::config::{path_key, LogTag};

// ---------------------------------------------------------------------------
// Regexes (compiled once)
// ---------------------------------------------------------------------------

fn regex_cell(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("static regex must compile"))
}

/// Every marker the game interprets: variables, script expressions, colour
/// codes, icon references, escapes and dynamic loc commands.
///
/// Two section-sign forms appear in real files and are *not* broken colour
/// codes: `§[expression]` (a dynamic colour) and `§§` (a literal section sign,
/// as in a US legal citation). Both are matched as one unit here.
pub fn placeholder_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex_cell(
        &CELL,
        r"(\$\$|\$[^$]+\$|\[[^\]]*\]|§\[[^\]]*\]|§§|§[A-Za-z0-9!]|\\+.|\\{2,}|£\w+(?:\|\d+)?|@\w+\[!?\]?)",
    )
}

fn value_prefix_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex_cell(&CELL, r"^(\s*[A-Za-z0-9_.-]+:\d*\s*)")
}

fn value_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex_cell(
        &CELL,
        r#"^(\s*[A-Za-z0-9_.-]+:\d*\s*)"((?:[^"\\]|\\.)*)"(.*)$"#,
    )
}

fn value_suffix_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex_cell(&CELL, r"^[ \t]*(?:#.*)?$")
}

fn language_header_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex_cell(&CELL, r"(?i)^[ \t]*l_([A-Za-z0-9_]+):[ \t]*(?:#.*)?$")
}

fn key_version_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex_cell(&CELL, r":\d*$")
}

fn language_suffix_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex_cell(&CELL, r"(?i)_l_[a-z0-9_-]+$")
}

// ---------------------------------------------------------------------------
// Language tables
// ---------------------------------------------------------------------------

/// Short translator code -> the game's built-in localisation database name.
pub const HOI4_LANGUAGE_NAMES: [(&str, &str); 10] = [
    ("en", "english"),
    ("fr", "french"),
    ("de", "german"),
    ("es", "spanish"),
    ("pt", "braz_por"),
    ("pl", "polish"),
    ("ru", "russian"),
    ("ja", "japanese"),
    ("zh-CN", "simp_chinese"),
    ("ko", "korean"),
];

/// Display order, kept separate from the translator's language codes so the
/// output target can only be one of the game's built-in databases.
pub const HOI4_TARGET_LANGS: [(&str, &str); 10] = [
    ("en", "Английский"),
    ("fr", "Французский"),
    ("de", "Немецкий"),
    ("es", "Испанский"),
    ("pt", "Бразильский португальский"),
    ("pl", "Польский"),
    ("ru", "Русский"),
    ("ja", "Японский"),
    ("zh-CN", "Упрощённый китайский"),
    ("ko", "Корейский"),
];

/// Languages that may be a *source*: more than the ten the game ships.
const SOURCE_LANGUAGE_EXTRAS: [(&str, &str); 6] = [
    ("uk", "ukrainian"),
    ("it", "italian"),
    ("tr", "turkish"),
    ("cs", "czech"),
    ("hu", "hungarian"),
    ("ro", "romanian"),
];

fn lower(value: &str) -> String {
    value.to_lowercase()
}

/// Internal database name for a target language, or an error listing what is
/// supported. Only the game's own databases may be written: `l_italian:` would
/// never be loaded by HOI4.
pub fn get_hoi4_language_name(target_lang: &str) -> Result<&'static str> {
    let wanted = lower(target_lang);
    for (code, name) in HOI4_LANGUAGE_NAMES {
        if lower(code) == wanted {
            return Ok(name);
        }
    }
    let supported: Vec<&str> = HOI4_LANGUAGE_NAMES.iter().map(|(code, _)| *code).collect();
    bail!(
        "Неподдерживаемый целевой язык HOI4: {target_lang:?}. Доступны: {}.",
        supported.join(", ")
    )
}

/// Case-insensitive lookup of a source language database name.
pub fn source_language_name(code: &str) -> Option<&'static str> {
    let wanted = lower(code);
    for (candidate, name) in HOI4_LANGUAGE_NAMES {
        if lower(candidate) == wanted {
            return Some(name);
        }
    }
    for (candidate, name) in SOURCE_LANGUAGE_EXTRAS {
        if lower(candidate) == wanted {
            return Some(name);
        }
    }
    None
}

/// Directory names that are recognised as language folders in a mod layout.
pub fn hoi4_language_directories() -> &'static HashSet<String> {
    static DIRS: OnceLock<HashSet<String>> = OnceLock::new();
    DIRS.get_or_init(|| {
        HOI4_LANGUAGE_NAMES
            .iter()
            .map(|(_, name)| lower(name))
            .collect()
    })
}

pub fn is_supported_target(code: &str) -> bool {
    let wanted = lower(code);
    HOI4_LANGUAGE_NAMES
        .iter()
        .any(|(candidate, _)| lower(candidate) == wanted)
}

// ---------------------------------------------------------------------------
// Reading files
// ---------------------------------------------------------------------------

/// HOI4 requires UTF-8 with BOM, but older mods ship single-byte code pages and
/// Windows editors happily save UTF-16. Every label `decode_source` can return is
/// listed here, so the log line always names something the user can act on.
pub const SOURCE_ENCODINGS: [&str; 7] = [
    "utf-8-sig",
    "utf-8",
    "utf-16le",
    "utf-16be",
    "cp1251",
    "cp1252",
    "latin-1",
];

/// Decode bytes into text, reporting which encoding won.
///
/// A byte-order mark is a declaration and wins outright; UTF-16 files written by
/// Notepad are common enough that decoding them as a single-byte code page would
/// silently turn every character into a plausible-looking wrong one. Without a
/// BOM the order is the one the Python build used — UTF-8, then cp1251, then
/// cp1252, then latin-1 — but a candidate only wins when it decodes the whole
/// file without an undefined byte, which is what makes cp1252 reachable at all.
fn decode_source(bytes: &[u8]) -> (String, &'static str) {
    if let Some(rest) = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        if let Ok(text) = std::str::from_utf8(rest) {
            return (text.to_string(), "utf-8-sig");
        }
    }
    if let Some(rest) = bytes.strip_prefix(&[0xFF, 0xFE]) {
        let (decoded, had_errors) = decode_utf16(rest, encoding_rs::UTF_16LE);
        if !had_errors {
            return (decoded, "utf-16le");
        }
    }
    if let Some(rest) = bytes.strip_prefix(&[0xFE, 0xFF]) {
        let (decoded, had_errors) = decode_utf16(rest, encoding_rs::UTF_16BE);
        if !had_errors {
            return (decoded, "utf-16be");
        }
    }
    if let Ok(text) = std::str::from_utf8(bytes) {
        return (text.to_string(), "utf-8");
    }
    for (encoding, label) in [
        (encoding_rs::WINDOWS_1251, "cp1251"),
        (encoding_rs::WINDOWS_1252, "cp1252"),
    ] {
        let (decoded, _actual, had_errors) = encoding.decode(bytes);
        if !had_errors {
            return (decoded.into_owned(), label);
        }
    }
    // latin-1 maps every byte to a character, so it can never fail.
    (bytes.iter().map(|byte| *byte as char).collect(), "latin-1")
}

fn decode_utf16(bytes: &[u8], encoding: &'static encoding_rs::Encoding) -> (String, bool) {
    let (decoded, _actual, had_errors) = encoding.decode(bytes);
    (decoded.into_owned(), had_errors)
}

/// Split text into physical lines, keeping their terminators.
///
/// `\r\n`, `\n` and a lone `\r` are all line breaks, which is what the game's
/// own files use; dropping the terminator here would rewrite line endings of a
/// file nobody asked to reformat.
pub fn split_physical_lines(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    let mut chars = text.chars().peekable();
    while let Some(character) = chars.next() {
        match character {
            '\r' => {
                current.push('\r');
                if chars.peek() == Some(&'\n') {
                    current.push('\n');
                    chars.next();
                }
                lines.push(std::mem::take(&mut current));
            }
            '\n' => {
                current.push('\n');
                lines.push(std::mem::take(&mut current));
            }
            _ => current.push(character),
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

/// Separate a line's content from its terminator.
pub fn split_line_ending(line: &str) -> (&str, &str) {
    if let Some(stem) = line.strip_suffix("\r\n") {
        (stem, "\r\n")
    } else if let Some(stem) = line.strip_suffix('\n') {
        (stem, "\n")
    } else if let Some(stem) = line.strip_suffix('\r') {
        (stem, "\r")
    } else {
        (line, "")
    }
}

/// Read a localisation file, detecting its encoding.
pub fn read_localisation_lines(
    path: &Path,
    log: &mut dyn FnMut(&str, LogTag),
) -> Result<(Vec<String>, &'static str)> {
    let bytes =
        fs::read(path).with_context(|| format!("не удалось прочитать {}", path.display()))?;
    let (text, encoding) = decode_source(&bytes);
    if encoding != "utf-8-sig" {
        log(
            &format!(
                "  Файл прочитан как {encoding} (не UTF-8); результат будет пересохранён в UTF-8 BOM"
            ),
            LogTag::Warning,
        );
    }
    Ok((split_physical_lines(&text), encoding))
}

// ---------------------------------------------------------------------------
// Parsing one value line
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValueParts {
    pub prefix: String,
    pub value: String,
    pub suffix: String,
}

/// True when the character at `index` follows an odd number of backslashes.
fn is_escaped(text: &str, index: usize) -> bool {
    let bytes = text.as_bytes();
    let mut backslashes = 0usize;
    let mut cursor = index;
    while cursor > 0 && bytes[cursor - 1] == b'\\' {
        backslashes += 1;
        cursor -= 1;
    }
    backslashes % 2 == 1
}

/// Split `KEY:0 "value"` into prefix, value and suffix.
///
/// The value runs to the last quote that leaves only whitespace or a comment
/// behind it, which is how the game reads a localisation line. Real files are
/// full of quotes that were never escaped (`"§L"Shoot them again"§!"`), so
/// stopping at the first quote would cut the value in half and rejecting the
/// line would throw away the whole file.
pub fn split_value_line(content: &str) -> Option<ValueParts> {
    let captures = value_prefix_re().captures(content)?;
    let prefix = captures.get(1)?.as_str().to_string();
    let start = prefix.len();
    let bytes = content.as_bytes();
    if bytes.get(start) != Some(&b'"') {
        return None;
    }

    let mut position = content.len();
    while position > start + 1 {
        position -= 1;
        if bytes[position] != b'"' || is_escaped(content, position) {
            continue;
        }
        let suffix = &content[position + 1..];
        if value_suffix_re().is_match(suffix) {
            return Some(ValueParts {
                prefix,
                value: content[start + 1..position].to_string(),
                suffix: suffix.to_string(),
            });
        }
    }
    None
}

/// Localisation key without its revision number.
///
/// `KEY:0` and `KEY:12` are the same key: the number only tracks revisions and
/// the game treats two such lines as duplicates.
pub fn normalize_key(prefix: &str) -> String {
    key_version_re().replace(prefix.trim(), "").to_string()
}

/// Stable fingerprint of a source value, used to detect edited source lines.
pub fn source_text_hash(text: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    format!("{:x}", hasher.finalize())
}

// ---------------------------------------------------------------------------
// Preparing the target file
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct PreparedFile {
    pub lines: Vec<String>,
    pub header_indexes: HashSet<usize>,
    pub structure_changed: bool,
}

/// Return a single-target-language file with the required first-line header.
///
/// When a source file contains several language databases, only the one
/// matching the selected source language is kept, instead of translating all of
/// them into duplicate target keys. `None` means "refuse this file": a
/// translation made from the wrong source language is not a translation.
pub fn prepare_target_localisation_lines(
    lines: &[String],
    target_name: &str,
    source_lang: Option<&str>,
    log: &mut dyn FnMut(&str, LogTag),
) -> Option<PreparedFile> {
    let mut header_rows: Vec<(usize, String)> = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        let (content, _) = split_line_ending(line);
        if let Some(captures) = language_header_re().captures(content) {
            if let Some(language) = captures.get(1) {
                header_rows.push((index, lower(language.as_str())));
            }
        }
    }

    let mut distinct_languages: Vec<String> = Vec::new();
    for (_, language) in &header_rows {
        if !distinct_languages.contains(language) {
            distinct_languages.push(language.clone());
        }
    }

    let selected_source_name = source_lang.and_then(source_language_name);

    if distinct_languages.len() == 1 {
        if let Some(expected) = selected_source_name {
            let actual = distinct_languages[0].as_str();
            if expected != actual {
                log(
                    &format!(
                        "  Файл пропущен: заголовок l_{actual} не совпадает с выбранным \
                         исходным языком {source_lang:?}; перевод из неверного исходного \
                         языка небезопасен"
                    ),
                    LogTag::Error,
                );
                return None;
            }
        }
    }

    let mut working: Vec<String> = lines.to_vec();
    if distinct_languages.len() > 1 {
        let Some(source_name) = selected_source_name else {
            log(
                &format!(
                    "  Файл содержит несколько языковых баз ({}), но исходный язык не задан; \
                     файл пропущен",
                    distinct_languages
                        .iter()
                        .map(|name| format!("l_{name}"))
                        .collect::<Vec<_>>()
                        .join(", ")
                ),
                LogTag::Error,
            );
            return None;
        };
        if !distinct_languages
            .iter()
            .any(|name| name.as_str() == source_name)
        {
            log(
                &format!(
                    "  Файл содержит несколько языковых баз ({}), но выбранный исходный \
                     язык {source_lang:?} не совпадает ни с одной; файл пропущен",
                    distinct_languages
                        .iter()
                        .map(|name| format!("l_{name}"))
                        .collect::<Vec<_>>()
                        .join(", ")
                ),
                LogTag::Error,
            );
            return None;
        }

        let first_header = header_rows[0].0;
        let mut selected_blocks: Vec<String> = Vec::new();
        for (row_number, (start, language)) in header_rows.iter().enumerate() {
            let stop = if row_number + 1 < header_rows.len() {
                header_rows[row_number + 1].0
            } else {
                working.len()
            };
            if language.as_str() == source_name {
                selected_blocks.extend(working[*start..stop].iter().cloned());
            }
        }
        let mut rebuilt: Vec<String> = working[..first_header].to_vec();
        rebuilt.extend(selected_blocks);
        working = rebuilt;
        log(
            &format!(
                "  В файле несколько языковых баз; оставлена l_{source_name} (исходный язык \
                 {:?})",
                source_lang.unwrap_or("")
            ),
            LogTag::Info,
        );
    }

    let mut output: Vec<String> = Vec::new();
    let mut header_indexes: HashSet<usize> = HashSet::new();
    let mut structure_changed = false;
    for (index, line) in working.iter().enumerate() {
        let (content, ending) = split_line_ending(line);
        if language_header_re().is_match(content) {
            output.push(format!("l_{target_name}:{ending}"));
            header_indexes.insert(index);
            if content != format!("l_{target_name}:") {
                structure_changed = true;
            }
        } else {
            output.push(line.clone());
        }
    }

    if !header_indexes.contains(&0) {
        let line_ending = output
            .iter()
            .map(|line| split_line_ending(line).1)
            .find(|ending| !ending.is_empty())
            .unwrap_or("\n")
            .to_string();
        output.insert(0, format!("l_{target_name}:{line_ending}"));
        let mut shifted: HashSet<usize> = HashSet::new();
        shifted.insert(0);
        for index in header_indexes {
            shifted.insert(index + 1);
        }
        header_indexes = shifted;
        structure_changed = true;
    }

    Some(PreparedFile {
        lines: output,
        header_indexes,
        structure_changed,
    })
}

// ---------------------------------------------------------------------------
// Existing translations
// ---------------------------------------------------------------------------

/// Load non-empty values from an existing target-language file.
pub fn load_existing_translations(
    dst_path: &Path,
    log: &mut dyn FnMut(&str, LogTag),
) -> HashMap<String, String> {
    let mut existing = HashMap::new();
    if !dst_path.exists() {
        return existing;
    }
    let lines = match read_localisation_lines(dst_path, log) {
        Ok((lines, _)) => lines,
        Err(error) => {
            log(
                &format!("Ошибка чтения уже переведённого файла: {error}"),
                LogTag::Warning,
            );
            return existing;
        }
    };
    for line in &lines {
        let (content, _) = split_line_ending(line);
        if let Some(captures) = value_re().captures(content) {
            let prefix = captures.get(1).map(|m| m.as_str()).unwrap_or("");
            let value = captures.get(2).map(|m| m.as_str()).unwrap_or("");
            let suffix = captures.get(3).map(|m| m.as_str()).unwrap_or("");
            if value_suffix_re().is_match(suffix) && !value.trim().is_empty() {
                existing.insert(normalize_key(prefix), value.to_string());
            }
        }
    }
    existing
}

// ---------------------------------------------------------------------------
// Output paths
// ---------------------------------------------------------------------------

/// Build an output path using a valid HOI4 language directory and suffix.
pub fn rename_dst(
    src_path: &Path,
    src_root: &Path,
    dst_root: &Path,
    target_lang: &str,
) -> Result<PathBuf> {
    let target_name = get_hoi4_language_name(target_lang)?;
    let relative = match src_path.strip_prefix(src_root) {
        Ok(relative) => relative.to_path_buf(),
        // A file outside the root still gets a usable name instead of panicking.
        Err(_) => src_path
            .file_name()
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("unnamed.yml")),
    };

    let parent = relative
        .parent()
        .map(|path| path.to_path_buf())
        .unwrap_or_default();
    let file_name = relative
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();

    let (stem, extension) = match file_name.rsplit_once('.') {
        Some((stem, extension)) => (stem.to_string(), extension.to_string()),
        None => (file_name.clone(), String::new()),
    };
    if !extension.eq_ignore_ascii_case("yml") {
        bail!("Локализационные файлы HOI4 должны иметь расширение .yml");
    }

    // Replace a previous internal-language suffix, or add one if the input is a
    // generic .yml name.
    let stem = language_suffix_re().replace(&stem, "").to_string();
    let new_name = format!("{stem}_l_{target_name}.yml");
    let rel_renamed = if parent.as_os_str().is_empty() {
        PathBuf::from(&new_name)
    } else {
        parent.join(&new_name)
    };

    // Folder names are organizational only, but update recognized language
    // directories to keep the conventional mod layout.
    let directories = hoi4_language_directories();
    let mut replaced = PathBuf::new();
    for component in rel_renamed.components() {
        match component {
            Component::Normal(part) => {
                let text = part.to_string_lossy().to_string();
                if directories.contains(&lower(&text)) {
                    replaced.push(target_name);
                } else {
                    replaced.push(text);
                }
            }
            other => replaced.push(other.as_os_str()),
        }
    }
    Ok(dst_root.join(replaced))
}

/// Reject equal or nested source/output roots so a run cannot reprocess its own
/// output or overwrite the mod it is reading.
pub fn validate_directory_relationships(src_root: &Path, dst_root: &Path) -> Result<()> {
    let source = crate::config::normalized_abs(src_root);
    let destination = crate::config::normalized_abs(dst_root);
    if crate::config::path_contains(&source, &destination)
        || crate::config::path_contains(&destination, &source)
    {
        bail!(
            "Исходная папка и папка перевода пересекаются. Выберите отдельные соседние \
             каталоги, чтобы не перезаписать исходники и не обрабатывать результаты повторно."
        );
    }
    Ok(())
}

/// Collect every `.yml` file under `root`, skipping the game's `languages.yml`.
pub fn collect_yml_files(root: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    if root.exists() {
        walk_yml(root, &mut found);
    }
    found
}

fn walk_yml(directory: &Path, found: &mut Vec<PathBuf>) {
    let Ok(reader) = fs::read_dir(directory) else {
        return;
    };
    let mut entries: Vec<PathBuf> = reader
        .filter_map(|entry| entry.ok())
        .map(|e| e.path())
        .collect();
    entries.sort();

    let mut subdirectories = Vec::new();
    for path in entries {
        // `symlink_metadata` does not follow the link, so a symlinked or
        // junctioned directory is never walked into: a cycle in a broken mod
        // would otherwise recurse until the stack ran out. `os.walk` in the
        // Python build did not follow links either.
        let is_directory = fs::symlink_metadata(&path)
            .map(|metadata| metadata.is_dir())
            .unwrap_or(false);
        if is_directory {
            subdirectories.push(path);
        } else if let Some(name) = path.file_name().and_then(|name| name.to_str()) {
            if name.ends_with(".yml") && !name.eq_ignore_ascii_case("languages.yml") {
                found.push(path);
            }
        }
    }
    for subdirectory in subdirectories {
        walk_yml(&subdirectory, found);
    }
}

/// Reject distinct source files that would overwrite the same output path.
pub fn validate_output_path_collisions(pairs: &[(PathBuf, PathBuf)]) -> Result<()> {
    let mut destinations: Vec<(String, Vec<&PathBuf>)> = Vec::new();
    for (source, destination) in pairs {
        let normalized = path_key(destination).to_lowercase();
        match destinations.iter_mut().find(|(key, _)| *key == normalized) {
            Some((_, sources)) => {
                if !sources.iter().any(|existing| *existing == source) {
                    sources.push(source);
                }
            }
            None => destinations.push((normalized, vec![source])),
        }
    }

    let mut collisions: Vec<(String, Vec<String>)> = Vec::new();
    for (key, sources) in destinations {
        if sources.len() > 1 {
            collisions.push((
                key,
                sources
                    .iter()
                    .map(|path| path.display().to_string())
                    .collect(),
            ));
        }
    }

    if !collisions.is_empty() {
        let details = collisions
            .iter()
            .take(10)
            .map(|(destination, sources)| format!("{destination} <- {}", sources.join(", ")))
            .collect::<Vec<_>>()
            .join("; ");
        let remaining = collisions.len().saturating_sub(10);
        let more = if remaining > 0 {
            format!("; и ещё {remaining} коллизий")
        } else {
            String::new()
        };
        bail!("Коллизия выходных файлов: {details}{more}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn no_log() -> impl FnMut(&str, LogTag) {
        |_message: &str, _tag: LogTag| {}
    }

    #[test]
    fn keys_lose_only_their_revision_number() {
        assert_eq!(normalize_key(" GER_test:0 "), "GER_test");
        assert_eq!(normalize_key("GER_test:12"), "GER_test");
        assert_eq!(normalize_key("GER_test"), "GER_test");
        assert_eq!(normalize_key("GER_1.2:3"), "GER_1.2");
    }

    #[test]
    fn values_with_unescaped_quotes_are_read_to_the_last_quote() {
        let parts = split_value_line(r#" GER_x:0 "§L"Shoot them again, they're not dead yet!"§!""#)
            .expect("line parses");
        assert_eq!(parts.prefix, " GER_x:0 ");
        // The line opens with a quote, then the value itself contains quoted
        // speaker text and a colour code: only the very last quote closes it.
        assert_eq!(
            parts.value,
            "§L\"Shoot them again, they're not dead yet!\"§!"
        );
        assert_eq!(parts.suffix, "");
    }

    #[test]
    fn comments_after_the_value_are_not_part_of_it() {
        let parts = split_value_line(r#"KEY:0 "Value" # a "quoted" comment"#).expect("line parses");
        assert_eq!(parts.value, "Value");
        assert_eq!(parts.suffix, r#" # a "quoted" comment"#);
    }

    #[test]
    fn an_unclosed_value_is_rejected() {
        assert!(split_value_line(r#"KEY:0 "never closed"#).is_none());
    }

    #[test]
    fn every_declared_encoding_is_reachable() {
        let done = "KEY:0 \"value\"\n";
        let cases: Vec<(&str, Vec<u8>)> = vec![
            ("utf-8-sig", {
                let mut bytes = vec![0xEF, 0xBB, 0xBF];
                bytes.extend_from_slice(done.as_bytes());
                bytes
            }),
            ("utf-8", done.as_bytes().to_vec()),
            ("utf-16le", {
                let mut bytes = vec![0xFF, 0xFE];
                for unit in done.encode_utf16() {
                    bytes.extend_from_slice(&unit.to_le_bytes());
                }
                bytes
            }),
            ("utf-16be", {
                let mut bytes = vec![0xFE, 0xFF];
                for unit in done.encode_utf16() {
                    bytes.extend_from_slice(&unit.to_be_bytes());
                }
                bytes
            }),
            // 0x98 has no cp1251 mapping, so this file can only be cp1252.
            ("cp1252", b"KEY:0 \"K\x98nig\"\n".to_vec()),
            // 0x98 and 0x81 are undefined in cp1252 as well.
            ("latin-1", b"KEY:0 \"K\x98\x81nig\"\n".to_vec()),
        ];
        for (expected, bytes) in cases {
            let (text, encoding) = decode_source(&bytes);
            assert_eq!(encoding, expected, "bytes {:?}", &bytes[..2]);
            assert!(text.contains("KEY:0"), "{encoding}: {text:?}");
        }
        // Cyrillic in cp1251, checked through an actual character rather than
        // the label alone.
        let (text, encoding) = decode_source(b"KEY:0 \"\xcf\xf0\xe8\xe2\xe5\xf2\"\n");
        assert_eq!(encoding, "cp1251");
        assert!(text.contains("Привет"), "{text:?}");
        assert!(SOURCE_ENCODINGS.contains(&encoding));
    }

    #[test]
    fn symlinked_directories_are_not_walked_into() {
        let root = std::env::temp_dir().join(format!("hoi4-walk-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let real = root.join("localisation").join("english");
        std::fs::create_dir_all(&real).expect("dirs");
        std::fs::write(real.join("a_l_english.yml"), "l_english:\n").expect("write");
        std::fs::write(real.join("languages.yml"), "l_english:\n").expect("write");
        #[cfg(unix)]
        {
            let link = root.join("localisation").join("loop");
            let _ = std::os::unix::fs::symlink(&root, &link);
        }
        let files = collect_yml_files(&root);
        let names: Vec<String> = files
            .iter()
            .filter_map(|path| path.file_name())
            .map(|name| name.to_string_lossy().to_string())
            .collect();
        assert_eq!(names, vec!["a_l_english.yml".to_string()]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn escaped_quotes_do_not_close_the_value() {
        // A raw string cannot end with a bare quote before its `"#` terminator,
        // so the escaped form is written out explicitly.
        let parts = split_value_line("KEY:0 \"he said \\\"hi\\\"\"").expect("line parses");
        assert_eq!(parts.value, "he said \\\"hi\\\"");
        assert_eq!(parts.suffix, "");
    }

    #[test]
    fn physical_lines_keep_their_terminators() {
        let lines = split_physical_lines("a\r\nb\nc\rd");
        let rendered: Vec<&str> = lines.iter().map(|line| line.as_str()).collect();
        assert_eq!(rendered, vec!["a\r\n", "b\n", "c\r", "d"]);
        assert_eq!(split_line_ending("b\n"), ("b", "\n"));
        assert_eq!(split_line_ending("b"), ("b", ""));
    }

    #[test]
    fn the_header_is_rewritten_for_the_target_language() {
        let lines = vec!["l_english:\n".to_string(), "KEY:0 \"Value\"\n".to_string()];
        let mut log = no_log();
        let prepared =
            prepare_target_localisation_lines(&lines, "russian", Some("en"), &mut log).expect("ok");
        assert_eq!(prepared.lines[0], "l_russian:\n");
        assert!(prepared.structure_changed);
        assert!(prepared.header_indexes.contains(&0));
    }

    #[test]
    fn a_missing_header_is_inserted_with_the_file_line_ending() {
        let lines = vec!["KEY:0 \"Value\"\r\n".to_string()];
        let mut log = no_log();
        let prepared =
            prepare_target_localisation_lines(&lines, "russian", Some("en"), &mut log).expect("ok");
        assert_eq!(prepared.lines[0], "l_russian:\r\n");
        assert_eq!(prepared.lines[1], "KEY:0 \"Value\"\r\n");
        assert!(prepared.header_indexes.contains(&0));
    }

    #[test]
    fn a_file_in_another_source_language_is_refused() {
        let lines = vec!["l_german:\n".to_string(), "KEY:0 \"Wert\"\n".to_string()];
        let mut log = no_log();
        assert!(
            prepare_target_localisation_lines(&lines, "russian", Some("en"), &mut log).is_none()
        );
    }

    #[test]
    fn multi_language_files_keep_only_the_selected_database() {
        let lines = vec![
            "l_english:\n".to_string(),
            "A:0 \"One\"\n".to_string(),
            "l_german:\n".to_string(),
            "A:0 \"Eins\"\n".to_string(),
        ];
        let mut log = no_log();
        let prepared =
            prepare_target_localisation_lines(&lines, "russian", Some("de"), &mut log).expect("ok");
        assert_eq!(
            prepared.lines,
            vec!["l_russian:\n".to_string(), "A:0 \"Eins\"\n".to_string()]
        );
    }

    #[test]
    fn output_paths_get_the_game_language_suffix_and_directory() {
        let root = Path::new("/mods/demo");
        let source = Path::new("/mods/demo/localisation/english/events_l_english.yml");
        let out = rename_dst(source, root, Path::new("/out"), "pt").expect("path");
        assert_eq!(
            out,
            PathBuf::from("/out/localisation/braz_por/events_l_braz_por.yml")
        );

        let generic = Path::new("/mods/demo/localisation/english/events.yml");
        let out = rename_dst(generic, root, Path::new("/out"), "ru").expect("path");
        assert_eq!(
            out,
            PathBuf::from("/out/localisation/russian/events_l_russian.yml")
        );
    }

    #[test]
    fn non_yml_output_names_are_rejected() {
        let root = Path::new("/mods/demo");
        let source = Path::new("/mods/demo/a.txt");
        assert!(rename_dst(source, root, Path::new("/out"), "ru").is_err());
    }

    #[test]
    fn overlapping_roots_are_rejected() {
        assert!(
            validate_directory_relationships(Path::new("/a/mod"), Path::new("/a/mod/out")).is_err()
        );
        assert!(validate_directory_relationships(Path::new("/a/mod"), Path::new("/a/out")).is_ok());
    }

    #[test]
    fn colliding_outputs_are_rejected() {
        let pairs = vec![
            (
                PathBuf::from("/mod/localisation/english/a_l_english.yml"),
                PathBuf::from("/out/a.yml"),
            ),
            (
                PathBuf::from("/mod/localisation/english/b_l_english.yml"),
                PathBuf::from("/out/a.yml"),
            ),
        ];
        assert!(validate_output_path_collisions(&pairs).is_err());

        let unique = vec![
            (
                PathBuf::from("/mod/a_l_english.yml"),
                PathBuf::from("/out/a.yml"),
            ),
            (
                PathBuf::from("/mod/b_l_english.yml"),
                PathBuf::from("/out/b.yml"),
            ),
        ];
        assert!(validate_output_path_collisions(&unique).is_ok());
    }
}
