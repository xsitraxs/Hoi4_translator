//! Command line interface: translate a folder of `.yml` files without a window.
//!
//! Exit codes: 0 — everything written, 1 — at least one file failed, 2 — the
//! command line or the folders are wrong (nothing was started).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;

use clap::{ArgAction, Parser};

use crate::config::{self, LogTag};
use crate::glossary::Glossary;
use crate::inspect::{format_report, inspect_files};
use crate::localisation::{
    collect_yml_files, is_supported_target, rename_dst, validate_directory_relationships,
    validate_output_path_collisions, HOI4_TARGET_LANGS,
};
use crate::modpack;
use crate::pipeline::{process_file, BrokenLineMode, FileResult};
use crate::providers::ENGINES;
use crate::report::RunReport;
use crate::translation::Translator;
use crate::vanilla::build_vanilla_index;

#[derive(Parser, Debug)]
#[command(
    name = "hoi4-translator",
    version,
    about = "Перевод локализации модов Hearts of Iron IV пачкой",
    long_about = "Проходит по .yml-файлам локализации, переводит только значения \
                  (ключи остаются как есть) и не ломает разметку Paradox. Перед записью \
                  каждый файл проверяется.",
    after_help = "Пример:\n  hoi4-translator --src MOD --dst OUT --target ru\n\
                  \nПроверить файлы, ничего не переводя:\n  hoi4-translator --src MOD \
                  --dst OUT --dry-run"
)]
pub struct Cli {
    /// Папка мода или localisation с исходниками
    #[arg(long, value_name = "DIR")]
    pub src: PathBuf,

    /// Куда писать перевод (отдельный каталог, не внутри исходников)
    #[arg(long, value_name = "DIR")]
    pub dst: PathBuf,

    /// Язык перевода (ru, en, de, ...)
    #[arg(long, default_value = "ru")]
    pub target: String,

    /// Язык исходников (по умолчанию en)
    #[arg(long = "source-lang", default_value = "en")]
    pub source_lang: String,

    /// Движок перевода: Google, DeepL или MyMemory
    #[arg(long, default_value = "Google")]
    pub engine: String,

    /// Ключ DeepL (лучше через переменную DEEPL_API_KEY: её не видно в списке процессов)
    #[arg(long = "api-key")]
    pub api_key: Option<String>,

    /// Устарело: движок JavaSim заменён встроенным HTTP-движком
    #[arg(long = "java-jar", hide = true)]
    pub java_jar: Option<PathBuf>,

    /// Файл глоссария (TSV/CSV/JSON)
    #[arg(long)]
    pub glossary: Option<PathBuf>,

    /// Папка localisation установленной игры: готовые официальные переводы
    /// переиспользуются вместо машинного перевода
    #[arg(long)]
    pub vanilla: Option<PathBuf>,

    /// Параллельных запросов
    #[arg(long, default_value_t = config::SEMAPHORE_LIMIT)]
    pub concurrency: usize,

    /// Не переводить строки, уже переведённые в папке результата
    #[arg(long = "skip-translated", action = ArgAction::SetTrue)]
    pub skip_translated: bool,

    /// Что делать со строкой, сломавшей формат локализации: reject или keep-original
    #[arg(long = "broken-lines", default_value = "reject")]
    pub broken_lines: String,

    /// Не создавать JSON/CSV отчёт
    #[arg(long = "no-report", action = ArgAction::SetTrue)]
    pub no_report: bool,

    /// Собрать готовый мод: имя для лаунчера (включает сборку)
    #[arg(long = "make-mod", value_name = "NAME")]
    pub make_mod: Option<String>,

    /// Имя папки и .mod-файла (латиница без пробелов); по умолчанию из --make-mod
    #[arg(long = "mod-folder", value_name = "NAME")]
    pub mod_folder: Option<String>,

    /// Версия мода перевода
    #[arg(long = "mod-version", value_name = "X.Y.Z", default_value = "1.0.0")]
    pub mod_version: String,

    /// supported_version мода; по умолчанию из --mod-target или 1.*
    #[arg(long = "mod-supported-version", value_name = "X.Y.*")]
    pub mod_supported_version: Option<String>,

    /// descriptor.mod мода-цели: оттуда берутся dependencies и supported_version
    #[arg(long = "mod-target", value_name = "PATH")]
    pub mod_target: Option<PathBuf>,

    /// Скопировать готовый мод в папку модов игры
    #[arg(long, action = ArgAction::SetTrue)]
    pub install: bool,

    /// Только проверить файлы: разобрать, найти битые строки и дубликаты ключей
    #[arg(long = "dry-run", action = ArgAction::SetTrue)]
    pub dry_run: bool,

    /// Только итог, без строк файлов
    #[arg(long, action = ArgAction::SetTrue)]
    pub quiet: bool,

    /// Устарело: CLI теперь запускается напрямую, флаг принимается для совместимости
    #[arg(long, hide = true, action = ArgAction::SetTrue)]
    pub cli: bool,
}

fn fail(message: &str) -> i32 {
    eprintln!("{message}");
    2
}

fn relative_display(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .display()
        .to_string()
}

/// Run a preflight check that has to happen before any network traffic.
fn preflight_engine(engine: &str, api_key: Option<&str>) -> Result<(), String> {
    if !ENGINES.contains(&engine) {
        return Err(format!(
            "Неизвестный движок перевода: {engine}. Доступны: {}",
            ENGINES.join(", ")
        ));
    }
    if engine == "DeepL" && api_key.unwrap_or("").is_empty() {
        return Err(
            "Для движка DeepL нужен ключ: --api-key или переменная окружения DEEPL_API_KEY. \
             Без него DeepL молча уступил бы Google."
                .to_string(),
        );
    }
    Ok(())
}

/// Entry point used by `main`; returns the process exit code.
pub fn run(cli: Cli) -> i32 {
    if !is_supported_target(&cli.target) {
        let available: Vec<&str> = HOI4_TARGET_LANGS.iter().map(|(code, _)| *code).collect();
        return fail(&format!(
            "Неизвестный язык перевода: {}. Доступны: {}",
            cli.target,
            available.join(", ")
        ));
    }
    if !cli.src.is_dir() {
        return fail(&format!(
            "Папка исходников не найдена: {}",
            cli.src.display()
        ));
    }
    if let Err(error) = validate_directory_relationships(&cli.src, &cli.dst) {
        return fail(&error.to_string());
    }
    if cli.concurrency == 0 {
        return fail("Параллельных запросов должно быть не меньше одного: --concurrency N (N ≥ 1)");
    }
    if cli.install && cli.make_mod.is_none() {
        return fail("--install без --make-mod \"Имя мода\": нечего устанавливать");
    }
    if cli.make_mod.is_some() && cli.dry_run {
        return fail("--make-mod нельзя совместить с --dry-run: пробный прогон ничего не пишет");
    }
    let target_mod = match cli.mod_target.as_deref() {
        Some(path) => match modpack::read_target_descriptor(path) {
            Ok(target) => Some(target),
            Err(error) => return fail(&error.to_string()),
        },
        None => None,
    };

    let files = collect_yml_files(&cli.src);
    if files.is_empty() {
        return fail(&format!("В папке нет файлов .yml: {}", cli.src.display()));
    }

    let mut pairs: Vec<(PathBuf, PathBuf)> = Vec::with_capacity(files.len());
    for source in &files {
        match rename_dst(source, &cli.src, &cli.dst, &cli.target) {
            Ok(destination) => pairs.push((source.clone(), destination)),
            Err(error) => return fail(&error.to_string()),
        }
    }
    if let Err(error) = validate_output_path_collisions(&pairs) {
        return fail(&error.to_string());
    }
    let destinations: HashMap<PathBuf, PathBuf> = pairs.iter().cloned().collect();

    let glossary: Option<Glossary> = match cli.glossary.as_deref() {
        Some(path) => match Glossary::load(path) {
            Ok(glossary) => Some(glossary),
            Err(error) => return fail(&error.to_string()),
        },
        None => None,
    };

    let mode = match cli.broken_lines.as_str() {
        "reject" => BrokenLineMode::RejectFile,
        "keep-original" => BrokenLineMode::KeepOriginal,
        other => {
            return fail(&format!(
                "Неизвестный режим --broken-lines: {other}. Доступны: reject, keep-original"
            ))
        }
    };

    if cli.dry_run {
        let inspections = inspect_files(&files, Some(&destinations));
        for line in format_report(&inspections, &cli.src) {
            println!("{line}");
        }
        let blocking = inspections
            .iter()
            .any(|inspection| inspection.has_blocking_problems());
        return if blocking { 1 } else { 0 };
    }

    let quiet = cli.quiet;
    let mut log_callback = move |message: &str, tag: LogTag| {
        // The log file always gets the message; the console decides whether to
        // show it. Filtering on the severity, not on the text: a write failure
        // must never be hidden by `--quiet` because its wording changed.
        config::log_line(tag, message);
        if quiet && tag != LogTag::Error {
            return;
        }
        let prefix = match tag {
            LogTag::Error => "! ",
            LogTag::Warning => "~ ",
            LogTag::Info => "  ",
        };
        println!("{prefix}{message}");
    };

    let vanilla = match build_vanilla_index(
        cli.vanilla.as_deref(),
        &cli.source_lang,
        &cli.target,
        &mut log_callback,
    ) {
        Ok(index) => index,
        Err(error) => return fail(&error.to_string()),
    };

    let engine = if cli.engine == "JavaSim" {
        // The JAR-based engine is gone: the Rust build talks to the same HTTP
        // endpoint directly, so an old script keeps working.
        eprintln!(
            "Движок JavaSim заменён встроенным HTTP-движком Google: отдельный JAR больше не нужен."
        );
        "Google".to_string()
    } else {
        cli.engine.clone()
    };

    let api_key = cli
        .api_key
        .clone()
        .or_else(|| std::env::var("DEEPL_API_KEY").ok())
        .filter(|key| !key.is_empty());

    if let Err(error) = preflight_engine(&engine, api_key.as_deref()) {
        return fail(&error);
    }
    if cli.java_jar.is_some() {
        eprintln!("--java-jar больше не используется: движок работает без Java.");
    }

    println!(
        "Hoi4 Translator {} | {engine} | {} → {} | файлов: {}",
        config::VERSION,
        cli.source_lang,
        cli.target,
        files.len()
    );
    if let Some(glossary) = &glossary {
        println!(
            "Глоссарий: {}, терминов: {}",
            glossary.origin,
            glossary.len()
        );
    }
    if let Some(vanilla) = &vanilla {
        println!("Ванильных переводов в индексе: {}", vanilla.len());
    }

    let translator = match Translator::new(
        &engine,
        api_key.clone(),
        &cli.source_lang,
        cli.concurrency,
        glossary,
    ) {
        Ok(translator) => translator,
        Err(error) => return fail(&format!("Не удалось открыть кэш переводов: {error}")),
    };

    let mut report = if cli.no_report {
        None
    } else {
        Some(RunReport::new(
            &engine,
            &cli.source_lang,
            &cli.target,
            &cli.src,
            &cli.dst,
        ))
    };

    let stop = AtomicBool::new(false);
    let mut failures = 0usize;
    let mut written = 0usize;

    for (index, source) in files.iter().enumerate() {
        let relative = relative_display(source, &cli.src);
        println!("[{}/{}] {relative}", index + 1, files.len());

        let Some(destination) = destinations.get(source) else {
            failures += 1;
            continue;
        };

        let outcome = process_file(
            source,
            destination,
            &translator,
            &mut log_callback,
            cli.skip_translated,
            &stop,
            &cli.target,
            None,
            mode,
            vanilla.as_ref(),
        );

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

        match outcome.result {
            Some(FileResult::Written(_)) => written += 1,
            Some(FileResult::PreviewCancelled) => {}
            _ => failures += 1,
        }
    }

    let fallbacks = translator.fallback_counts();
    if let Some(report) = report.as_mut() {
        report.set_fallbacks(fallbacks.clone());
        for path in report.write(&cli.dst) {
            println!("Отчёт: {}", path.display());
        }
    }
    for (pair, count) in &fallbacks {
        println!("Фолбэк {pair}: {count} раз");
    }
    if !cli.quiet {
        println!("{}", translator.validation_summary());
    }

    let mut pack_failed = false;
    if let Some(display_name) = cli.make_mod.clone() {
        let folder = cli
            .mod_folder
            .clone()
            .unwrap_or_else(|| modpack::mod_folder_name(&display_name, &cli.target));
        let supported_version = cli
            .mod_supported_version
            .clone()
            .or_else(|| {
                target_mod
                    .as_ref()
                    .and_then(|target| target.supported_version.clone())
            })
            .unwrap_or_else(|| modpack::DEFAULT_SUPPORTED_VERSION.to_string());
        let dependency = target_mod
            .as_ref()
            .and_then(|target| target.name.clone());
        let options = modpack::ModPackOptions {
            display_name: display_name.clone(),
            folder: folder.clone(),
            version: cli.mod_version.clone(),
            supported_version,
            dependency: dependency.clone(),
            tags: vec![modpack::DEFAULT_TAG.to_string()],
            target_lang_code: cli.target.clone(),
            source_dir: cli.dst.clone(),
            output_dir: cli.dst.clone(),
            install: cli.install,
        };
        match modpack::build(&options, &mut log_callback) {
            Ok(result) => {
                println!(
                    "Мод собран: {} (файлов перевода: {})",
                    result.folder.display(),
                    result.files
                );
                println!("Описание для лаунчера: {}", result.mod_file.display());
                if let Some(installed) = &result.installed {
                    println!("Установлен в папку игры: {}", installed.display());
                } else {
                    println!(
                        "Скопируйте {} и {} в папку модов игры (или добавьте --install)",
                        result.folder.display(),
                        result.mod_file
                            .file_name()
                            .map(|name| name.to_string_lossy().to_string())
                            .unwrap_or_default()
                    );
                }
                match &dependency {
                    Some(dependency) => println!("Зависимость (мод-цель): {dependency}"),
                    None => println!(
                        "Мод-цель не указан (--mod-target): перевод ляжет поверх ванили"
                    ),
                }
            }
            Err(error) => {
                log_callback(
                    &format!("  Не удалось собрать мод: {error}"),
                    LogTag::Error,
                );
                pack_failed = true;
            }
        }
    }

    println!("Готово: файлов записано {written}, ошибок {failures}");
    if failures == 0 && !pack_failed {
        0
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    #[test]
    fn the_command_line_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn required_folders_are_enforced() {
        assert!(Cli::try_parse_from(["hoi4-translator"]).is_err());
        let parsed = Cli::try_parse_from([
            "hoi4-translator",
            "--src",
            "mod",
            "--dst",
            "out",
            "--target",
            "ru",
        ])
        .expect("parsed");
        assert_eq!(parsed.target, "ru");
        assert_eq!(parsed.engine, "Google");
        assert!(!parsed.dry_run);
    }

    #[test]
    fn the_engine_preflight_rejects_impossible_runs() {
        assert!(preflight_engine("DeepL", None).is_err());
        assert!(preflight_engine("DeepL", Some("key")).is_ok());
        assert!(preflight_engine("Bing", Some("key")).is_err());
        assert!(preflight_engine("Google", None).is_ok());
    }

    fn pack_cli(extra: &[&str]) -> Cli {
        // A destination outside the source tree, so the folder checks pass and
        // the packing rules themselves are what the test exercises.
        let destination = std::env::temp_dir()
            .join("hoi4-translator-pack-test")
            .to_string_lossy()
            .to_string();
        let mut args = vec![
            "hoi4-translator",
            "--src",
            env!("CARGO_MANIFEST_DIR"),
            "--dst",
            destination.as_str(),
        ];
        args.extend_from_slice(extra);
        Cli::try_parse_from(args).expect("parsed")
    }

    #[test]
    fn packing_flags_are_checked_before_anything_runs() {
        // --install on its own has nothing to install.
        assert_eq!(run(pack_cli(&["--install"])), 2);
        // A dry run writes nothing, so it cannot build a mod either.
        assert_eq!(run(pack_cli(&["--dry-run", "--make-mod", "MyMod ru"])), 2);
        // A descriptor that does not exist is a usage error, not a late surprise.
        assert_eq!(
            run(pack_cli(&[
                "--make-mod",
                "MyMod ru",
                "--mod-target",
                "/definitely/missing/descriptor.mod",
            ])),
            2
        );
    }

    #[test]
    fn the_pack_options_come_from_the_command_line() {
        let cli = pack_cli(&[
            "--make-mod",
            "MyMod — русский",
            "--mod-version",
            "2.0.0",
            "--mod-supported-version",
            "1.15.*",
        ]);
        assert_eq!(cli.make_mod.as_deref(), Some("MyMod — русский"));
        assert_eq!(cli.mod_version, "2.0.0");
        assert_eq!(cli.mod_supported_version.as_deref(), Some("1.15.*"));
        assert_eq!(
            modpack::mod_folder_name(
                cli.make_mod.as_deref().unwrap_or_default(),
                &cli.target
            ),
            "MyMod"
        );
        assert!(!cli.install);
    }

    #[test]
    fn an_unknown_target_language_is_a_usage_error() {
        let cli = Cli::try_parse_from([
            "hoi4-translator",
            "--src",
            "/definitely/missing",
            "--dst",
            "/tmp/out",
            "--target",
            "it",
        ])
        .expect("parsed");
        assert_eq!(run(cli), 2);
    }
}
