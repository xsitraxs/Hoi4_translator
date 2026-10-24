"""Machine-readable report of a translation run.

The GUI log is convenient while a run is in progress, but a mod author needs
a durable artefact: which files were written, which lines were rejected or
repaired, and how often the engine had to fall back.
"""
import csv
import io
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List

from .config import __version__, atomic_write_text, logger

# A large mod can produce thousands of broken rows; keep the file bounded but
# keep counting everything in the summary.
MAX_TRACKED_ROWS = 500


class RunReport:
    """Collects what happened during one run and writes it next to the output."""

    def __init__(self, engine: str, source_lang: str, target_lang: str,
                 src_root: str, dst_root: str):
        self.started_at = time.time()
        self.meta: Dict[str, Any] = {
            "version": __version__,
            "started": datetime.now().isoformat(timespec="seconds"),
            "engine": engine,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "source_dir": src_root,
            "output_dir": dst_root,
        }
        self.files: List[Dict[str, Any]] = []
        self.broken_rows: List[Dict[str, Any]] = []
        self.recovered_rows: List[Dict[str, Any]] = []
        self.untracked_rows = 0
        self.fallbacks: Dict[str, int] = {}

    # --- сбор данных -----------------------------------------------------

    def record_file(self, source: str, destination: str, status: str, *,
                    translated: int = 0, skipped: int = 0, stale: int = 0,
                    recovered: int = 0, broken: int = 0, reused: int = 0,
                    message: str = "") -> None:
        self.files.append({
            "source": source,
            "output": destination,
            "status": status,
            "translated": translated,
            "skipped": skipped,
            "stale": stale,
            "recovered": recovered,
            "broken": broken,
            "reused": reused,
            "message": message,
        })

    def record_broken(self, file: str, key: str, issues: List[str],
                      recovered: bool) -> None:
        entry = {"file": file, "key": key, "issues": list(issues)}
        target = self.recovered_rows if recovered else self.broken_rows
        if len(self.recovered_rows) + len(self.broken_rows) >= MAX_TRACKED_ROWS:
            self.untracked_rows += 1
            return
        target.append(entry)

    def set_fallbacks(self, counts: Dict[str, int]) -> None:
        self.fallbacks = dict(counts)

    # --- итоги -----------------------------------------------------------

    def summary(self) -> Dict[str, int]:
        """Aggregate counters for the final status line."""
        summary = {
            "files": len(self.files),
            "written": 0, "rejected": 0, "cancelled": 0, "errors": 0,
            "lines": 0, "skipped": 0, "stale": 0, "reused": 0,
            "recovered": 0, "broken": 0, "fallbacks": sum(self.fallbacks.values()),
        }
        status_key = {
            "written": "written", "cancelled": "cancelled", "error": "errors",
        }
        # "translated" is reported as "lines": it is the number of values that
        # were written from a fresh translation.
        counters = (
            ("translated", "lines"), ("skipped", "skipped"), ("stale", "stale"),
            ("reused", "reused"), ("recovered", "recovered"), ("broken", "broken"),
        )
        for entry in self.files:
            summary[status_key.get(entry["status"], "rejected")] += 1
            for source_key, target_key in counters:
                summary[target_key] += int(entry[source_key])
        return summary

    def to_dict(self) -> Dict[str, Any]:
        elapsed = max(time.time() - self.started_at, 0.0)
        return {
            **self.meta,
            "duration_seconds": round(elapsed, 1),
            "summary": self.summary(),
            "fallbacks": self.fallbacks,
            "hidden_rows": self.untracked_rows,
            "files": self.files,
            "recovered_rows": self.recovered_rows,
            "broken_rows": self.broken_rows,
        }

    # --- запись ----------------------------------------------------------

    def _file_stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    def _csv_text(self) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=';', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["state", "file", "key", "issues"])
        for row in self.recovered_rows:
            writer.writerow(["recovered", row["file"], row["key"], " | ".join(row["issues"])])
        for row in self.broken_rows:
            writer.writerow(["broken", row["file"], row["key"], " | ".join(row["issues"])])
        return buffer.getvalue()

    def write(self, directory: str) -> List[str]:
        """Write the report next to the translated mod. Returns the file paths."""
        stamp = self._file_stamp()
        json_path = os.path.join(directory, f"hoi4_translator_report_{stamp}.json")
        csv_path = os.path.join(directory, f"hoi4_translator_report_{stamp}.csv")
        written: List[str] = []
        if not directory or not os.path.isdir(directory):
            # The report is a side artefact: it must never create folders, and
            # it must never fail the run it describes.
            logger.warning("Отчёт не записан: нет каталога %s", directory)
            return written
        try:
            atomic_write_text(
                json_path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
            )
            written.append(json_path)
            atomic_write_text(csv_path, self._csv_text())
            written.append(csv_path)
        except OSError as error:
            logger.warning("Отчёт не записан: %s", error)
        return written
