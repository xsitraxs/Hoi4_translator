//! End-to-end behaviour of the CLI and the file pipeline.
//!
//! Every test here runs without a network: the values it translates are either
//! pinned by a glossary or are pure markup, which the pipeline resolves before an
//! engine is ever called.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use hoi4_translator::cache::TranslationCache;
use hoi4_translator::cli::{run, Cli};
use hoi4_translator::config::LogTag;
use hoi4_translator::glossary::Glossary;
use hoi4_translator::pipeline::{process_file, BrokenLineMode, FileResult};
use hoi4_translator::report::RunReport;
use hoi4_translator::translation::Translator;

static SERIAL: AtomicUsize = AtomicUsize::new(0);

fn temp_dir(tag: &str) -> PathBuf {
    let serial = SERIAL.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!("hoi4-it-{tag}-{}-{serial}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

fn write(path: &Path, content: &str) {
    std::fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
    std::fs::write(path, content).expect("write");
}

fn base_cli(src: PathBuf, dst: PathBuf) -> Cli {
    Cli {
        src,
        dst,
        target: "ru".to_string(),
        source_lang: "en".to_string(),
        engine: "Google".to_string(),
        api_key: None,
        java_jar: None,
        glossary: None,
        vanilla: None,
        concurrency: 2,
        skip_translated: false,
        broken_lines: "reject".to_string(),
        no_report: true,
        dry_run: false,
        quiet: true,
        cli: false,
        make_mod: None,
        mod_folder: None,
        mod_version: None,
        mod_supported_version: None,
        mod_target: None,
        install: false,
        install_dir: None,
        file_timeout: None,
        cache_stats: false,
        clear_cache: false,
    }
}

fn pinned_translator(glossary: Option<Glossary>) -> Translator {
    Translator::with_cache(
        "Google",
        None,
        "en",
        2,
        glossary,
        TranslationCache::in_memory().expect("cache"),
    )
}

#[test]
fn the_dry_run_reports_the_bundled_demo_mod_and_writes_nothing() {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let src = repo.join("examples").join("demo-mod");
    let workspace = temp_dir("dry-run");
    let dst = workspace.join("out");

    let mut cli = base_cli(src, dst.clone());
    cli.dry_run = true;

    assert_eq!(run(cli), 0, "a clean mod must pass the dry run");
    assert!(
        !dst.exists(),
        "the dry run must not create the output folder"
    );
    let _ = std::fs::remove_dir_all(&workspace);
}

#[test]
fn a_file_whose_values_are_pinned_by_the_glossary_is_written_without_an_engine() {
    let dir = temp_dir("glossary-run");
    let src = dir
        .join("localisation")
        .join("english")
        .join("events_l_english.yml");
    write(
        &src,
        "l_english:\n army.1:0 \"Division\"\n army.2:0 \"$STATE$\"\n",
    );
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("events_l_russian.yml");

    let glossary = Glossary::from_pairs(vec![("Division", "дивизия")], "test");
    let translator = pinned_translator(Some(glossary));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );

    assert_eq!(outcome.result, Some(FileResult::Written(2)));
    assert!(dst.exists(), "expected {}", dst.display());

    let bytes = std::fs::read(&dst).expect("read");
    assert!(
        bytes.starts_with(&[0xEF, 0xBB, 0xBF]),
        "HOI4 needs a UTF-8 BOM"
    );
    let text = String::from_utf8_lossy(&bytes).to_string();
    assert!(text.contains("l_russian:"), "{text}");
    assert!(text.contains("army.1:0 \"дивизия\""), "{text}");
    assert!(text.contains("army.2:0 \"$STATE$\""), "{text}");

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn duplicate_keys_reject_the_file_before_any_translation_happens() {
    let dir = temp_dir("duplicates");
    let src = dir
        .join("localisation")
        .join("english")
        .join("dup_l_english.yml");
    write(&src, "l_english:\n A:0 \"One\"\n A:1 \"Two\"\n");
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("dup_l_russian.yml");

    let translator = pinned_translator(None);
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );

    assert_eq!(outcome.result, Some(FileResult::Rejected));
    assert_eq!(outcome.status, "rejected");
    assert!(!dst.exists(), "a rejected file must not be written");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_second_run_skips_lines_that_are_already_translated() {
    let dir = temp_dir("skip");
    let src = dir
        .join("localisation")
        .join("english")
        .join("keep_l_english.yml");
    write(&src, "l_english:\n army.1:0 \"Division\"\n");
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("keep_l_russian.yml");

    let glossary = Glossary::from_pairs(vec![("Division", "дивизия")], "test");
    let translator = pinned_translator(Some(glossary));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let first = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );
    assert_eq!(first.result, Some(FileResult::Written(1)));

    let second = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        true,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );
    assert_eq!(second.result, Some(FileResult::Written(0)));
    assert_eq!(second.skipped, 1, "the stored translation must be reused");

    let text = std::fs::read_to_string(&dst).expect("read");
    assert!(text.contains("army.1:0 \"дивизия\""), "{text}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_edited_source_line_is_translated_again() {
    let dir = temp_dir("stale");
    let src = dir
        .join("localisation")
        .join("english")
        .join("edit_l_english.yml");
    write(&src, "l_english:\n army.1:0 \"Division\"\n");
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("edit_l_russian.yml");

    let glossary = Glossary::from_pairs(vec![("Division", "дивизия")], "test");
    let translator = pinned_translator(Some(glossary));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );

    write(&src, "l_english:\n army.1:0 \"Division!\"\n");
    let stale_glossary = Glossary::from_pairs(vec![("Division!", "дивизия!")], "test");
    let stale_translator = pinned_translator(Some(stale_glossary));
    // Share the fingerprints of the first run with the second translator.
    stale_translator
        .cache()
        .set_source_hashes(&dst, &translator.cache().get_source_hashes(&dst));

    let second = process_file(
        &src,
        &dst,
        &stale_translator,
        &mut log,
        true,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );
    assert_eq!(
        second.stale, 1,
        "an edited source line must be re-translated"
    );
    assert_eq!(second.skipped, 0);
    let text = std::fs::read_to_string(&dst).expect("read");
    assert!(text.contains("army.1:0 \"дивизия!\""), "{text}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_report_is_written_next_to_the_output() {
    let dir = temp_dir("report");
    let mut report = RunReport::new("Google", "en", "ru", Path::new("/in"), &dir);
    report.record_file(
        "a.yml",
        &dir.join("a_l_russian.yml"),
        "written",
        2,
        0,
        0,
        0,
        0,
        0,
        "",
    );

    let written = report.write(&dir);
    assert_eq!(written.len(), 2, "JSON and CSV are both written");
    for path in &written {
        assert!(path.exists(), "{}", path.display());
    }
    let json = std::fs::read_to_string(&written[0]).expect("json");
    assert!(json.contains("\"written\": 1"), "{json}");
    let _ = std::fs::remove_dir_all(&dir);
}

/// A translator whose engines answer without a network, so the write gate can be
/// exercised on answers the pipeline has to refuse.
fn answering_translator(
    answer: impl Fn(&str, &str) -> Result<String, String> + Send + Sync + 'static,
) -> Translator {
    Translator::with_cache(
        "Google",
        None,
        "en",
        2,
        None,
        TranslationCache::in_memory().expect("cache"),
    )
    .with_engine_answer(Arc::new(answer))
}

#[test]
fn a_malformed_source_line_rejects_the_whole_file() {
    let dir = temp_dir("malformed");
    let src = dir
        .join("localisation")
        .join("english")
        .join("bad_l_english.yml");
    // A non-numeric revision marker: the line has a colon, so it is a broken
    // localisation row rather than a comment or a stray line.
    write(
        &src,
        "l_english:\n army.1:0 \"One\"\n army.2:zz \"bad version\"\n",
    );
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("bad_l_russian.yml");

    let translator = pinned_translator(None);
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );

    assert_eq!(outcome.result, Some(FileResult::Rejected));
    assert_eq!(outcome.status, "rejected");
    assert!(!dst.exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_answer_that_loses_a_marker_rejects_the_file() {
    let dir = temp_dir("lost-marker");
    let src = dir
        .join("localisation")
        .join("english")
        .join("drop_l_english.yml");
    write(&src, "l_english:\n army.1:0 \"Rearm $STATE$ with tanks\"\n");
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("drop_l_russian.yml");

    // The engine drops the protected marker instead of copying it.
    let translator = answering_translator(|_text, _lang| Ok("Перевооружить танками".to_string()));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );

    assert_eq!(outcome.result, Some(FileResult::Rejected));
    assert_eq!(outcome.status, "rejected");
    assert!(
        !dst.exists(),
        "a file that lost $STATE$ must not be written"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn keep_original_writes_the_english_line_and_translates_it_again_next_time() {
    let dir = temp_dir("keep-original");
    let src = dir
        .join("localisation")
        .join("english")
        .join("keep_l_english.yml");
    let english = "Rearm $STATE$ with tanks";
    write(&src, &format!("l_english:\n army.1:0 \"{english}\"\n"));
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("keep_l_russian.yml");

    let broken = answering_translator(|_text, _lang| Ok("Перевооружить танками".to_string()));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &broken,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::KeepOriginal,
        None,
    );

    assert_eq!(outcome.result, Some(FileResult::Written(1)));
    assert_eq!(outcome.recovered, 1, "the line must be counted as repaired");
    let text = std::fs::read_to_string(&dst).expect("read");
    assert!(text.contains(&format!("army.1:0 \"{english}\"")), "{text}");

    // The stored value is English, so the next run must ask the engine again.
    let good = answering_translator(|_text, _lang| Ok("Перевооружить $STATE$ танками".to_string()));
    good.cache()
        .set_source_hashes(&dst, &broken.cache().get_source_hashes(&dst));
    let second = process_file(
        &src,
        &dst,
        &good,
        &mut log,
        true,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );
    assert_eq!(
        second.repaired, 1,
        "the repaired line must be translated again"
    );
    assert_eq!(second.skipped, 0);
    let text = std::fs::read_to_string(&dst).expect("read");
    assert!(
        text.contains("army.1:0 \"Перевооружить $STATE$ танками\""),
        "{text}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_unreachable_engine_is_an_error_not_english_output() {
    let dir = temp_dir("unreachable");
    let src = dir
        .join("localisation")
        .join("english")
        .join("down_l_english.yml");
    write(&src, "l_english:\n army.1:0 \"Rearm $STATE$ with tanks\"\n");
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("down_l_russian.yml");

    let translator = answering_translator(|_text, _lang| Err("нет сети".to_string()));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::KeepOriginal,
        None,
    );

    assert_eq!(outcome.result, Some(FileResult::Error));
    assert_eq!(outcome.status, "error");
    assert!(
        !dst.exists(),
        "an engine outage must never be written as untranslated text"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_cp1251_source_is_decoded_before_it_is_translated() {
    let dir = temp_dir("cp1251");
    let src = dir
        .join("localisation")
        .join("english")
        .join("legacy_l_english.yml");
    std::fs::create_dir_all(src.parent().expect("parent")).expect("dirs");
    // "Привет" in cp1251; UTF-8 decoding of these bytes fails, cp1251 succeeds.
    let mut bytes = b"l_english:\n army.1:0 \"".to_vec();
    bytes.extend_from_slice(&[0xCF, 0xF0, 0xE8, 0xE2, 0xE5, 0xF2]);
    bytes.extend_from_slice(b"\"\n");
    std::fs::write(&src, bytes).expect("write");
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("legacy_l_russian.yml");

    let glossary = Glossary::from_pairs(vec![("Привет", "Здравствуй")], "test");
    let translator = pinned_translator(Some(glossary));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );

    assert_eq!(outcome.result, Some(FileResult::Written(1)));
    let bytes = std::fs::read(&dst).expect("read");
    assert!(
        bytes.starts_with(&[0xEF, 0xBB, 0xBF]),
        "output must be UTF-8 BOM"
    );
    let text = String::from_utf8(bytes).expect("utf-8");
    assert!(text.contains("army.1:0 \"Здравствуй\""), "{text}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_finished_run_is_packed_into_a_launcher_ready_mod() {
    let dir = temp_dir("pack");
    let src = dir.join("mod");
    write(
        &src.join("localisation")
            .join("english")
            .join("pack_l_english.yml"),
        "l_english:\n army.1:0 \"Division\"\n",
    );
    // One pinned term, so the run never reaches an engine.
    let glossary = dir.join("glossary.tsv");
    write(&glossary, "Division\tдивизия\n");

    let mut cli = base_cli(src, dir.join("mod-ru"));
    cli.glossary = Some(glossary);
    cli.make_mod = Some("Pack Test".to_string());
    assert_eq!(run(cli), 0, "a pinned value must translate and pack");

    let output = dir.join("mod-ru");
    let mod_root = output.join("Pack_Test_ru");
    let descriptor =
        std::fs::read_to_string(mod_root.join("descriptor.mod")).expect("descriptor.mod");
    assert!(descriptor.starts_with('\u{feff}'), "descriptor needs a BOM");
    assert!(descriptor.contains("name=\"Pack Test\""), "{descriptor}");
    assert!(
        !descriptor.contains("path="),
        "the path belongs in the .mod file only: {descriptor}"
    );

    let mod_file =
        std::fs::read_to_string(output.join("Pack_Test_ru.mod")).expect(".mod file");
    assert!(mod_file.ends_with("path=\"mod/Pack_Test_ru\"\n"), "{mod_file}");

    let packed = mod_root
        .join("localisation")
        .join("russian")
        .join("pack_l_russian.yml");
    assert!(packed.exists(), "translation was not packed: {packed:?}");
    let text = std::fs::read(&packed).expect("packed bytes");
    assert!(text.starts_with(&[0xEF, 0xBB, 0xBF]), "BOM must survive");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_written_file_leaves_no_temporary_siblings() {
    let dir = temp_dir("temp-files");
    let src = dir
        .join("localisation")
        .join("english")
        .join("tidy_l_english.yml");
    write(&src, "l_english:\n army.1:0 \"Division\"\n");
    let dst = dir
        .join("out")
        .join("localisation")
        .join("russian")
        .join("tidy_l_russian.yml");

    let glossary = Glossary::from_pairs(vec![("Division", "дивизия")], "test");
    let translator = pinned_translator(Some(glossary));
    let stop = AtomicBool::new(false);
    let mut log = |_message: &str, _tag: LogTag| {};

    let outcome = process_file(
        &src,
        &dst,
        &translator,
        &mut log,
        false,
        &stop,
        "ru",
        None,
        BrokenLineMode::RejectFile,
        None,
    );
    assert_eq!(outcome.result, Some(FileResult::Written(1)));

    let leftovers: Vec<String> = std::fs::read_dir(dst.parent().expect("parent"))
        .expect("read dir")
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .filter(|name| name.ends_with(".tmp"))
        .collect();
    assert!(leftovers.is_empty(), "temporary files left: {leftovers:?}");
    let _ = std::fs::remove_dir_all(&dir);
}
