//! HoI4 Localization Tool — batch translation of Hearts of Iron IV mods.
//!
//! The library is split the way a translation run is:
//!
//! * [`localisation`] reads and writes the game's `.yml` format;
//! * [`text`] protects Paradox markup and fixes typography;
//! * [`validation`] decides whether a translated line may be written at all;
//! * [`translation`] talks to Google, DeepL and MyMemory through the fallback
//!   chain, with the [`cache`] in front of it;
//! * [`pipeline`] puts those together per file;
//! * [`modpack`] turns a finished run into a launcher-ready mod;
//! * [`cli`] is the command line front end.

pub mod cache;
pub mod cli;
pub mod config;
pub mod glossary;
pub mod inspect;
pub mod localisation;
pub mod modpack;
pub mod pipeline;
pub mod providers;
pub mod report;
pub mod text;
pub mod translation;
pub mod validation;
pub mod vanilla;

/// Version of the crate, also used by the release workflow.
pub use config::VERSION as __version__;
pub use localisation::{
    get_hoi4_language_name, normalize_key, HOI4_LANGUAGE_NAMES, HOI4_TARGET_LANGS,
};
pub use translation::{TranslationError, Translator};

use std::sync::atomic::AtomicBool;

/// Convenience wrapper: process a single file with the default options.
///
/// Mostly useful for integrations and tests; the CLI uses [`pipeline::process_file`]
/// directly so it can share one translator across every file.
pub fn translate_file(
    src_path: &std::path::Path,
    dst_path: &std::path::Path,
    translator: &Translator,
    target_lang: &str,
    skip_translated: bool,
    stop: &AtomicBool,
) -> pipeline::FileOutcome {
    let mut log = |message: &str, tag: config::LogTag| match tag {
        config::LogTag::Error => log::error!("{message}"),
        config::LogTag::Warning => log::warn!("{message}"),
        config::LogTag::Info => log::info!("{message}"),
    };
    pipeline::process_file(
        src_path,
        dst_path,
        translator,
        &mut log,
        skip_translated,
        stop,
        target_lang,
        None,
        pipeline::BrokenLineMode::RejectFile,
        None,
    )
}
