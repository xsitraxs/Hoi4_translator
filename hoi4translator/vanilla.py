"""Reuse the game's own translations instead of machine-translating them.

Vanilla Hearts of Iron IV ships ~100 000 localisation lines and an official
translation for every supported language. A mod reuses those lines verbatim all
the time: focus names, unit types, country adjectives and interface labels are
usually copied from the base game. Sending them to Google produces a second,
worse translation of a string the game already translates well — the player
sees two different renderings of the same concept.

This module indexes `<english value> → <official translation>` from a vanilla
installation (or from any other already-translated mod) and lets the pipeline
take that translation instead of calling an engine.
"""
import os
from typing import Dict, List, Optional

from .config import logger
from .localisation import (
    get_hoi4_language_name,
    normalize_key,
    read_localisation_lines,
    split_value_line,
)

#: Values shorter than this are markers, numbers or single letters; matching
#: them would replace half the mod with the wrong string.
MIN_USEFUL_LENGTH = 3


def _language_suffix(language: str) -> str:
    return f"_l_{get_hoi4_language_name(language).casefold()}.yml"


def read_values(path: str) -> Dict[str, str]:
    """Parse one localisation file into ``key → value``."""
    values: Dict[str, str] = {}
    try:
        lines, _encoding = read_localisation_lines(path)
    except (OSError, ValueError) as error:
        logger.warning("Ванильный файл пропущен (%s): %s", path, error)
        return values
    for line in lines:
        parts = split_value_line(line.rstrip("\r\n"))
        if parts is None:
            continue
        # KEY:0 and KEY:3 are the same key; the game's own files are full of
        # revision numbers, so they must not decide whether a line matches.
        values[normalize_key(parts[0])] = parts[1]
    return values


def _iter_language_files(root: str, language: str) -> List[str]:
    suffix = _language_suffix(language)
    found: List[str] = []
    for directory, _subdirs, files in os.walk(root):
        for name in files:
            if name.casefold().endswith(suffix):
                found.append(os.path.join(directory, name))
    return sorted(found)


class VanillaIndex:
    """Maps a source-language value to the game's own translation of it."""

    def __init__(self, translations: Dict[str, str], origin: str = ""):
        self._translations = translations
        self.origin = origin
        self.conflicts = 0

    def __len__(self) -> int:
        return len(self._translations)

    def __bool__(self) -> bool:
        return bool(self._translations)

    def lookup(self, value: str) -> Optional[str]:
        """Return the vanilla translation of `value`, if the game has one."""
        if not value:
            return None
        found = self._translations.get(value)
        if found is None:
            found = self._translations.get(value.strip())
        return found or None

    @classmethod
    def build(cls, root: str, source_lang: str, target_lang: str,
              log_cb=None) -> "VanillaIndex":
        """Index `root` (vanilla `localisation` folder or a mod root)."""
        source_files = _iter_language_files(root, source_lang)
        target_files = _iter_language_files(root, target_lang)
        if not source_files or not target_files:
            if log_cb:
                log_cb(
                    "Ванильные переводы: не найдены файлы на обоих языках "
                    f"({source_lang} / {target_lang}); переиспользование выключено",
                    'warning',
                )
            return cls({})

        translated: Dict[str, str] = {}
        conflicts = 0
        target_suffix = _language_suffix(target_lang)
        target_by_name = {
            os.path.basename(path).replace(target_suffix, "").casefold(): path
            for path in target_files
        }

        for source_path in source_files:
            # Prefer the same file name in the target language: the same key
            # means different things in different files only in broken mods,
            # but joining by file first keeps the index honest.
            key = os.path.basename(source_path).replace(
                _language_suffix(source_lang), ""
            ).casefold()
            target_path = target_by_name.get(key)
            if target_path is None:
                continue
            source_values = read_values(source_path)
            target_values = read_values(target_path)
            if not source_values or not target_values:
                continue
            for key_name, source_value in source_values.items():
                target_value = target_values.get(key_name)
                if not target_value:
                    continue
                if len(source_value) < MIN_USEFUL_LENGTH or source_value == target_value:
                    continue
                existing = translated.get(source_value)
                if existing is not None and existing != target_value:
                    conflicts += 1
                    continue
                translated[source_value] = target_value

        index = cls(translated, origin=root)
        index.conflicts = conflicts
        if log_cb:
            if index:
                log_cb(
                    f"Ванильные переводы: {len(index)} готовых строк "
                    f"из {len(source_files)} файлов",
                    'info',
                )
            else:
                log_cb(
                    "Ванильные переводы: совпадений не найдено "
                    "(проверьте путь к папке localisation и пару языков)",
                    'warning',
                )
            if conflicts:
                log_cb(
                    f"Ванильные переводы: {conflicts} строк имеют разный официальный "
                    "перевод в разных файлах; взят первый вариант",
                    'info',
                )
        return index


def build_vanilla_index(path: Optional[str], source_lang: str, target_lang: str,
                        log_cb=None) -> Optional[VanillaIndex]:
    """Build an index when a path is given, otherwise return None."""
    if not path:
        return None
    if not os.path.isdir(path):
        if log_cb:
            log_cb(f"Ванильные переводы: папка не найдена: {path}", 'warning')
        return None
    return VanillaIndex.build(path, source_lang, target_lang, log_cb=log_cb)


__all__ = ["VanillaIndex", "build_vanilla_index", "read_values"]
