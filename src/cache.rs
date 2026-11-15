//! SQLite-backed translation cache and source fingerprints.
//!
//! The cache has two jobs: never pay for the same sentence twice, and remember
//! which source text produced every value already written to a target file, so
//! an edited English line is translated again instead of being answered with a
//! stale translation.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

use anyhow::{Context, Result};
use rusqlite::{params, params_from_iter, Connection, OptionalExtension};
use sha2::{Digest, Sha256};

use crate::config::{path_key, CACHE_FORMAT_VERSION};

pub struct TranslationCache {
    path: PathBuf,
    connection: Mutex<Connection>,
}

impl TranslationCache {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                let _ = std::fs::create_dir_all(parent);
            }
        }
        let connection = Connection::open(path)
            .with_context(|| format!("не удалось открыть кэш {}", path.display()))?;
        let cache = Self {
            path: path.to_path_buf(),
            connection: Mutex::new(connection),
        };
        cache.init()?;
        Ok(cache)
    }

    /// An ephemeral cache, used by tests and by `--dry-run`.
    pub fn in_memory() -> Result<Self> {
        let connection = Connection::open_in_memory().context("не удалось создать кэш в памяти")?;
        let cache = Self {
            path: PathBuf::from(":memory:"),
            connection: Mutex::new(connection),
        };
        cache.init()?;
        Ok(cache)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn connection(&self) -> MutexGuard<'_, Connection> {
        // A panic in a worker thread must not take the cache down with it.
        self.connection
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn init(&self) -> Result<()> {
        let connection = self.connection();
        // Two runs may share the cache (a window and a command line, or a second
        // window); WAL plus a busy timeout turns "database is locked" into a
        // wait. Both are best-effort: a read-only or network file still works.
        let _ = connection.execute_batch(
            "PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA busy_timeout = 5000;",
        );
        connection.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS cache (
                hash_key TEXT PRIMARY KEY,
                original_text TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                engine_name TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hash ON cache(hash_key);
            CREATE TABLE IF NOT EXISTS file_sources (
                dst_path TEXT NOT NULL,
                key TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (dst_path, key)
            );
            "#,
        )?;
        Ok(())
    }

    fn hash_key(text: &str, source_lang: &str, target_lang: &str, engine: &str) -> String {
        let payload =
            serde_json::to_string(&(CACHE_FORMAT_VERSION, text, source_lang, target_lang, engine))
                .unwrap_or_else(|_| {
                    format!("{CACHE_FORMAT_VERSION}{text}{source_lang}{target_lang}{engine}")
                });
        let mut hasher = Sha256::new();
        hasher.update(payload.as_bytes());
        format!("{:x}", hasher.finalize())
    }

    pub fn get(
        &self,
        text: &str,
        source_lang: &str,
        target_lang: &str,
        engine: &str,
    ) -> Option<String> {
        let key = Self::hash_key(text, source_lang, target_lang, engine);
        let connection = self.connection();
        connection
            .query_row(
                "SELECT translated_text FROM cache WHERE hash_key = ?1",
                params![key],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .ok()
            .flatten()
    }

    pub fn set(
        &self,
        text: &str,
        translation: &str,
        source_lang: &str,
        target_lang: &str,
        engine: &str,
    ) {
        let key = Self::hash_key(text, source_lang, target_lang, engine);
        let created_at = crate::config::unix_now() as f64;
        let connection = self.connection();
        let _ = connection.execute(
            "INSERT OR REPLACE INTO cache
                (hash_key, original_text, translated_text, source_lang, target_lang, engine_name, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![key, text, translation, source_lang, target_lang, engine, created_at],
        );
    }

    // --- source fingerprints for "skip already translated" ------------------

    /// `{key: source_hash}` recorded for an already written file.
    pub fn get_source_hashes(&self, dst_path: &Path) -> BTreeMap<String, String> {
        let mut hashes = BTreeMap::new();
        let connection = self.connection();
        let Ok(mut statement) =
            connection.prepare("SELECT key, source_hash FROM file_sources WHERE dst_path = ?1")
        else {
            return hashes;
        };
        let Ok(rows) = statement.query_map(params![path_key(dst_path)], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        }) else {
            return hashes;
        };
        for row in rows.flatten() {
            hashes.insert(row.0, row.1);
        }
        hashes
    }

    /// Record the source text fingerprint of every value in a written file.
    pub fn set_source_hashes(&self, dst_path: &Path, hashes: &BTreeMap<String, String>) {
        if hashes.is_empty() {
            return;
        }
        let normalized = path_key(dst_path);
        let updated_at = crate::config::unix_now() as f64;
        let mut connection = self.connection();
        let Ok(transaction) = connection.transaction() else {
            return;
        };
        {
            let Ok(mut statement) = transaction.prepare(
                "INSERT INTO file_sources (dst_path, key, source_hash, updated_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(dst_path, key) DO UPDATE SET
                     source_hash = excluded.source_hash,
                     updated_at = excluded.updated_at",
            ) else {
                return;
            };
            for (key, hash) in hashes {
                let _ = statement.execute(params![&normalized, key, hash, updated_at]);
            }
        }
        let _ = transaction.commit();
    }

    pub fn forget_file(&self, dst_path: &Path) {
        let connection = self.connection();
        let _ = connection.execute(
            "DELETE FROM file_sources WHERE dst_path = ?1",
            params![path_key(dst_path)],
        );
    }

    /// Delete cached translations, optionally filtered. Returns the row count.
    pub fn clear(
        &self,
        engine: Option<&str>,
        source_lang: Option<&str>,
        target_lang: Option<&str>,
    ) -> usize {
        let mut conditions: Vec<&str> = Vec::new();
        let mut values: Vec<String> = Vec::new();
        if let Some(engine) = engine.filter(|value| !value.is_empty()) {
            conditions.push("engine_name = ?");
            values.push(engine.to_string());
        }
        if let Some(source_lang) = source_lang.filter(|value| !value.is_empty()) {
            conditions.push("source_lang = ?");
            values.push(source_lang.to_string());
        }
        if let Some(target_lang) = target_lang.filter(|value| !value.is_empty()) {
            conditions.push("target_lang = ?");
            values.push(target_lang.to_string());
        }
        let where_clause = if conditions.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", conditions.join(" AND "))
        };

        let connection = self.connection();
        let removed: usize = connection
            .query_row(
                &format!("SELECT COUNT(*) FROM cache{where_clause}"),
                params_from_iter(values.iter()),
                |row| row.get::<_, i64>(0),
            )
            .map(|count| count.max(0) as usize)
            .unwrap_or(0);
        let _ = connection.execute(
            &format!("DELETE FROM cache{where_clause}"),
            params_from_iter(values.iter()),
        );
        removed
    }

    /// Human-readable summary of the cache, one fact per line.
    ///
    /// Shared by `--cache-stats` and the window: the same numbers must not be
    /// described twice in two different ways.
    pub fn describe(path: &Path, stats: &BTreeMap<&'static str, usize>) -> Vec<String> {
        let mut lines = vec![
            format!("Кэш: {}", path.display()),
            format!(
                "  переводов: {}",
                stats.get("total").copied().unwrap_or(0)
            ),
            format!(
                "  движков: {}",
                stats.get("engines").copied().unwrap_or(0)
            ),
            format!(
                "  файлов с сохранёнными исходниками: {}",
                stats.get("files").copied().unwrap_or(0)
            ),
        ];
        if let Ok(metadata) = std::fs::metadata(path) {
            lines.push(format!(
                "  размер: {:.1} МБ",
                metadata.len() as f64 / (1024.0 * 1024.0)
            ));
        }
        lines
    }

    pub fn stats(&self) -> BTreeMap<&'static str, usize> {
        let connection = self.connection();
        let total: usize = connection
            .query_row("SELECT COUNT(*) FROM cache", [], |row| row.get::<_, i64>(0))
            .map(|count| count.max(0) as usize)
            .unwrap_or(0);
        let engines: usize = connection
            .query_row("SELECT COUNT(DISTINCT engine_name) FROM cache", [], |row| {
                row.get::<_, i64>(0)
            })
            .map(|count| count.max(0) as usize)
            .unwrap_or(0);
        let files: usize = connection
            .query_row(
                "SELECT COUNT(DISTINCT dst_path) FROM file_sources",
                [],
                |row| row.get::<_, i64>(0),
            )
            .map(|count| count.max(0) as usize)
            .unwrap_or(0);

        let mut stats = BTreeMap::new();
        stats.insert("total", total);
        stats.insert("engines", engines);
        stats.insert("files", files);
        stats
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_keys_stay_compatible_with_the_python_build() {
        // sha256 of json.dumps([2, text, src, tgt, engine], ensure_ascii=False,
        // separators=(",", ":")) — the exact key format 1.4.x wrote and 2.0 must
        // keep reading, otherwise every cached translation is silently ignored.
        assert_eq!(
            TranslationCache::hash_key("Division", "en", "ru", "Google"),
            "3fbfea85275925d24cc85bb1fa538bc8f565d81c48bcb9acd93e2c257e7d83d0"
        );
        assert_eq!(
            TranslationCache::hash_key("Привет, мир", "en", "ru", "DeepL"),
            "425582c0b71b4c16447f8c74b7e2737b0c188a4f18482a01a1afe51dfb30a799"
        );
    }

    #[test]
    fn values_round_trip() {
        let cache = TranslationCache::in_memory().expect("cache");
        assert!(cache.get("Rearm $STATE$", "en", "ru", "Google").is_none());
        cache.set(
            "Rearm $STATE$",
            "Перевооружите $STATE$",
            "en",
            "ru",
            "Google",
        );
        assert_eq!(
            cache.get("Rearm $STATE$", "en", "ru", "Google").as_deref(),
            Some("Перевооружите $STATE$")
        );
        // A different engine is a different cache entry.
        assert!(cache.get("Rearm $STATE$", "en", "ru", "DeepL").is_none());
        assert_eq!(cache.stats()["total"], 1);
    }

    #[test]
    fn source_hashes_are_stored_per_output_file() {
        let cache = TranslationCache::in_memory().expect("cache");
        let mut hashes = BTreeMap::new();
        hashes.insert("A".to_string(), "hash-a".to_string());
        hashes.insert("B".to_string(), "hash-b".to_string());
        let dst = Path::new("/out/localisation/russian/events_l_russian.yml");
        cache.set_source_hashes(dst, &hashes);
        assert_eq!(cache.get_source_hashes(dst), hashes);

        cache.forget_file(dst);
        assert!(cache.get_source_hashes(dst).is_empty());
    }

    #[test]
    fn the_summary_describes_the_same_facts_the_console_prints() {
        let mut stats = BTreeMap::new();
        stats.insert("total", 42usize);
        stats.insert("engines", 3usize);
        stats.insert("files", 7usize);
        // A path that does not exist: the size line must simply be absent.
        let lines = TranslationCache::describe(Path::new("/no/such/cache.db"), &stats);
        assert_eq!(lines.len(), 4);
        assert!(lines[0].starts_with("Кэш: "));
        assert!(lines[1].contains("42"));
        assert!(lines[2].contains('3'));
        assert!(lines[3].contains('7'));
    }

    #[test]
    fn clearing_can_be_filtered() {
        let cache = TranslationCache::in_memory().expect("cache");
        cache.set("one", "один", "en", "ru", "Google");
        cache.set("two", "два", "en", "de", "DeepL");
        assert_eq!(cache.clear(Some("Google"), None, None), 1);
        assert_eq!(cache.stats()["total"], 1);
        assert_eq!(cache.clear(None, None, None), 1);
        assert_eq!(cache.stats()["total"], 0);
    }
}
