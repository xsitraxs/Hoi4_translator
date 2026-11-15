//! Building a launcher-ready translation mod out of a finished run.
//!
//! The pipeline writes `localisation/<language>/*.yml`; the game launcher also
//! wants a folder in `mod/` with a `descriptor.mod` inside and a sibling
//! `<name>.mod` that points at it. Getting those three pieces to agree by hand
//! is where translation mods usually break (a space in the file name, `path` in
//! the wrong file, a descriptor that disagrees with the `.mod`), so the tool
//! builds them instead.
//!
//! Everything that could destroy work is done defensively: the mod is assembled
//! in a scratch folder and moved into place, and an existing folder is replaced
//! only when its descriptor says it belongs to this same mod.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use anyhow::{bail, Context, Result};

use crate::config::{atomic_write_bytes, LogTag};
use crate::localisation::{collect_yml_files, get_hoi4_language_name};

/// Version written into the descriptor when the caller does not care.
pub const DEFAULT_MOD_VERSION: &str = "1.0.0";
/// Game version the mod claims to support when the target mod says nothing.
pub const DEFAULT_SUPPORTED_VERSION: &str = "1.*";
/// Launcher tag a translation normally carries.
pub const DEFAULT_TAG: &str = "Translation";

/// Longest folder name the launcher is given; well under every path limit.
const MAX_FOLDER_LEN: usize = 48;

#[derive(Debug, Clone)]
pub struct ModPackOptions {
    /// Name shown in the launcher; may be Cyrillic, it is not a path.
    pub display_name: String,
    /// Folder and `.mod` file name: ASCII, no spaces.
    pub folder: String,
    pub version: String,
    pub supported_version: String,
    /// `name=` of the mod being translated, written as a dependency.
    pub dependency: Option<String>,
    pub tags: Vec<String>,
    /// Translator code of the language (`ru`), resolved to a game database.
    pub target_lang_code: String,
    /// The `--dst` folder of the run, which holds `localisation/…`.
    pub source_dir: PathBuf,
    /// Where the mod folder and its `.mod` file are built.
    pub output_dir: PathBuf,
    pub install: bool,
    /// Where `--install` copies the mod; `None` means the game's own `mod/`.
    pub install_dir: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct ModPackResult {
    pub folder: PathBuf,
    pub mod_file: PathBuf,
    pub descriptor: PathBuf,
    pub files: usize,
    pub installed: Option<PathBuf>,
}

/// What `descriptor.mod` of the mod being translated says about itself.
#[derive(Debug, Default, Clone)]
pub struct TargetMod {
    pub name: Option<String>,
    pub version: Option<String>,
    pub supported_version: Option<String>,
}

/// The part of a folder name that marks the language: `zh-CN` → `zh_cn`.
fn language_marker(target_lang_code: &str) -> String {
    let marker: String = target_lang_code
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect();
    marker.trim_matches('_').to_string()
}

/// A folder name the launcher accepts: ASCII letters, digits, `_`, `-`.
///
/// Anything else becomes `_` and repeated separators collapse, because the
/// launcher silently ignores a mod whose path contains a space or a non-ASCII
/// character — the user would see "the mod is not there" with no explanation.
///
/// The language marker is appended unless the name already ends with it: a mod
/// called `MyMod` and its translation must not share one folder name, or
/// installing the translation would overwrite the mod it translates.
pub fn mod_folder_name(display_name: &str, target_lang_code: &str) -> String {
    let language = language_marker(target_lang_code);
    let mut name = String::new();
    for character in display_name.chars() {
        let mapped = if character.is_ascii_alphanumeric() || character == '_' || character == '-' {
            character
        } else {
            '_'
        };
        if mapped == '_' && name.ends_with('_') {
            continue;
        }
        name.push(mapped);
    }
    let mut base = name.trim_matches('_').to_string();
    if base.is_empty() {
        return if language.is_empty() {
            "translation".to_string()
        } else {
            format!("translation_{language}")
        };
    }

    if !language.is_empty() {
        let suffix = format!("_{language}");
        if !base.to_lowercase().ends_with(&suffix) {
            base.push_str(&suffix);
        }
    }
    if base.len() > MAX_FOLDER_LEN {
        base.truncate(MAX_FOLDER_LEN);
        base = base
            .trim_end_matches(|character: char| character == '_' || character == '-')
            .to_string();
    }
    if base.trim_matches('_').is_empty() {
        return if language.is_empty() {
            "translation".to_string()
        } else {
            format!("translation_{language}")
        };
    }
    base
}

pub fn validate_folder_name(folder: &str) -> Result<()> {
    if folder.is_empty() {
        bail!("Имя папки мода не может быть пустым");
    }
    if folder.starts_with('.') {
        bail!("Имя папки мода не должно начинаться с точки: {folder:?}");
    }
    if folder.chars().any(char::is_whitespace) {
        bail!("В имени папки мода не должно быть пробелов: {folder:?}");
    }
    if folder.eq_ignore_ascii_case("localisation") {
        bail!(
            "Имя папки мода не может быть localisation: это папка с переводом, её нельзя \
             перезаписывать"
        );
    }
    if !folder
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-' | '.'))
    {
        bail!(
            "Имя папки мода должно быть латиницей без пробелов и спецсимволов: {folder:?} \
             (лаунчер не увидит мод с кириллицей в пути)"
        );
    }
    if folder.len() > 64 {
        bail!("Слишком длинное имя папки мода: {folder:?}");
    }
    Ok(())
}

/// Everything that can be judged before a single line is translated.
pub fn preflight(options: &ModPackOptions) -> Result<()> {
    if options.display_name.trim().is_empty() {
        bail!("Имя мода для лаунчера не может быть пустым: --make-mod \"Имя мода\"");
    }
    if options.version.trim().is_empty() {
        bail!("Версия мода не может быть пустой: --mod-version 1.0.0");
    }
    if options.supported_version.trim().is_empty() {
        bail!("supported_version не может быть пустым: --mod-supported-version \"1.*\"");
    }
    validate_folder_name(&options.folder)
}

fn clean_field(value: &str) -> String {
    value
        .replace('"', "'")
        .replace(['\n', '\r', '\t'], " ")
        .trim()
        .to_string()
}

/// Descriptor text; `with_path` adds the one line the sibling `.mod` needs.
pub fn render_descriptor(options: &ModPackOptions, with_path: bool) -> String {
    let mut text = String::from('\u{feff}');
    text.push_str(&format!(
        "name=\"{}\"\n",
        clean_field(&options.display_name)
    ));
    text.push_str(&format!("version=\"{}\"\n", clean_field(&options.version)));
    text.push_str(&format!(
        "supported_version=\"{}\"\n",
        clean_field(&options.supported_version)
    ));
    text.push_str("tags={\n");
    for tag in &options.tags {
        text.push_str(&format!("\t\"{}\"\n", clean_field(tag)));
    }
    text.push_str("}\n");
    text.push_str("dependencies={\n");
    if let Some(dependency) = &options.dependency {
        text.push_str(&format!("\t\"{}\"\n", clean_field(dependency)));
    }
    text.push_str("}\n");
    if with_path {
        text.push_str(&format!("path=\"mod/{}\"\n", options.folder));
    }
    text
}

/// Split `key = "value"  # comment` into its parts.
///
/// A `#` starts a comment only outside quotes: a mod called `Mod #1` keeps its
/// name instead of being cut at the hash.
fn split_descriptor_line(line: &str) -> Option<(String, String)> {
    let trimmed = line.trim();
    if trimmed.is_empty() || trimmed.starts_with('#') {
        return None;
    }
    let (key, rest) = trimmed.split_once('=')?;
    let key = key.trim().to_lowercase();
    if key.is_empty() {
        return None;
    }
    let mut value = String::new();
    let mut quoted = false;
    for character in rest.chars() {
        match character {
            '"' => quoted = !quoted,
            '#' if !quoted => break,
            _ => value.push(character),
        }
    }
    Some((key, value.trim().to_string()))
}

pub fn read_target_descriptor(path: &Path) -> Result<TargetMod> {
    let descriptor_path = if path.is_dir() {
        path.join("descriptor.mod")
    } else {
        path.to_path_buf()
    };
    let text = fs::read_to_string(&descriptor_path).with_context(|| {
        format!(
            "не удалось прочитать дескриптор мода-цели {}",
            descriptor_path.display()
        )
    })?;
    let text = text.trim_start_matches('\u{feff}');
    let mut target = TargetMod::default();
    for line in text.lines() {
        let Some((key, value)) = split_descriptor_line(line) else {
            continue;
        };
        if value.is_empty() {
            continue;
        }
        match key.as_str() {
            "name" => target.name = Some(value),
            "version" => target.version = Some(value),
            "supported_version" => target.supported_version = Some(value),
            _ => {}
        }
    }
    Ok(target)
}

/// Reject a file the game would not load, before it reaches the mod.
fn check_translation_file(path: &Path, header: &str) -> Result<()> {
    let bytes = fs::read(path).with_context(|| format!("не удалось прочитать {}", path.display()))?;
    if !bytes.starts_with(&[0xEF, 0xBB, 0xBF]) {
        bail!(
            "{}: файл перевода должен быть в UTF-8 с BOM — игра не читает такой файл",
            path.display()
        );
    }
    let text = String::from_utf8(bytes[3..].to_vec())
        .with_context(|| format!("{}: файл не в UTF-8", path.display()))?;
    let first = text
        .lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("")
        .trim()
        .to_string();
    if first != header {
        bail!(
            "{}: первая строка должна быть {header}, а не {first:?} — иначе игра не поймёт язык",
            path.display()
        );
    }
    Ok(())
}

/// Every file of the target language, paired with the place it takes in the mod.
///
/// `--dst` may be the root of the translation (with `localisation/` inside) or
/// the language folder itself, so both layouts are accepted and normalised to
/// `localisation/<language>/<file>`.
pub fn collect_translation_files(root: &Path, database: &str) -> Result<Vec<(PathBuf, PathBuf)>> {
    let candidates = collect_yml_files(root);
    plan_translation_files(root, database, &candidates)
}

/// The same planning over an explicit list, so a build can leave its own
/// previous output out of the search.
fn plan_translation_files(
    root: &Path,
    database: &str,
    candidates: &[PathBuf],
) -> Result<Vec<(PathBuf, PathBuf)>> {
    let suffix = format!("_l_{}.yml", database.to_lowercase());
    let language = database.to_lowercase();
    let header = format!("l_{database}:");

    let mut planned: BTreeMap<PathBuf, PathBuf> = BTreeMap::new();
    for path in candidates {
        let path = path.clone();
        let name = path
            .file_name()
            .map(|name| name.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if !name.ends_with(&suffix) {
            continue;
        }
        let relative = match path.strip_prefix(root) {
            Ok(relative) => relative.to_path_buf(),
            Err(_) => PathBuf::from(path.file_name().unwrap_or_default()),
        };
        let starts_with_localisation = relative
            .components()
            .next()
            .map(|component| {
                component
                    .as_os_str()
                    .to_string_lossy()
                    .eq_ignore_ascii_case("localisation")
            })
            .unwrap_or(false);
        let target = if starts_with_localisation {
            relative
        } else {
            Path::new("localisation")
                .join(&language)
                .join(path.file_name().unwrap_or_default())
        };
        if let Some(previous) = planned.insert(target.clone(), path.clone()) {
            bail!(
                "Два файла претендуют на одно место в моде ({}): {} и {}",
                target.display(),
                previous.display(),
                path.display()
            );
        }
    }

    if planned.is_empty() {
        bail!(
            "В папке {} нет файлов {suffix}: сначала переведите мод в эту папку",
            root.display()
        );
    }
    for path in planned.values() {
        check_translation_file(path, &header)?;
    }
    Ok(planned
        .into_iter()
        .map(|(target, source)| (source, target))
        .collect())
}

/// `Documents/Paradox Interactive/Hearts of Iron IV/mod` for the current user.
pub fn paradox_mods_dir() -> Option<PathBuf> {
    let mut path = if cfg!(windows) || cfg!(target_os = "macos") {
        std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(|home| PathBuf::from(home).join("Documents"))?
    } else {
        // XDG first: a Linux install that moved its data directory keeps it
        // there, and the launcher follows XDG as well.
        match std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .filter(|candidate| candidate.is_absolute())
        {
            Some(directory) => directory,
            None => std::env::var_os("HOME").map(|home| {
                PathBuf::from(home)
                    .join(".local")
                    .join("share")
            })?,
        }
    };
    path.push("Paradox Interactive");
    path.push("Hearts of Iron IV");
    path.push("mod");
    Some(path)
}

fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)
        .with_context(|| format!("не удалось создать {}", destination.display()))?;
    for entry in fs::read_dir(source)
        .with_context(|| format!("не удалось прочитать {}", source.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        let target = destination.join(entry.file_name());
        // `symlink_metadata` keeps a link from being walked as a folder: the
        // tree here is our own, so a loop would be a bug, not a feature.
        let is_directory = fs::symlink_metadata(&path)
            .map(|metadata| metadata.is_dir())
            .unwrap_or(false);
        if is_directory {
            copy_tree(&path, &target)?;
        } else {
            fs::copy(&path, &target).with_context(|| {
                format!("не удалось скопировать {} в {}", path.display(), target.display())
            })?;
        }
    }
    Ok(())
}

/// A hidden sibling name, so a half-built folder is never mistaken for a mod.
fn scratch_path(directory: &Path, folder: &str, tag: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let serial = COUNTER.fetch_add(1, Ordering::Relaxed);
    directory.join(format!(
        ".{folder}.{tag}-{}-{serial}",
        std::process::id()
    ))
}

/// Refuse to touch a folder that holds somebody else's mod.
fn ensure_replaceable(folder: &Path, options: &ModPackOptions) -> Result<()> {
    if !folder.exists() {
        return Ok(());
    }
    if !folder.is_dir() {
        bail!(
            "{} уже существует и это файл, а не папка: уберите его вручную",
            folder.display()
        );
    }
    let descriptor = folder.join("descriptor.mod");
    let owner = if descriptor.is_file() {
        read_target_descriptor(&descriptor)
            .ok()
            .and_then(|target| target.name)
    } else {
        None
    };
    match owner {
        Some(name) if name != clean_field(&options.display_name) => bail!(
            "В {} уже лежит другой мод (name=\"{name}\"); сборка остановлена, чтобы его не \
             потерять. Выберите другое имя папки (--mod-folder) или удалите её сами.",
            folder.display()
        ),
        Some(_) => Ok(()),
        None => {
            let empty = fs::read_dir(folder)
                .map(|mut entries| entries.next().is_none())
                .unwrap_or(false);
            if empty {
                Ok(())
            } else {
                bail!(
                    "{} уже существует и не похожа на этот мод (внутри нет descriptor.mod): \
                     сборка остановлена, чтобы не потерять её содержимое. Уберите папку вручную \
                     или выберите другое имя через --mod-folder.",
                    folder.display()
                )
            }
        }
    }
}

fn install_pack(
    mod_root: &Path,
    mod_file: &Path,
    options: &ModPackOptions,
    log: &mut dyn FnMut(&str, LogTag),
) -> Result<PathBuf> {
    let mods_dir = match &options.install_dir {
        Some(path) => path.clone(),
        None => paradox_mods_dir().context(
            "не удалось найти папку пользователя (HOME/USERPROFILE/XDG_DATA_HOME), \
             чтобы установить мод",
        )?,
    };
    fs::create_dir_all(&mods_dir)
        .with_context(|| format!("не удалось создать {}", mods_dir.display()))?;

    let target = mods_dir.join(&options.folder);
    ensure_replaceable(&target, options)?;

    // Copy next to the target and rename: the mods folder never holds a
    // half-copied mod while the old one is already gone.
    let staging = scratch_path(&mods_dir, &options.folder, "install");
    if staging.exists() {
        let _ = fs::remove_dir_all(&staging);
    }
    if let Err(error) = copy_tree(mod_root, &staging) {
        let _ = fs::remove_dir_all(&staging);
        return Err(error);
    }

    if target.exists() {
        let backup = scratch_path(&mods_dir, &options.folder, "old");
        if backup.exists() {
            let _ = fs::remove_dir_all(&backup);
        }
        if let Err(error) = fs::rename(&target, &backup) {
            let _ = fs::remove_dir_all(&staging);
            return Err(anyhow::Error::new(error).context(format!(
                "не удалось отложить старую версию {} — возможно, игра её держит открытой",
                target.display()
            )));
        }
        if let Err(error) = fs::rename(&staging, &target) {
            let restored = fs::rename(&backup, &target).is_ok();
            let _ = fs::remove_dir_all(&staging);
            let hint = if restored {
                format!("старая версия возвращена в {}", target.display())
            } else {
                format!(
                    "старая версия осталась в {} — перенесите её вручную",
                    backup.display()
                )
            };
            return Err(anyhow::Error::new(error).context(format!(
                "не удалось установить мод в {}: {hint}",
                target.display()
            )));
        }
        let _ = fs::remove_dir_all(&backup);
        log(
            &format!("  Старая версия {} в папке игры заменена", options.folder),
            LogTag::Info,
        );
    } else if let Err(error) = fs::rename(&staging, &target) {
        let _ = fs::remove_dir_all(&staging);
        return Err(anyhow::Error::new(error)
            .context(format!("не удалось установить мод в {}", target.display())));
    }

    let installed_file = mods_dir.join(format!("{}.mod", options.folder));
    let bytes = fs::read(mod_file)
        .with_context(|| format!("не удалось прочитать {}", mod_file.display()))?;
    atomic_write_bytes(&installed_file, &bytes)
        .with_context(|| format!("не удалось записать {}", installed_file.display()))?;
    Ok(target)
}

/// Build the mod folder next to the translation and, optionally, install it.
pub fn build(options: &ModPackOptions, log: &mut dyn FnMut(&str, LogTag)) -> Result<ModPackResult> {
    preflight(options)?;
    let database = get_hoi4_language_name(&options.target_lang_code)?;
    let mod_root = options.output_dir.join(&options.folder);

    // A previous build sits inside the same output folder. It is output, not a
    // source: without this filter a second run would package the first one's
    // copy of every file and then refuse itself over the collision.
    let candidates: Vec<PathBuf> = collect_yml_files(&options.source_dir)
        .into_iter()
        .filter(|path| !path.starts_with(&mod_root))
        .collect();
    let files = plan_translation_files(&options.source_dir, database, &candidates)?;
    ensure_replaceable(&mod_root, options)?;

    fs::create_dir_all(&options.output_dir)
        .with_context(|| format!("не удалось создать {}", options.output_dir.display()))?;
    let staging = scratch_path(&options.output_dir, &options.folder, "build");
    if staging.exists() {
        let _ = fs::remove_dir_all(&staging);
    }
    fs::create_dir_all(&staging)
        .with_context(|| format!("не удалось создать {}", staging.display()))?;

    let assembled = (|| -> Result<usize> {
        let mut copied = 0usize;
        for (source, relative) in &files {
            let destination = staging.join(relative);
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)
                    .with_context(|| format!("не удалось создать {}", parent.display()))?;
            }
            fs::copy(source, &destination).with_context(|| {
                format!(
                    "не удалось скопировать {} в {}",
                    source.display(),
                    destination.display()
                )
            })?;
            copied += 1;
        }
        let descriptor = staging.join("descriptor.mod");
        atomic_write_bytes(&descriptor, render_descriptor(options, false).as_bytes())
            .with_context(|| format!("не удалось записать {}", descriptor.display()))?;
        Ok(copied)
    })();
    let copied = match assembled {
        Ok(copied) => copied,
        Err(error) => {
            let _ = fs::remove_dir_all(&staging);
            return Err(error);
        }
    };

    if mod_root.exists() {
        if let Err(error) = fs::remove_dir_all(&mod_root) {
            let _ = fs::remove_dir_all(&staging);
            return Err(anyhow::Error::new(error)
                .context(format!("не удалось освободить {}", mod_root.display())));
        }
    }
    if let Err(error) = fs::rename(&staging, &mod_root) {
        let _ = fs::remove_dir_all(&staging);
        return Err(anyhow::Error::new(error)
            .context(format!("не удалось перенести мод в {}", mod_root.display())));
    }

    let descriptor = mod_root.join("descriptor.mod");
    let mod_file = options.output_dir.join(format!("{}.mod", options.folder));
    atomic_write_bytes(&mod_file, render_descriptor(options, true).as_bytes())
        .with_context(|| format!("не удалось записать {}", mod_file.display()))?;

    let installed = if options.install {
        Some(install_pack(&mod_root, &mod_file, options, log)?)
    } else {
        None
    };

    Ok(ModPackResult {
        folder: mod_root,
        mod_file,
        descriptor,
        files: copied,
        installed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "hoi4-pack-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).expect("temp dir");
        path
    }

    fn write_translation(root: &Path, language: &str, name: &str, header: &str, body: &str) {
        let directory = root.join("localisation").join(language);
        fs::create_dir_all(&directory).expect("dirs");
        let mut bytes = vec![0xEF, 0xBB, 0xBF];
        bytes.extend_from_slice(format!("{header}\n{body}\n").as_bytes());
        fs::write(directory.join(name), bytes).expect("write");
    }

    fn options(source_dir: PathBuf, output_dir: PathBuf) -> ModPackOptions {
        ModPackOptions {
            display_name: "MyMod — русский перевод".to_string(),
            folder: "MyMod_ru".to_string(),
            version: DEFAULT_MOD_VERSION.to_string(),
            supported_version: DEFAULT_SUPPORTED_VERSION.to_string(),
            dependency: Some("MyMod".to_string()),
            tags: vec![DEFAULT_TAG.to_string()],
            target_lang_code: "ru".to_string(),
            source_dir,
            output_dir,
            install: false,
            install_dir: None,
        }
    }

    #[test]
    fn folder_names_become_launcher_safe() {
        assert_eq!(mod_folder_name("MyMod — русский перевод", "ru"), "MyMod_ru");
        assert_eq!(mod_folder_name("  my mod  ", "ru"), "my_mod_ru");
        assert_eq!(mod_folder_name("Перевод", "zh-CN"), "translation_zh_cn");
        // Already marked: no second suffix.
        assert_eq!(mod_folder_name("MyMod_ru", "ru"), "MyMod_ru");
        assert_eq!(mod_folder_name("MyMod RU", "ru"), "MyMod_RU");
        let name = mod_folder_name("Проверка кириллицы", "ru");
        assert!(name.is_ascii(), "{name}");
        assert!(!name.contains(' '), "{name}");
        assert!(name.len() <= MAX_FOLDER_LEN, "{name}");
        assert!(validate_folder_name(&name).is_ok());
        assert!(validate_folder_name("with space").is_err());
        assert!(validate_folder_name("Кириллица").is_err());
        assert!(validate_folder_name(".hidden").is_err());
        assert!(validate_folder_name("localisation").is_err());
        assert!(validate_folder_name("").is_err());
    }

    #[test]
    fn an_empty_launcher_name_is_refused_before_anything_is_built() {
        let mut options = options(PathBuf::from("."), PathBuf::from("."));
        options.display_name = "   ".to_string();
        assert!(preflight(&options).is_err());
        options.display_name = "MyMod".to_string();
        options.version = String::new();
        assert!(preflight(&options).is_err());
    }

    #[test]
    fn descriptor_and_mod_file_differ_only_by_path() {
        let root = temp_dir("descriptor");
        write_translation(&root, "russian", "a_l_russian.yml", "l_russian:", " A:0 \"А\"");
        let result = build(&options(root.clone(), root.clone()), &mut |_, _| {}).expect("built");

        let descriptor = fs::read_to_string(&result.descriptor).expect("descriptor");
        let mod_file = fs::read_to_string(&result.mod_file).expect("mod file");
        assert!(descriptor.starts_with('\u{feff}'), "descriptor needs a BOM");
        assert!(mod_file.starts_with('\u{feff}'), ".mod needs a BOM");
        assert!(descriptor.contains("name=\"MyMod — русский перевод\""));
        assert!(descriptor.contains("dependencies={\n\t\"MyMod\"\n}"));
        assert!(descriptor.contains("supported_version=\"1.*\""));
        assert!(!descriptor.contains("path="), "descriptor must not carry path");
        assert!(mod_file.contains("path=\"mod/MyMod_ru\""));

        let expected = format!(
            "{}\npath=\"mod/MyMod_ru\"\n",
            descriptor.trim_end_matches('\n')
        );
        assert_eq!(mod_file, expected, ".mod must be the descriptor plus path");
        assert_eq!(result.files, 1);
        assert!(result.installed.is_none());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn translation_files_are_copied_with_their_language_folder() {
        let root = temp_dir("copy");
        write_translation(&root, "russian", "a_l_russian.yml", "l_russian:", " A:0 \"А\"");
        write_translation(&root, "english", "a_l_english.yml", "l_english:", " A:0 \"A\"");
        let result = build(&options(root.clone(), root.clone()), &mut |_, _| {}).expect("built");

        let russian = result
            .folder
            .join("localisation")
            .join("russian")
            .join("a_l_russian.yml");
        assert!(russian.exists(), "{russian:?}");
        let english = result
            .folder
            .join("localisation")
            .join("english")
            .join("a_l_english.yml");
        assert!(!english.exists(), "another language must not be packaged");
        assert_eq!(result.files, 1);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_language_folder_as_destination_is_packaged_too() {
        // `--dst OUT/localisation/russian` puts the files at the top level, and
        // that run must still pack: the folder layout is rebuilt here.
        let root = temp_dir("flat");
        fs::create_dir_all(&root).expect("dirs");
        let mut bytes = vec![0xEF, 0xBB, 0xBF];
        // A normal string, not a byte string: byte literals must stay ASCII.
        bytes.extend_from_slice("l_russian:\n A:0 \"А\"\n".as_bytes());
        fs::write(root.join("a_l_russian.yml"), bytes).expect("write");

        let result = build(&options(root.clone(), root.clone()), &mut |_, _| {}).expect("built");
        let packed = result
            .folder
            .join("localisation")
            .join("russian")
            .join("a_l_russian.yml");
        assert!(packed.exists(), "{packed:?}");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_file_without_a_bom_or_the_right_header_is_refused() {
        let root = temp_dir("invalid");
        write_translation(&root, "russian", "a_l_russian.yml", "l_russian:", " A:0 \"А\"");
        let bad = root
            .join("localisation")
            .join("russian")
            .join("b_l_russian.yml");
        fs::write(&bad, "l_russian:\n B:0 \"Б\"\n").expect("write");
        let error = build(&options(root.clone(), root.clone()), &mut |_, _| {})
            .expect_err("a file without a BOM must be refused");
        assert!(error.to_string().contains("BOM"), "{error}");

        fs::write(
            &bad,
            [&[0xEF, 0xBB, 0xBF][..], b"l_english:\n B:0 \"B\"\n"].concat(),
        )
        .expect("write");
        let error = build(&options(root.clone(), root.clone()), &mut |_, _| {})
            .expect_err("a wrong header must be refused");
        assert!(error.to_string().contains("l_russian"), "{error}");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn an_empty_translation_folder_is_refused() {
        let root = temp_dir("empty");
        fs::create_dir_all(root.join("localisation")).expect("dirs");
        let error = build(&options(root.clone(), root.clone()), &mut |_, _| {})
            .expect_err("nothing to package");
        assert!(error.to_string().contains("_l_russian.yml"), "{error}");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn the_target_descriptor_supplies_the_dependency() {
        let root = temp_dir("target");
        let descriptor = root.join("descriptor.mod");
        fs::write(
            &descriptor,
            "\u{feff}name=\"MyMod #1\"\nversion=\"2.1\"\nsupported_version=\"1.15.*\"\
             \ntags={\n\t\"Gameplay\"\n}\n# trailing comment\n",
        )
        .expect("write");

        let target = read_target_descriptor(&descriptor).expect("parsed");
        // A hash inside quotes belongs to the name, not to a comment.
        assert_eq!(target.name.as_deref(), Some("MyMod #1"));
        assert_eq!(target.version.as_deref(), Some("2.1"));
        assert_eq!(target.supported_version.as_deref(), Some("1.15.*"));

        // A folder works as well as the file itself.
        let from_folder = read_target_descriptor(&root).expect("parsed from folder");
        assert_eq!(from_folder.name.as_deref(), Some("MyMod #1"));
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn the_translation_folder_cannot_be_named_like_the_mod_folder() {
        let root = temp_dir("clobber");
        write_translation(&root, "russian", "a_l_russian.yml", "l_russian:", " A:0 \"А\"");
        let mut broken = options(root.clone(), root.clone());
        broken.folder = "localisation".to_string();
        let error = build(&broken, &mut |_, _| {}).expect_err("must not overwrite the run");
        assert!(error.to_string().contains("localisation"), "{error}");
        // The translation is still there.
        assert!(root
            .join("localisation")
            .join("russian")
            .join("a_l_russian.yml")
            .exists());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn rebuilding_replaces_our_own_version_and_refuses_a_foreign_one() {
        let root = temp_dir("rebuild");
        write_translation(&root, "russian", "a_l_russian.yml", "l_russian:", " A:0 \"А\"");
        build(&options(root.clone(), root.clone()), &mut |_, _| {}).expect("first build");
        build(&options(root.clone(), root.clone()), &mut |_, _| {}).expect("second build");

        // The same folder name, a different mod: it must survive untouched.
        let mut foreign = options(root.clone(), root.clone());
        foreign.display_name = "Совсем другой мод".to_string();
        let error = build(&foreign, &mut |_, _| {}).expect_err("foreign mod must be protected");
        assert!(error.to_string().contains("другой мод"), "{error}");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn install_copies_the_mod_and_its_descriptor_into_the_mods_folder() {
        let root = temp_dir("install");
        write_translation(&root, "russian", "a_l_russian.yml", "l_russian:", " A:0 \"А\"");
        let mods = root.join("mods");
        let mut options = options(root.clone(), root.clone());
        options.install = true;
        options.install_dir = Some(mods.clone());

        let result = build(&options, &mut |_, _| {}).expect("installed");
        let installed = result.installed.expect("installed path");
        assert!(installed.join("descriptor.mod").exists(), "{installed:?}");
        assert!(installed
            .join("localisation")
            .join("russian")
            .join("a_l_russian.yml")
            .exists());
        let mod_file = mods.join("MyMod_ru.mod");
        assert!(mod_file.exists(), "{mod_file:?}");
        assert!(fs::read_to_string(&mod_file)
            .expect("mod file")
            .contains("path=\"mod/MyMod_ru\""));

        // A second run replaces our own folder without leftovers.
        build(&options, &mut |_, _| {}).expect("second install");
        let leftovers: Vec<String> = fs::read_dir(&mods)
            .expect("read mods")
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().to_string())
            .filter(|name| name.starts_with('.'))
            .collect();
        assert!(leftovers.is_empty(), "scratch folders left: {leftovers:?}");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn install_refuses_to_replace_a_mod_it_did_not_build() {
        let root = temp_dir("install-foreign");
        write_translation(&root, "russian", "a_l_russian.yml", "l_russian:", " A:0 \"А\"");
        let mods = root.join("mods");
        let stolen = mods.join("MyMod_ru");
        fs::create_dir_all(&stolen).expect("dirs");
        fs::write(
            stolen.join("descriptor.mod"),
            "name=\"Настоящий мод\"\nversion=\"1.0\"\n",
        )
        .expect("write");

        let mut options = options(root.clone(), root.clone());
        options.install = true;
        options.install_dir = Some(mods.clone());
        let error = build(&options, &mut |_, _| {}).expect_err("must refuse");
        assert!(error.to_string().contains("другой мод"), "{error}");
        assert!(stolen.join("descriptor.mod").exists(), "must be untouched");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn the_install_path_is_the_game_mod_folder() {
        let Some(path) = paradox_mods_dir() else {
            return;
        };
        assert!(path.ends_with(Path::new("mod")), "{path:?}");
        assert!(
            path.to_string_lossy()
                .contains("Paradox Interactive/Hearts of Iron IV")
                || path
                    .to_string_lossy()
                    .contains("Paradox Interactive\\Hearts of Iron IV"),
            "{path:?}"
        );
    }
}
