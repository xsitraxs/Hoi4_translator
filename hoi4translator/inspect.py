"""Inspect localisation files without translating anything.

Not everyone has the game installed, and nobody wants to spend DeepL quota to
find out that one key is malformed. This module reads a mod's localisation the
same way the translator does, and reports what the translator would trip over:
lines that do not parse, duplicate keys, colour markers that are never closed.
It answers "will this mod translate cleanly?" without a single network request.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .localisation import (
    load_existing_translations,
    normalize_key,
    read_localisation_lines,
    split_value_line,
)
from .validation import PlaceholderValidator


@dataclass
class FileInspection:
    """Everything worth knowing about one localisation file."""

    path: str
    encoding: str = "utf-8"
    keys: int = 0
    values: int = 0
    comments: int = 0
    malformed: List[int] = field(default_factory=list)
    duplicates: Dict[str, List[int]] = field(default_factory=dict)
    broken_markers: List[tuple] = field(default_factory=list)
    translated: int = 0
    error: str = ""

    @property
    def has_blocking_problems(self) -> bool:
        """Problems that make the translator skip the file.

        A malformed line or a duplicate key means the translator cannot write
        the file; an unbalanced colour marker in the mod's own English does not
        -- the translation keeps it and the line is written as it is.
        """
        return bool(self.error or self.malformed or self.duplicates)

    @property
    def has_notes(self) -> bool:
        """Worth reading, but the file still translates."""
        return bool(self.broken_markers)

    @property
    def has_problems(self) -> bool:
        return self.has_blocking_problems or self.has_notes


def inspect_file(
    path: str,
    target_path: Optional[str] = None,
) -> FileInspection:
    """Parse `path` and collect its problems; never raises for bad content."""
    inspection = FileInspection(path=path)
    try:
        lines, encoding = read_localisation_lines(path)
    except (OSError, ValueError) as error:
        inspection.error = str(error)
        return inspection
    inspection.encoding = encoding

    seen: Dict[str, List[int]] = {}
    for number, line in enumerate(lines, start=1):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            inspection.comments += 1
            continue
        # The language header is not a key/value pair.
        if stripped.casefold().startswith("l_") and stripped.endswith(":"):
            continue

        parts = split_value_line(content)
        if parts is None:
            if ":" in stripped:
                inspection.malformed.append(number)
            continue

        key = normalize_key(parts[0])
        value = parts[1]
        inspection.keys += 1
        inspection.values += 1
        seen.setdefault(key, []).append(number)

        # An unclosed § in the source is a real bug that survives translation.
        issues = PlaceholderValidator.validate(value, value)
        if issues:
            inspection.broken_markers.append((number, key, issues))

    inspection.duplicates = {key: rows for key, rows in seen.items() if len(rows) > 1}
    if target_path and os.path.isfile(target_path):
        inspection.translated = len(load_existing_translations(target_path))
    return inspection


def inspect_files(
    paths: Sequence[str],
    target_paths: Optional[Dict[str, str]] = None,
) -> List[FileInspection]:
    """Inspect every file, keeping the given order."""
    target_paths = target_paths or {}
    return [inspect_file(path, target_paths.get(path)) for path in paths]


def format_report(inspections: Sequence[FileInspection], src_root: str) -> List[str]:
    """Human-readable summary: one line per file, then the problems."""
    lines: List[str] = []
    total_keys = 0
    total_malformed = 0
    bad_files = 0

    for inspection in inspections:
        relative = os.path.relpath(inspection.path, src_root)
        total_keys += inspection.keys
        total_malformed += len(inspection.malformed)
        if inspection.error:
            bad_files += 1
            lines.append(f"! {relative}: {inspection.error}")
            continue
        if inspection.has_blocking_problems:
            bad_files += 1
        parts = [f"{inspection.keys} ключей"]
        if inspection.translated:
            parts.append(f"{inspection.translated} уже переведено")
        if inspection.malformed:
            rows = ", ".join(map(str, inspection.malformed[:5]))
            more = "…" if len(inspection.malformed) > 5 else ""
            parts.append(f"НЕ ПАРСИТСЯ строк: {len(inspection.malformed)} (строки {rows}{more})")
        if inspection.duplicates:
            keys = ", ".join(list(inspection.duplicates)[:3])
            more = "…" if len(inspection.duplicates) > 3 else ""
            parts.append(f"дубликаты ключей: {len(inspection.duplicates)} ({keys}{more})")
        if inspection.broken_markers:
            parts.append(
                f"замечания по § в исходнике: {len(inspection.broken_markers)}"
            )
        if inspection.has_blocking_problems:
            prefix = "! "
        elif inspection.has_notes:
            prefix = "~ "
        else:
            prefix = "  "
        lines.append(f"{prefix}{relative}: " + ", ".join(parts))

        for number, key, issues in inspection.broken_markers[:5]:
            lines.append(f"      строка {number} [{key}]: {', '.join(issues)}")

    lines.append("")
    summary = f"Итого: файлов {len(inspections)}, ключей {total_keys}"
    if bad_files:
        summary += f", с проблемами {bad_files}"
    lines.append(summary)
    if total_malformed:
        lines.append(
            "Файлы со строками, которые не парсятся, переводчик пропустит целиком — "
            "исправьте их до запуска."
        )
    notes = sum(len(item.broken_markers) for item in inspections)
    if notes and not total_malformed:
        lines.append(
            "Замечания по § — это разметка самого мода (например, знак, закрытый через "
            "$ПЕРЕМЕННУЮ$): такие строки записываются как есть, файл переводится."
        )
    return lines
