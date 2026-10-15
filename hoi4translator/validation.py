"""Validation of translated values and the per-run validation report."""
import re
from typing import Any, Dict, List

from .localisation import PLACEHOLDER_RE
from .text import zip_equal


class PlaceholderValidator:
    """
    Проверяет что все плейсхолдеры из оригинала сохранились в переводе.
    Возвращает список проблем или пустой список если всё ок.
    """

    @staticmethod
    def extract_placeholders(text: str) -> List[str]:
        # Escapes are protected before translation, so they must match exactly.
        return PLACEHOLDER_RE.findall(text)

    _COLOR_MARKER_RE = re.compile(r'§[A-Za-z0-9!]')
    _INVALID_SECTION_RE = re.compile(r'§(?![A-Za-z0-9!])')

    @classmethod
    def _color_format_issues(cls, text: str, label: str) -> List[str]:
        issues = []
        if cls._INVALID_SECTION_RE.search(text):
            issues.append(f"{label}: некорректный знак §")
        active_color = False
        for marker in cls._COLOR_MARKER_RE.findall(text):
            active_color = marker != "§!"
        if active_color:
            issues.append(f"{label}: цветовой маркер не закрыт через §!")
        return issues

    @classmethod
    def validate(cls, original: str, translated: str) -> List[str]:
        """
        Возвращает список строк с описанием проблем.
        Пустой список = перевод валиден.
        """
        issues = []
        issues.extend(cls._color_format_issues(original, "исходник"))
        issues.extend(cls._color_format_issues(translated, "перевод"))
        original_colors = cls._COLOR_MARKER_RE.findall(original)
        translated_colors = cls._COLOR_MARKER_RE.findall(translated)
        if (
            original_colors != translated_colors
            and sorted(original_colors) == sorted(translated_colors)
        ):
            issues.append("изменён порядок цветовых маркеров §")

        orig_phs = cls.extract_placeholders(original)
        trans_phs = cls.extract_placeholders(translated)

        # Считаем вхождения каждого плейсхолдера
        orig_counts: Dict[str, int] = {}
        for ph in orig_phs:
            orig_counts[ph] = orig_counts.get(ph, 0) + 1

        trans_counts: Dict[str, int] = {}
        for ph in trans_phs:
            trans_counts[ph] = trans_counts.get(ph, 0) + 1

        for ph, count in orig_counts.items():
            trans_count = trans_counts.get(ph, 0)
            if trans_count == 0:
                issues.append(f"потерян: {ph!r}")
            elif trans_count < count:
                issues.append(f"недостаёт {count - trans_count}x {ph!r}")
            elif trans_count > count:
                issues.append(f"дублирован {trans_count - count}x {ph!r}")

        # Плейсхолдеры которые появились в переводе, но не были в оригинале
        for ph, _count in trans_counts.items():
            if ph not in orig_counts:
                issues.append(f"лишний: {ph!r}")

        return issues




def validate_pairs(originals: List[str], translations: List[str]) -> List[List[str]]:
    """Return the issues of every row; an empty list means the row is valid.

    The per-row form is what the pipeline needs to repair a single bad line
    instead of rejecting the whole file.
    """
    if len(originals) != len(translations):
        raise ValueError(
            f"Количество строк изменилось: ожидалось {len(originals)}, "
            f"получено {len(translations)}"
        )

    rows: List[List[str]] = []
    for original, translated in zip_equal(originals, translations):
        issues = []
        if "\n" in translated or "\r" in translated:
            issues.append("значение должно занимать одну физическую строку; используйте \\n")
        if _has_unescaped_quote(translated):
            issues.append('внутренние кавычки нужно экранировать как \\"')
        issues.extend(PlaceholderValidator.validate(original, translated))
        rows.append(issues)
    return rows


def validate_preview_translations(originals: List[str], translations: List[str]) -> List[str]:
    """Return preview rows that break the one-line value format or game tokens."""
    try:
        rows = validate_pairs(originals, translations)
    except ValueError as error:
        return [str(error)]

    problems = []
    for row_number, issues in enumerate(rows, start=1):
        if issues:
            problems.append(f"Строка {row_number}: {', '.join(issues)}")
    return problems


def _has_unescaped_quote(text: str) -> bool:
    """Return whether a value contains a raw quote instead of an escaped \"."""
    backslashes = 0
    for character in text:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"' and backslashes % 2 == 0:
            return True
        backslashes = 0
    return False


class ValidationReport:
    """Собирает статистику валидации за сессию."""
    # A large mod can produce thousands of broken rows; keep memory and the
    # final log bounded, but keep counting everything.
    MAX_TRACKED_BROKEN = 500

    def __init__(self):
        self.total = 0
        self.valid = 0
        self.fixed = 0
        self.broken: List[Dict[str, Any]] = []  # {file, key, issues}
        self.untracked_broken = 0

    def record(self, file: str, key: str, issues: List[str], auto_fixed: bool):
        self.total += 1
        if not issues:
            self.valid += 1
        elif auto_fixed:
            self.fixed += 1
        elif len(self.broken) < self.MAX_TRACKED_BROKEN:
            self.broken.append({'file': file, 'key': key, 'issues': issues})
        else:
            self.untracked_broken += 1

    @property
    def broken_count(self) -> int:
        return len(self.broken) + self.untracked_broken

    def broken_for_file(self, file: str) -> List[Dict[str, Any]]:
        return [entry for entry in self.broken if entry['file'] == file]

    def summary(self) -> str:
        return (f"Валидация: {self.valid}/{self.total} OK | "
                f"авто-фикс: {self.fixed} | битых: {self.broken_count}")
