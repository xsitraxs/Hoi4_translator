//! Paths, logging and crash-safe file IO.
//!
//! Everything here is shared by every other module: where the cache lives,
//! where the log goes, and how a translated file is written so that a crash
//! never leaves a half-written `.yml` behind.

use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Keep in sync with the git tag of a release (CI compares them).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

pub const APP_NAME: &str = "Hoi4Translator";

/// Default number of concurrent translation requests for one run.
pub const DEFAULT_CONCURRENCY: usize = 15;

/// Largest thread count one file may use. Each worker is an OS thread, and the
/// engines rate-limit long before this, so more is only a way to hurt the machine.
pub const MAX_CONCURRENCY: usize = 64;

/// Bump whenever translation/post-processing semantics change so stale cache
/// entries cannot bypass newer validation or typography rules.
pub const CACHE_FORMAT_VERSION: u32 = 2;

/// Severity of a pipeline message, used to prefix CLI output.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LogTag {
    Info,
    Warning,
    Error,
}

// ---------------------------------------------------------------------------
// Application directories
// ---------------------------------------------------------------------------

/// Persistent, per-user data directory; falls back like the Python build did.
pub fn app_data_dir() -> PathBuf {
    static DIR: OnceLock<PathBuf> = OnceLock::new();
    DIR.get_or_init(compute_app_data_dir).clone()
}

fn compute_app_data_dir() -> PathBuf {
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));

    let mut candidates: Vec<PathBuf> = Vec::new();
    if cfg!(windows) {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Local"));
        candidates.push(base.join(APP_NAME));
    } else if cfg!(target_os = "macos") {
        candidates.push(
            home.join("Library")
                .join("Application Support")
                .join(APP_NAME),
        );
    } else {
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| home.join(".local").join("share"));
        candidates.push(base.join(APP_NAME));
    }
    // Last resort: still persistent, unlike a temporary directory.
    candidates.push(home.join(".hoi4_translator"));

    for directory in &candidates {
        if fs::create_dir_all(directory).is_ok() {
            return directory.clone();
        }
    }
    std::env::temp_dir()
}

pub fn log_file() -> PathBuf {
    app_data_dir().join("hoi4_translator.log")
}

pub fn cache_file() -> PathBuf {
    app_data_dir().join("translation_cache.db")
}

pub fn settings_file() -> PathBuf {
    app_data_dir().join("settings.json")
}

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

/// Rotate the log once it reaches a megabyte, keeping three older files: a run
/// over a big mod can log tens of thousands of lines, and an append-only file
/// would grow without limit on the user's disk.
const LOG_MAX_BYTES: u64 = 1024 * 1024;
const LOG_BACKUPS: usize = 3;

fn backup_path(path: &Path, index: usize) -> PathBuf {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "hoi4_translator.log".to_string());
    path.with_file_name(format!("{name}.{index}"))
}

fn rotate_log(path: &Path) {
    let size = fs::metadata(path)
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    if size < LOG_MAX_BYTES {
        return;
    }
    for index in (1..LOG_BACKUPS).rev() {
        let _ = fs::rename(backup_path(path, index), backup_path(path, index + 1));
    }
    let _ = fs::rename(path, backup_path(path, 1));
}

/// The single log file, opened once per process; `None` when it cannot be
/// created, which must never stop a run.
fn log_handle() -> Option<&'static Mutex<File>> {
    static HANDLE: OnceLock<Option<Mutex<File>>> = OnceLock::new();
    HANDLE
        .get_or_init(|| {
            let path = log_file();
            rotate_log(&path);
            OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .ok()
                .map(Mutex::new)
        })
        .as_ref()
}

fn write_log_line(line: &str) {
    if let Some(handle) = log_handle() {
        if let Ok(mut file) = handle.lock() {
            let _ = file.write_all(line.as_bytes());
        }
    }
}

fn level_name(tag: LogTag) -> &'static str {
    match tag {
        LogTag::Info => "INFO",
        LogTag::Warning => "WARNING",
        LogTag::Error => "ERROR",
    }
}

/// Append a pipeline message to the run log.
///
/// Pipeline messages reach the user through the CLI callback, which owns the
/// console (and the `--quiet` filter); they still belong in the file, otherwise
/// the log of a failed run says nothing about the files that failed.
pub fn log_line(tag: LogTag, message: &str) {
    write_log_line(&format!(
        "{} - {} - {}\n",
        now_iso8601(),
        level_name(tag),
        message
    ));
}

struct AppLogger;

impl log::Log for AppLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.level() <= log::Level::Info
    }

    fn log(&self, record: &log::Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let level = match record.level() {
            log::Level::Error => "ERROR",
            log::Level::Warn => "WARNING",
            log::Level::Info => "INFO",
            log::Level::Debug => "DEBUG",
            log::Level::Trace => "TRACE",
        };
        let line = format!("{} - {level} - {}\n", now_iso8601(), record.args());
        // Console messages share stdout with progress lines: two streams made the
        // output unreadable as soon as an engine warning landed mid-file. The
        // write is fallible on purpose — `| head` closes the pipe, and a logging
        // call must not be the thing that panics.
        let mut stdout = io::stdout();
        let _ = stdout.write_all(line.as_bytes());
        let _ = stdout.flush();
        write_log_line(&line);
    }

    fn flush(&self) {
        if let Some(handle) = log_handle() {
            if let Ok(mut file) = handle.lock() {
                let _ = file.flush();
            }
        }
    }
}

/// Install the global logger once; a read-only data directory is not fatal.
pub fn init_logging() {
    static LOGGER: OnceLock<AppLogger> = OnceLock::new();
    let logger = LOGGER.get_or_init(|| AppLogger);
    if log::set_logger(logger).is_ok() {
        log::set_max_level(log::LevelFilter::Info);
    }
}

// ---------------------------------------------------------------------------
// Time helpers (no external date crate)
// ---------------------------------------------------------------------------

/// Seconds since the Unix epoch, saturating instead of panicking pre-1970.
pub fn unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// `(year, month, day, hour, minute, second)` in UTC.
fn civil_now() -> (i64, u32, u32, u32, u32, u32) {
    let secs = unix_now() as i64;
    let days = secs.div_euclid(86_400);
    let time_of_day = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    (
        year,
        month,
        day,
        (time_of_day / 3600) as u32,
        ((time_of_day % 3600) / 60) as u32,
        (time_of_day % 60) as u32,
    )
}

/// Howard Hinnant's `civil_from_days`, valid for any day count.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }) / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m as u32, d as u32)
}

/// ISO-8601 timestamp with second precision, e.g. `2026-11-14T09:31:07`.
pub fn now_iso8601() -> String {
    let (y, mo, d, h, mi, s) = civil_now();
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}")
}

/// File-name stamp for reports, e.g. `20261114-093107`.
pub fn file_stamp() -> String {
    let (y, mo, d, h, mi, s) = civil_now();
    format!("{y:04}{mo:02}{d:02}-{h:02}{mi:02}{s:02}")
}

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

/// Absolute path with `.` and `..` resolved lexically.
///
/// Symlinks are deliberately not followed: the checks that use this only need
/// to know whether two user-supplied directories overlap, and resolving a link
/// would require the target to exist.
pub fn normalized_abs(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };

    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    normalized
}

/// Cache/database key for a path: absolute and case-folded on Windows.
pub fn path_key(path: &Path) -> String {
    let text = normalized_abs(path).to_string_lossy().to_string();
    if cfg!(windows) {
        text.to_lowercase()
    } else {
        text
    }
}

/// `child.starts_with(parent)`, case-insensitively on Windows.
pub fn path_contains(parent: &Path, child: &Path) -> bool {
    if cfg!(windows) {
        let parent = path_key(parent);
        let child = path_key(child);
        child == parent
            || child.starts_with(&format!("{parent}\\"))
            || child.starts_with(&format!("{parent}/"))
    } else {
        child.starts_with(parent)
    }
}

// ---------------------------------------------------------------------------
// Atomic writes
// ---------------------------------------------------------------------------

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn temp_sibling(destination: &Path) -> PathBuf {
    let name = destination
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "output".to_string());
    let serial = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    destination.with_file_name(format!(".{name}.{}.{serial}.tmp", std::process::id()))
}

/// The shape `temp_sibling` produces: `.<name>.<pid>.<serial>.tmp`.
fn is_temp_name(name: &str) -> bool {
    name.starts_with('.') && name.ends_with(".tmp")
}

/// Remove temporary siblings an earlier run left behind when it was killed.
///
/// Only our own `.<name>.<pid>.<serial>.tmp` files older than a day are touched:
/// a file a live run is writing is seconds old, and a day is long enough that no
/// honest write looks abandoned. The sweep runs once per directory per process.
fn sweep_abandoned_temp_files(directory: &Path) {
    static SWEPT: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();
    let swept = SWEPT.get_or_init(|| Mutex::new(HashSet::new()));
    {
        let mut guard = swept
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if !guard.insert(directory.to_path_buf()) {
            return;
        }
    }
    const MAX_AGE: Duration = Duration::from_secs(24 * 60 * 60);
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        if !is_temp_name(name) {
            continue;
        }
        let abandoned = entry
            .metadata()
            .and_then(|metadata| metadata.modified())
            .ok()
            .and_then(|modified| modified.elapsed().ok())
            .map(|age| age > MAX_AGE)
            .unwrap_or(false);
        if abandoned {
            let _ = fs::remove_file(&path);
        }
    }
}

/// Persist a rename by flushing the directory that holds the new entry.
#[cfg(unix)]
fn sync_directory(directory: &Path) {
    if let Ok(handle) = File::open(directory) {
        let _ = handle.sync_all();
    }
}

#[cfg(not(unix))]
fn sync_directory(_directory: &Path) {}

/// Write `bytes` to a sibling temporary file, then replace `destination`.
///
/// The game may be reading the output folder while a run is in progress, so a
/// truncated file must never be observable: either the old content or the new
/// content is on disk, never a half-written line.
pub fn atomic_write_bytes(destination: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = match destination.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
        _ => PathBuf::from("."),
    };
    fs::create_dir_all(&parent)?;
    sweep_abandoned_temp_files(&parent);

    let temporary = temp_sibling(destination);
    let result = (|| -> io::Result<()> {
        let mut file = File::create(&temporary)?;
        file.write_all(bytes)?;
        file.flush()?;
        file.sync_all()?;
        drop(file);
        match fs::rename(&temporary, destination) {
            Ok(()) => {
                sync_directory(&parent);
                Ok(())
            }
            Err(error) => {
                // Windows refuses to replace an existing file with rename(), so
                // the old file is removed first. That leaves a window where the
                // path does not exist, unlike the POSIX branch — the game must
                // never see a *truncated* file, and it does not.
                if destination.exists() {
                    fs::remove_file(destination)?;
                    fs::rename(&temporary, destination)?;
                    sync_directory(&parent);
                    Ok(())
                } else {
                    Err(error)
                }
            }
        }
    })();

    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

pub fn atomic_write_text(destination: &Path, text: &str) -> io::Result<()> {
    atomic_write_bytes(destination, text.as_bytes())
}

/// Write a localisation file the way the game expects it: UTF-8 with a BOM.
pub fn atomic_write_localisation(destination: &Path, lines: &[String]) -> io::Result<()> {
    let total: usize = lines.iter().map(|line| line.len()).sum();
    let mut buffer = String::with_capacity(total + 3);
    buffer.push('\u{feff}');
    for line in lines {
        buffer.push_str(line);
    }
    atomic_write_bytes(destination, buffer.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates_are_correct() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(19_000), (2022, 1, 8));
        assert_eq!(civil_from_days(-1), (1969, 12, 31));
    }

    #[test]
    fn normalized_paths_drop_dot_segments() {
        let path = normalized_abs(Path::new("examples/../examples/demo-mod"));
        assert!(path.ends_with(Path::new("examples/demo-mod")));
        assert!(path.is_absolute());
    }

    #[test]
    fn only_our_own_temp_names_are_swept() {
        assert!(is_temp_name(".file.yml.1234.7.tmp"));
        assert!(!is_temp_name("file.yml"));
        assert!(!is_temp_name(".file.yml"));
        assert!(!is_temp_name(".file.yml.1234.7.tmp.bak"));
    }

    #[test]
    fn a_fresh_temp_file_is_left_alone() {
        let dir = std::env::temp_dir().join(format!("hoi4-sweep-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("temp dir");
        let fresh = dir.join(".kept.yml.1.0.tmp");
        fs::write(&fresh, "x").expect("write");

        atomic_write_text(&dir.join("kept.yml"), "content").expect("write");
        assert!(fresh.exists(), "a live run's temp file must survive");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn atomic_write_replaces_existing_content() {
        let dir = std::env::temp_dir().join(format!("hoi4-atomic-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("temp dir");
        let target = dir.join("file.yml");
        atomic_write_text(&target, "first").expect("first write");
        atomic_write_text(&target, "second").expect("second write");
        assert_eq!(fs::read_to_string(&target).expect("read"), "second");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn localisation_writes_start_with_a_bom() {
        let dir = std::env::temp_dir().join(format!("hoi4-bom-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("temp dir");
        let target = dir.join("out.yml");
        atomic_write_localisation(&target, &["l_russian:\n".to_string()]).expect("write");
        let bytes = fs::read(&target).expect("read");
        assert!(bytes.starts_with(&[0xEF, 0xBB, 0xBF]));
        let _ = fs::remove_dir_all(&dir);
    }
}
