//! Building a launcher-ready translation mod out of a finished run.
//!
//! The pipeline writes `localisation/<language>/*.yml`; the game launcher also
//! wants a folder in `mod/` with a `descriptor.mod` inside and a sibling
//! `<name>.mod` that points at it. Getting those three pieces to agree by hand
//! is where translation mods usually break (a space in the file name, `path` in
//! the wrong file, a descriptor that disagrees with the `.mod`), so the tool
//! builds them instead.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

use crate::config::{atomic_write_bytes, LogTag};
use crate::localisation::{collect_yml_files, get_hoi4_language_name};

/// Version written into the descriptor when the caller does not care.
pub const DEFAULT_MOD_VERSION: &str = "1.0.0";
/// Game version the mod claims to support when the target mod says nothing.
pub const DEFAULT_SUPPORTED_VERSION: &str = "1.*";
/// Launcher tag a translation normally carries.
pub const DEFAULT_TAG: &str = "Translation";

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

/// A folder name the launcher accepts: ASCII letters, digits, `_`, `-`, `.`.
///
/// Anything else becomes `_` and repeated separators collapse, because the
/// launcher silently ignores a mod whose path contains a space or a non-ASCII
/// character — the user would see "the mod is not there" with no explanation.
pub fn mod_folder_name(display_name: &str, target_lang_code: &str) -> String {
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
    let mut trimmed = name.trim_matches('_').to_string();
    if trimmed.len() > 48 {
        trimmed.truncate(48);
        trimmed = trimmed.trim_end_matches('_').to_string();
    }
    if trimmed.is_empty() {
        let language = target_lang_code.to_lowercase().replace('-', "_");
        return format!("translation_{language}");
    }
    trimmed
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

fn clean_field(value: &str) -> String {
    value.replace('"', "'").replace(['\n', '\r'], " ")
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

/// Read `descriptor.mod` of the mod being translated.
///
/// Accepts the descriptor itself or the folder that contains it. Only the three
/// keys a translation needs are read: a full mod descriptor carries much more,
/// and guessing at it is not this tool's job.
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
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim().to_lowercase();
        let value = value
            .split('#')
            .next()
            .unwrap_or("")
            .trim()
            .trim_matches('"')
            .trim()
            .to_string();
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

/// Translation files of one language, checked for what the game needs.
///
/// A mod with one non-BOM file or one wrong header loads half-translated, and
/// the reason is invisible in game, so the build refuses it instead.
pub fn collect_translation_files(localisation_root: &Path, database: &str) -> Result<Vec<PathBuf>> {
    let suffix = format!("_l_{}.yml", database.to_lowercase());
    let header = format!("l_{database}:");
    let mut selected: Vec<PathBuf> = Vec::new();
    for path in collect_yml_files(localisation_root) {
        let name = path
            .file_name()
            .map(|name| name.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if !name.ends_with(&suffix) {
            continue;
        }
        selected.push(path);
    }
    if selected.is_empty() {
        bail!(
            "В папке {} нет файлов {suffix}: сначала переведите мод в эту папку",
            localisation_root.display()
        );
    }

    for path in &selected {
        let bytes =
            fs::read(path).with_context(|| format!("не удалось прочитать {}", path.display()))?;
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
    }
    selected.sort();
    Ok(selected)
}

/// `Documents/Paradox Interactive/Hearts of Iron IV/mod` for the current user.
pub fn paradox_mods_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)?;
    let mut path = if cfg!(windows) || cfg!(target_os = "macos") {
        home.join("Documents")
    } else {
        home.join(".local").join("share")
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
        if path.is_dir() {
            copy_tree(&path, &target)?;
        } else {
            fs::copy(&path, &target).with_context(|| {
                format!(
                    "не удалось скопировать {} в {}",
                    path.display(),
                    target.display()
                )
            })?;
        }
    }
    Ok(())
}

fn install_pack(
    mod_root: &Path,
    mod_file: &Path,
    options: &ModPackOptions,
    log: &mut dyn FnMut(&str, LogTag),
) -> Result<PathBuf> {
    let Some(mods_dir) = paradox_mods_dir() else {
        bail!("не удалось найти папку пользователя (HOME/USERPROFILE), чтобы установить мод");
    };
    fs::create_dir_all(&mods_dir)
        .with_context(|| format!("не удалось создать {}", mods_dir.display()))?;

    let target_folder = mods_dir.join(&options.folder);
    if target_folder.exists() {
        log(
            &format!(
                "  Мод {} уже был в папке игры: перезаписываю",
                options.folder
            ),
            LogTag::Warning,
        );
        fs::remove_dir_all(&target_folder).with_context(|| {
            format!(
                "не удалось удалить старую версию {}",
                target_folder.display()
            )
        })?;
    }
    copy_tree(mod_root, &target_folder)?;

    let target_mod_file = mods_dir.join(format!("{}.mod", options.folder));
    fs::copy(mod_file, &target_mod_file).with_context(|| {
        format!(
            "не удалось скопировать {} в {}",
            mod_file.display(),
            target_mod_file.display()
        )
    })?;
    Ok(target_folder)
}

/// Build the mod folder next to the translation and, optionally, install it.
pub fn build(options: &ModPackOptions, log: &mut dyn FnMut(&str, LogTag)) -> Result<ModPackResult> {
    validate_folder_name(&options.folder)?;
    let database = get_hoi4_language_name(&options.target_lang_code)?;

    let localisation_root = options.source_dir.join("localisation");
    if !localisation_root.is_dir() {
        bail!(
            "Нет папки {}: сначала выполните перевод в {}",
            localisation_root.display(),
            options.source_dir.display()
        );
    }
    let files = collect_translation_files(&localisation_root, database)?;

    let mod_root = options.output_dir.join(&options.folder);
    if mod_root.exists() {
        fs::remove_dir_all(&mod_root)
            .with_context(|| format!("не удалось очистить {}", mod_root.display()))?;
    }
    fs::create_dir_all(&mod_root)
        .with_context(|| format!("не удалось создать {}", mod_root.display()))?;

    let mut copied = 0usize;
    for path in &files {
        let relative = path.strip_prefix(&localisation_root).unwrap_or(path);
        let destination = mod_root.join("localisation").join(relative);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("не удалось создать {}", parent.display()))?;
        }
        fs::copy(path, &destination).with_context(|| {
            format!(
                "не удалось скопировать {} в {}",
                path.display(),
                destination.display()
            )
        })?;
        copied += 1;
    }

    let descriptor = mod_root.join("descriptor.mod");
    atomic_write_bytes(&descriptor, render_descriptor(options, false).as_bytes())
        .with_context(|| format!("не удалось записать {}", descriptor.display()))?;

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
        }
    }

    #[test]
    fn folder_names_become_launcher_safe() {
        assert_eq!(mod_folder_name("MyMod — русский перевод", "ru"), "MyMod");
        assert_eq!(mod_folder_name("  my mod  ", "ru"), "my_mod");
        assert_eq!(mod_folder_name("Перевод", "zh-CN"), "translation_zh_cn");
        let name = mod_folder_name("Проверка кириллицы", "ru");
        assert!(name.is_ascii(), "{name}");
        assert!(!name.contains(' '), "{name}");
        assert!(validate_folder_name(&name).is_ok());
        assert!(validate_folder_name("with space").is_err());
        assert!(validate_folder_name("Кириллица").is_err());
        assert!(validate_folder_name(".hidden").is_err());
        assert!(validate_folder_name("").is_err());
    }

    #[test]
    fn descriptor_and_mod_file_differ_only_by_path() {
        let root = temp_dir("descriptor");
        write_translation(
            &root,
            "russian",
            "a_l_russian.yml",
            "l_russian:",
            " A:0 \"А\"",
        );
        let result = build(&options(root.clone(), root.clone()), &mut |_, _| {}).expect("built");

        let descriptor = fs::read_to_string(&result.descriptor).expect("descriptor");
        let mod_file = fs::read_to_string(&result.mod_file).expect("mod file");
        assert!(descriptor.starts_with('\u{feff}'), "descriptor needs a BOM");
        assert!(mod_file.starts_with('\u{feff}'), ".mod needs a BOM");
        assert!(descriptor.contains("name=\"MyMod — русский перевод\""));
        assert!(descriptor.contains("dependencies={\n\t\"MyMod\"\n}"));
        assert!(descriptor.contains("supported_version=\"1.*\""));
        assert!(
            !descriptor.contains("path="),
            "descriptor must not carry path"
        );
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
        write_translation(
            &root,
            "russian",
            "a_l_russian.yml",
            "l_russian:",
            " A:0 \"А\"",
        );
        write_translation(
            &root,
            "english",
            "a_l_english.yml",
            "l_english:",
            " A:0 \"A\"",
        );
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
    fn a_file_without_a_bom_or_the_right_header_is_refused() {
        let root = temp_dir("invalid");
        write_translation(
            &root,
            "russian",
            "a_l_russian.yml",
            "l_russian:",
            " A:0 \"А\"",
        );
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
            "\u{feff}name=\"MyMod\"\nversion=\"2.1\"\nsupported_version=\"1.15.*\"\ntags={\n\t\"Gameplay\"\n}\n",
        )
        .expect("write");

        let target = read_target_descriptor(&descriptor).expect("parsed");
        assert_eq!(target.name.as_deref(), Some("MyMod"));
        assert_eq!(target.version.as_deref(), Some("2.1"));
        assert_eq!(target.supported_version.as_deref(), Some("1.15.*"));

        // A folder works as well as the file itself.
        let from_folder = read_target_descriptor(&root).expect("parsed from folder");
        assert_eq!(from_folder.name.as_deref(), Some("MyMod"));
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
