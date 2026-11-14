//! Reuse the game's own translations instead of machine-translating them.
//!
//! Vanilla Hearts of Iron IV ships ~100 000 localisation lines and an official
//! translation for every supported language. Mods copy those lines verbatim all
//! the time — focus names, unit types, country adjectives, interface labels — and
//! sending them to Google produces a second, worse translation of a string the
//! game already translates well.
//!
//! This module indexes `<english value> → <official translation>` from a vanilla
//! installation (or from any other already-translated mod).

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::config::LogTag;
use crate::localisation::{
    get_hoi4_language_name, normalize_key, read_localisation_lines, split_value_line,
};

/// Values shorter than this are markers, numbers or single letters; matching
/// them would replace half the mod with the wrong string.
pub const MIN_USEFUL_LENGTH: usize = 3;

fn language_suffix(language: &str) -> Result<String> {
    let name = get_hoi4_language_name(language)?;
    Ok(format!("_l_{}.yml", name.to_lowercase()))
}

fn quiet_log() -> impl FnMut(&str, LogTag) {
    |message: &str, tag: LogTag| match tag {
        LogTag::Error => log::error!("{message}"),
        LogTag::Warning => log::warn!("{message}"),
        LogTag::Info => log::info!("{message}"),
    }
}

/// Parse one localisation file into `key → value`.
pub fn read_values(path: &Path) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    let mut log = quiet_log();
    let lines = match read_localisation_lines(path, &mut log) {
        Ok((lines, _)) => lines,
        Err(error) => {
            log::warn!("Ванильный файл пропущен ({}): {error}", path.display());
            return values;
        }
    };
    for line in &lines {
        let content = line.trim_end_matches(['\r', '\n']);
        if let Some(parts) = split_value_line(content) {
            // KEY:0 and KEY:3 are the same key; the game's own files are full of
            // revision numbers, so they must not decide whether a line matches.
            values.insert(normalize_key(&parts.prefix), parts.value);
        }
    }
    values
}

fn iter_language_files(root: &Path, language: &str) -> Result<Vec<PathBuf>> {
    let suffix = language_suffix(language)?;
    let mut found = Vec::new();
    walk(root, &suffix, &mut found);
    found.sort();
    Ok(found)
}

fn walk(directory: &Path, suffix: &str, found: &mut Vec<PathBuf>) {
    let Ok(reader) = std::fs::read_dir(directory) else {
        return;
    };
    let mut entries: Vec<PathBuf> = reader
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .collect();
    entries.sort();

    let mut subdirectories = Vec::new();
    for path in entries {
        if path.is_dir() {
            subdirectories.push(path);
        } else if let Some(name) = path.file_name().and_then(|name| name.to_str()) {
            if name.to_lowercase().ends_with(suffix) {
                found.push(path);
            }
        }
    }
    for subdirectory in subdirectories {
        walk(&subdirectory, suffix, found);
    }
}

/// Maps a source-language value to the game's own translation of it.
#[derive(Debug, Default, Clone)]
pub struct VanillaIndex {
    translations: BTreeMap<String, String>,
    pub origin: String,
    pub conflicts: usize,
}

impl VanillaIndex {
    pub fn new(translations: BTreeMap<String, String>, origin: impl Into<String>) -> Self {
        Self {
            translations,
            origin: origin.into(),
            conflicts: 0,
        }
    }

    pub fn len(&self) -> usize {
        self.translations.len()
    }

    pub fn is_empty(&self) -> bool {
        self.translations.is_empty()
    }

    /// The vanilla translation of `value`, if the game has one.
    pub fn lookup(&self, value: &str) -> Option<String> {
        if value.is_empty() {
            return None;
        }
        let found = self
            .translations
            .get(value)
            .or_else(|| self.translations.get(value.trim()))?;
        if found.is_empty() {
            None
        } else {
            Some(found.clone())
        }
    }

    /// Index a vanilla `localisation` folder (or a mod root).
    pub fn build(
        root: &Path,
        source_lang: &str,
        target_lang: &str,
        log: &mut dyn FnMut(&str, LogTag),
    ) -> Result<Self> {
        let source_files = iter_language_files(root, source_lang)?;
        let target_files = iter_language_files(root, target_lang)?;

        if source_files.is_empty() || target_files.is_empty() {
            log(
                &format!(
                    "Ванильные переводы: не найдены файлы на обоих языках \
                     ({source_lang} / {target_lang}); переиспользование выключено"
                ),
                LogTag::Warning,
            );
            return Ok(Self::new(BTreeMap::new(), root.display().to_string()));
        }

        let target_suffix = language_suffix(target_lang)?;
        let mut target_by_name: HashMap<String, PathBuf> = HashMap::new();
        for path in &target_files {
            if let Some(name) = path.file_name().and_then(|name| name.to_str()) {
                target_by_name.insert(
                    name.replace(&target_suffix, "").to_lowercase(),
                    path.clone(),
                );
            }
        }

        let mut translated: BTreeMap<String, String> = BTreeMap::new();
        let mut conflicts = 0usize;
        let source_suffix = language_suffix(source_lang)?;

        for source_path in &source_files {
            // Prefer the same file name in the target language: the same key
            // means different things in different files only in broken mods, but
            // joining by file first keeps the index honest.
            let Some(source_name) = source_path.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            let key = source_name.replace(&source_suffix, "").to_lowercase();
            let Some(target_path) = target_by_name.get(&key) else {
                continue;
            };
            let source_values = read_values(source_path);
            let target_values = read_values(target_path);
            if source_values.is_empty() || target_values.is_empty() {
                continue;
            }
            for (key_name, source_value) in &source_values {
                let Some(target_value) = target_values.get(key_name) else {
                    continue;
                };
                if target_value.is_empty()
                    || source_value.chars().count() < MIN_USEFUL_LENGTH
                    || source_value == target_value
                {
                    continue;
                }
                match translated.get(source_value) {
                    Some(existing) if existing != target_value => {
                        conflicts += 1;
                        continue;
                    }
                    _ => {
                        translated.insert(source_value.clone(), target_value.clone());
                    }
                }
            }
        }

        let mut index = Self::new(translated, root.display().to_string());
        index.conflicts = conflicts;

        if index.is_empty() {
            log(
                "Ванильные переводы: совпадений не найдено \
                 (проверьте путь к папке localisation и пару языков)",
                LogTag::Warning,
            );
        } else {
            log(
                &format!(
                    "Ванильные переводы: {} готовых строк из {} файлов",
                    index.len(),
                    source_files.len()
                ),
                LogTag::Info,
            );
        }
        if conflicts > 0 {
            log(
                &format!(
                    "Ванильные переводы: {conflicts} строк имеют разный официальный перевод \
                     в разных файлах; взят первый вариант"
                ),
                LogTag::Info,
            );
        }
        Ok(index)
    }
}

/// Build an index when a path is given, otherwise return `None`.
pub fn build_vanilla_index(
    path: Option<&Path>,
    source_lang: &str,
    target_lang: &str,
    log: &mut dyn FnMut(&str, LogTag),
) -> Result<Option<VanillaIndex>> {
    let Some(path) = path else {
        return Ok(None);
    };
    if !path.is_dir() {
        log(
            &format!("Ванильные переводы: папка не найдена: {}", path.display()),
            LogTag::Warning,
        );
        return Ok(None);
    }
    VanillaIndex::build(path, source_lang, target_lang, &mut *log)
        .with_context(|| "не удалось построить индекс ванильных переводов")
        .map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(path: &Path, content: &str) {
        std::fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
        std::fs::write(path, content).expect("write");
    }

    #[test]
    fn official_translations_are_reused_by_value() {
        let dir = std::env::temp_dir().join(format!("hoi4-vanilla-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        write(
            &dir.join("localisation")
                .join("english")
                .join("units_l_english.yml"),
            "l_english:\n infantry_equipment:0 \"Infantry Equipment\"\n tiny:0 \"ab\"\n",
        );
        write(
            &dir.join("localisation")
                .join("russian")
                .join("units_l_russian.yml"),
            "l_russian:\n infantry_equipment:0 \"Пехотное снаряжение\"\n tiny:0 \"аб\"\n",
        );

        let mut log = quiet_log();
        let index = VanillaIndex::build(&dir, "en", "ru", &mut log).expect("index");
        assert_eq!(index.len(), 1, "short values must not be indexed");
        assert_eq!(
            index.lookup("Infantry Equipment").as_deref(),
            Some("Пехотное снаряжение")
        );
        assert!(index.lookup("Unknown").is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_language_pair_is_reported_not_fatal() {
        let dir = std::env::temp_dir().join(format!("hoi4-vanilla-empty-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("dir");
        let mut log = quiet_log();
        let index = VanillaIndex::build(&dir, "en", "ru", &mut log).expect("index");
        assert!(index.is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn no_path_means_no_index() {
        let mut log = quiet_log();
        assert!(build_vanilla_index(None, "en", "ru", &mut log)
            .expect("ok")
            .is_none());
    }
}
