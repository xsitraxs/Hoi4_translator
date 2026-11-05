"""SQLite-backed translation cache and source fingerprints."""
import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from .config import CACHE_FORMAT_VERSION, logger


class TranslationCache:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # One connection for the whole session, shared by the async engine's
        # worker threads and by the GUI thread, so every statement goes through
        # self._lock. `timeout` is what SQLite calls a busy timeout: a second
        # instance of the app (or a WAL reader) holding the write lock makes a
        # statement wait instead of failing with "database is locked".
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._lock = threading.Lock()
        self._closed = False
        self._apply_pragmas()
        self._init_db()

    def _apply_pragmas(self) -> None:
        """Durability and concurrency settings for the cache.

        WAL keeps a committed entry readable while a writer is appending, and
        leaves a journal SQLite can roll back after a crash in the middle of a
        commit. It is still only a cache: a lost entry costs one API call, so
        `synchronous=NORMAL` (the mode SQLite itself recommends for WAL) is
        enough and saves an fsync per written translation.

        The transaction mode is deliberately left at the default (deferred,
        implicit BEGIN). `isolation_level=None` would autocommit every
        statement, so `clear()` would delete rows without the surrounding
        transaction and a failing `executemany` would leave half a file's
        fingerprints behind.
        """
        try:
            self._conn.execute("PRAGMA busy_timeout = 30000")
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            # ":memory:" has no journal to switch and no file to share, so a
            # differing mode is only interesting for a real database file.
            if mode and str(mode[0]).casefold() != "wal" and self.db_path != ":memory:":
                logger.warning(
                    "Кэш работает без WAL (режим %s): файловая система не поддерживает",
                    mode[0],
                )
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as error:
            # A read-only or network copy of the database still has to open.
            logger.warning("Не удалось включить WAL для кэша: %s", error)

    def _init_db(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS cache (
                    hash_key TEXT PRIMARY KEY,
                    original_text TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    engine_name TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_hash ON cache(hash_key)')
            # Remembers which source text was behind every value already
            # written to a target file, so an edited source line is re-translated
            # instead of being answered with a stale translation.
            cur.execute('''
                CREATE TABLE IF NOT EXISTS file_sources (
                    dst_path TEXT NOT NULL,
                    key TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (dst_path, key)
                )
            ''')
            self._conn.commit()

    def _get_hash(self, text: str, src: str, tgt: str, engine: str) -> str:
        key_data = json.dumps(
            [CACHE_FORMAT_VERSION, text, src, tgt, engine],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(key_data.encode("utf-8")).hexdigest()

    def get(self, text: str, src: str, tgt: str, engine: str) -> Optional[str]:
        h = self._get_hash(text, src, tgt, engine)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute('SELECT translated_text FROM cache WHERE hash_key = ?', (h,))
            row = cur.fetchone()
        return row[0] if row else None

    def set(self, text: str, translation: str, src: str, tgt: str, engine: str):
        h = self._get_hash(text, src, tgt, engine)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT OR REPLACE INTO cache
                (hash_key, original_text, translated_text, source_lang, target_lang, engine_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (h, text, translation, src, tgt, engine, time.time()))
            self._conn.commit()

    # --- Контрольные суммы исходника для режима «пропускать переведённое» ---

    def get_source_hashes(self, dst_path: str) -> Dict[str, str]:
        """Return {key: source_hash} recorded for an already written file."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                'SELECT key, source_hash FROM file_sources WHERE dst_path = ?',
                (os.path.normcase(os.path.abspath(dst_path)),),
            )
            return dict(cur.fetchall())

    def set_source_hashes(self, dst_path: str, hashes: Dict[str, str]) -> None:
        """Record the source text fingerprint of every value in a written file."""
        if not hashes:
            return
        normalized = os.path.normcase(os.path.abspath(dst_path))
        now = time.time()
        rows = [(normalized, key, value, now) for key, value in hashes.items()]
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(
                '''
                INSERT INTO file_sources (dst_path, key, source_hash, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(dst_path, key) DO UPDATE SET
                    source_hash = excluded.source_hash,
                    updated_at = excluded.updated_at
                ''',
                rows,
            )
            self._conn.commit()

    def forget_file(self, dst_path: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                'DELETE FROM file_sources WHERE dst_path = ?',
                (os.path.normcase(os.path.abspath(dst_path)),),
            )
            self._conn.commit()

    def clear(self, engine: Optional[str] = None, source_lang: Optional[str] = None,
              target_lang: Optional[str] = None) -> int:
        """Delete cached translations, optionally filtered. Returns the row count."""
        conditions = []
        parameters: List[Any] = []
        if engine:
            conditions.append('engine_name = ?')
            parameters.append(engine)
        if source_lang:
            conditions.append('source_lang = ?')
            parameters.append(source_lang)
        if target_lang:
            conditions.append('target_lang = ?')
            parameters.append(target_lang)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f'SELECT COUNT(*) FROM cache{where}', parameters)
            removed = cur.fetchone()[0]
            cur.execute(f'DELETE FROM cache{where}', parameters)
            self._conn.commit()
        return removed

    def get_stats(self) -> Dict[str, int]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute('SELECT COUNT(*) FROM cache')
            total = cur.fetchone()[0]
            cur.execute('SELECT COUNT(DISTINCT engine_name) FROM cache')
            engines = cur.fetchone()[0]
            cur.execute('SELECT COUNT(DISTINCT dst_path) FROM file_sources')
            tracked_files = cur.fetchone()[0]
        return {'total': total, 'engines': engines, 'files': tracked_files}

    def close(self):
        """Close the database; safe to call twice and from any thread.

        `translator.__aexit__` runs on the event-loop thread while worker
        threads may still be finishing a `get`/`set`, so the lock has to cover
        the close as well -- otherwise the connection is torn down in the
        middle of a statement.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                # Fold the WAL back into the main file so a portable copy of the
                # cache is one file and no -wal grows next to it forever.
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
