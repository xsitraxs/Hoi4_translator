//! End-to-end behaviour of the CLI and the file pipeline.
//!
//! Every test here runs without a network: the values it translates are either
//! pinned by a glossary or are pure markup, which the pipeline resolves before an
//! engine is ever called.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

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
    let dst = temp_dir("dry-run");

    let mut cli = base_cli(src, dst.clone());
    cli.dry_run = true;

    assert_eq!(run(cli), 0, "a clean mod must pass the dry run");
    assert!(!dst.exists(), "the dry run must not create the output folder");
    let _ = std::fs::remove_dir_all(&dst);
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
    assert_eq!(second.stale, 1, "an edited source line must be re-translated");
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
