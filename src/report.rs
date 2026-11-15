//! Machine-readable report of a translation run.
//!
//! The console log is convenient while a run is in progress, but a mod author
//! needs a durable artefact: which files were written, which lines were rejected
//! or repaired, and how often the engine had to fall back.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::config::{atomic_write_text, file_stamp, now_iso8601, unix_now, VERSION};

/// A large mod can produce thousands of broken rows; keep the file bounded but
/// keep counting everything in the summary.
pub const MAX_TRACKED_ROWS: usize = 500;

#[derive(Debug, Clone)]
pub struct FileRecord {
    pub source: String,
    pub output: String,
    pub status: String,
    pub translated: usize,
    pub skipped: usize,
    pub stale: usize,
    pub recovered: usize,
    pub broken: usize,
    pub reused: usize,
    pub message: String,
}

#[derive(Debug, Clone)]
pub struct RowRecord {
    pub file: String,
    pub key: String,
    pub issues: Vec<String>,
}

/// Collects what happened during one run and writes it next to the output.
#[derive(Debug, Clone)]
pub struct RunReport {
    pub started_at: u64,
    pub started: String,
    pub engine: String,
    pub source_lang: String,
    pub target_lang: String,
    pub source_dir: String,
    pub output_dir: String,
    pub files: Vec<FileRecord>,
    pub broken_rows: Vec<RowRecord>,
    pub recovered_rows: Vec<RowRecord>,
    pub untracked_rows: usize,
    pub fallbacks: BTreeMap<String, usize>,
}

impl RunReport {
    pub fn new(
        engine: &str,
        source_lang: &str,
        target_lang: &str,
        src_root: &Path,
        dst_root: &Path,
    ) -> Self {
        Self {
            started_at: unix_now(),
            started: now_iso8601(),
            engine: engine.to_string(),
            source_lang: source_lang.to_string(),
            target_lang: target_lang.to_string(),
            source_dir: src_root.display().to_string(),
            output_dir: dst_root.display().to_string(),
            files: Vec::new(),
            broken_rows: Vec::new(),
            recovered_rows: Vec::new(),
            untracked_rows: 0,
            fallbacks: BTreeMap::new(),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn record_file(
        &mut self,
        source: &str,
        destination: &Path,
        status: &str,
        translated: usize,
        skipped: usize,
        stale: usize,
        recovered: usize,
        broken: usize,
        reused: usize,
        message: &str,
    ) {
        self.files.push(FileRecord {
            source: source.to_string(),
            output: destination.display().to_string(),
            status: status.to_string(),
            translated,
            skipped,
            stale,
            recovered,
            broken,
            reused,
            message: message.to_string(),
        });
    }

    pub fn record_broken(&mut self, file: &str, key: &str, issues: &[String], recovered: bool) {
        if self.recovered_rows.len() + self.broken_rows.len() >= MAX_TRACKED_ROWS {
            self.untracked_rows += 1;
            return;
        }
        let entry = RowRecord {
            file: file.to_string(),
            key: key.to_string(),
            issues: issues.to_vec(),
        };
        if recovered {
            self.recovered_rows.push(entry);
        } else {
            self.broken_rows.push(entry);
        }
    }

    pub fn set_fallbacks(&mut self, counts: BTreeMap<String, usize>) {
        self.fallbacks = counts;
    }

    /// Aggregate counters for the final status line.
    pub fn summary(&self) -> BTreeMap<&'static str, i64> {
        let mut summary: BTreeMap<&'static str, i64> = BTreeMap::new();
        for key in [
            "files",
            "written",
            "rejected",
            "cancelled",
            "errors",
            "lines",
            "skipped",
            "stale",
            "reused",
            "recovered",
            "broken",
            "fallbacks",
        ] {
            summary.insert(key, 0);
        }
        *summary.entry("files").or_insert(0) = self.files.len() as i64;

        for entry in &self.files {
            let status_key = match entry.status.as_str() {
                "written" => "written",
                "cancelled" => "cancelled",
                "error" => "errors",
                _ => "rejected",
            };
            *summary.entry(status_key).or_insert(0) += 1;
            *summary.entry("lines").or_insert(0) += entry.translated as i64;
            *summary.entry("skipped").or_insert(0) += entry.skipped as i64;
            *summary.entry("stale").or_insert(0) += entry.stale as i64;
            *summary.entry("reused").or_insert(0) += entry.reused as i64;
            *summary.entry("recovered").or_insert(0) += entry.recovered as i64;
            *summary.entry("broken").or_insert(0) += entry.broken as i64;
        }
        *summary.entry("fallbacks").or_insert(0) =
            self.fallbacks.values().map(|count| *count as i64).sum();
        summary
    }

    pub fn to_json(&self) -> Value {
        let elapsed = unix_now().saturating_sub(self.started_at) as f64;
        let summary: BTreeMap<&str, i64> = self.summary().into_iter().collect();

        let rows = |records: &[RowRecord]| -> Vec<Value> {
            records
                .iter()
                .map(|row| json!({"file": &row.file, "key": &row.key, "issues": &row.issues}))
                .collect()
        };

        json!({
            "version": VERSION,
            "started": &self.started,
            "engine": &self.engine,
            "source_lang": &self.source_lang,
            "target_lang": &self.target_lang,
            "source_dir": &self.source_dir,
            "output_dir": &self.output_dir,
            "duration_seconds": (elapsed * 10.0).round() / 10.0,
            "summary": summary,
            "fallbacks": &self.fallbacks,
            "hidden_rows": self.untracked_rows,
            "files": self.files.iter().map(|file| json!({
                "source": &file.source,
                "output": &file.output,
                "status": &file.status,
                "translated": file.translated,
                "skipped": file.skipped,
                "stale": file.stale,
                "recovered": file.recovered,
                "broken": file.broken,
                "reused": file.reused,
                "message": &file.message,
            })).collect::<Vec<_>>(),
            "recovered_rows": rows(&self.recovered_rows),
            "broken_rows": rows(&self.broken_rows),
        })
    }

    fn csv_text(&self) -> String {
        let mut output = String::from("state;file;key;issues\n");
        for row in &self.recovered_rows {
            output.push_str(&format!(
                "recovered;{};{};{}\n",
                csv_field(&row.file),
                csv_field(&row.key),
                csv_field(&row.issues.join(" | "))
            ));
        }
        for row in &self.broken_rows {
            output.push_str(&format!(
                "broken;{};{};{}\n",
                csv_field(&row.file),
                csv_field(&row.key),
                csv_field(&row.issues.join(" | "))
            ));
        }
        output
    }

    /// Write the report next to the translated mod; returns the file paths.
    ///
    /// A report is a side artefact: it never creates folders and never fails the
    /// run it describes.
    pub fn write(&self, directory: &Path) -> Vec<PathBuf> {
        let mut written = Vec::new();
        if directory.as_os_str().is_empty() {
            log::warn!("Отчёт не записан: не указан каталог результата");
            return written;
        }
        if !directory.is_dir() && std::fs::create_dir_all(directory).is_err() {
            // Every file may have been rejected, so the folder never appeared.
            log::warn!("Отчёт не записан: нет каталога {}", directory.display());
            return written;
        }

        let stamp = file_stamp();
        let json_path = directory.join(format!("hoi4_translator_report_{stamp}.json"));
        let csv_path = directory.join(format!("hoi4_translator_report_{stamp}.csv"));

        let json_text =
            serde_json::to_string_pretty(&self.to_json()).unwrap_or_else(|_| "{}".to_string());
        if let Err(error) = atomic_write_text(&json_path, &json_text) {
            log::warn!("Отчёт не записан: {error}");
            return written;
        }
        written.push(json_path);

        if let Err(error) = atomic_write_text(&csv_path, &self.csv_text()) {
            log::warn!("Отчёт не записан: {error}");
            return written;
        }
        written.push(csv_path);
        written
    }
}

/// Quote a CSV field the way the Python report did: only when it must be.
fn csv_field(value: &str) -> String {
    if value.contains(';') || value.contains('"') || value.contains('\n') || value.contains('\r') {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_summary_counts_every_status() {
        let mut report = RunReport::new("Google", "en", "ru", Path::new("/in"), Path::new("/out"));
        report.record_file(
            "a.yml",
            Path::new("/out/a.yml"),
            "written",
            3,
            1,
            0,
            0,
            0,
            2,
            "",
        );
        report.record_file(
            "b.yml",
            Path::new("/out/b.yml"),
            "rejected",
            0,
            0,
            0,
            0,
            1,
            0,
            "bad",
        );
        report.record_file(
            "c.yml",
            Path::new("/out/c.yml"),
            "error",
            0,
            0,
            0,
            0,
            0,
            0,
            "boom",
        );

        let summary = report.summary();
        assert_eq!(summary["files"], 3);
        assert_eq!(summary["written"], 1);
        assert_eq!(summary["rejected"], 1);
        assert_eq!(summary["errors"], 1);
        assert_eq!(summary["lines"], 3);
        assert_eq!(summary["skipped"], 1);
        assert_eq!(summary["reused"], 2);
        assert_eq!(summary["broken"], 1);
    }

    #[test]
    fn broken_rows_are_bounded_but_counted() {
        let mut report = RunReport::new("Google", "en", "ru", Path::new("/in"), Path::new("/out"));
        for index in 0..(MAX_TRACKED_ROWS + 5) {
            report.record_broken("a.yml", &format!("K{index}"), &["issue".to_string()], false);
        }
        assert_eq!(report.broken_rows.len(), MAX_TRACKED_ROWS);
        assert_eq!(report.untracked_rows, 5);
    }

    #[test]
    fn the_json_report_has_the_expected_shape() {
        let mut report = RunReport::new("Google", "en", "ru", Path::new("/in"), Path::new("/out"));
        report.record_file(
            "a.yml",
            Path::new("/out/a.yml"),
            "written",
            1,
            0,
            0,
            0,
            0,
            0,
            "",
        );
        report.set_fallbacks(BTreeMap::from([("Google → MyMemory".to_string(), 2)]));
        let document = report.to_json();
        assert_eq!(document["version"], VERSION);
        assert_eq!(document["engine"], "Google");
        assert_eq!(document["summary"]["written"], 1);
        assert_eq!(document["fallbacks"]["Google → MyMemory"], 2);
        assert_eq!(document["files"][0]["source"], "a.yml");
    }

    #[test]
    fn the_csv_report_quotes_separators() {
        let mut report = RunReport::new("Google", "en", "ru", Path::new("/in"), Path::new("/out"));
        report.record_broken("a.yml", "K", &["one".to_string(), "two".to_string()], false);
        let csv = report.csv_text();
        assert!(csv.starts_with("state;file;key;issues\n"));
        assert!(csv.contains("broken;a.yml;K;one | two"));
    }
}
