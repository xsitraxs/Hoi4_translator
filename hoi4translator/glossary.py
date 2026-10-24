"""Term glossary: force the same translation for chosen words.

Machine engines translate the same term differently in different sentences —
"Division" becomes «дивизия», «подразделение», «отдел» depending on context.
A glossary pins the terms that matter (country names, unit types, ideologies)
so the mod reads consistently.

The glossary does not ask an engine to respect the terms: it replaces every
occurrence with a placeholder before the request and writes the chosen
translation back afterwards. An engine therefore cannot translate, reorder or
decline a glossary term, which is what makes the guarantee absolute.
"""
import csv
import io
import json
import os
import re
from typing import Dict, Iterable, List, Optional, Pattern, Tuple

from .config import logger


class GlossaryError(ValueError):
    """The glossary file could not be read as a list of term pairs."""


def _clean_pair(source: str, target: str) -> Optional[Tuple[str, str]]:
    source = (source or "").strip()
    target = (target or "").strip()
    if not source or not target:
        return None
    if source.startswith("#"):
        return None
    return source, target


def _sniff_delimiter(sample: str) -> str:
    """Pick the delimiter a term file uses; tab wins when several appear."""
    counts = {delimiter: sample.count(delimiter) for delimiter in ("\t", ";", "=")}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count else "\t"


class Glossary:
    """A source → target term map, matched case-insensitively."""

    def __init__(self, pairs: Iterable[Tuple[str, str]], origin: str = ""):
        self._terms: Dict[str, str] = {}
        for source, target in pairs:
            cleaned = _clean_pair(source, target)
            if cleaned is None:
                continue
            source, target = cleaned
            self._terms[source.casefold()] = target
        self.origin = origin
        # Longest term first, so "Soviet Union" wins over "Soviet".
        self._ordered = sorted(self._terms, key=len, reverse=True)
        self._pattern: Optional[Pattern] = None

    # --- construction ---------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "Glossary":
        """Read a glossary from TSV/CSV/`key=value`/JSON.

        Accepted layouts (one pair per line, `#` starts a comment):
        ``Term<TAB>Перевод``, ``Term;Перевод``, ``Term=Перевод`` or a JSON
        object ``{"Term": "Перевод"}``.
        """
        if not path:
            raise GlossaryError("Не указан файл глоссария")
        if not os.path.isfile(path):
            raise GlossaryError(f"Файл глоссария не найден: {path}")
        try:
            with open(path, encoding="utf-8-sig", newline="") as stream:
                content = stream.read()
        except OSError as error:
            raise GlossaryError(f"Не удалось прочитать глоссарий: {error}") from error
        except UnicodeDecodeError as error:
            raise GlossaryError(
                "Глоссарий должен быть в UTF-8 (пересохраните файл в UTF-8)"
            ) from error

        pairs = cls._parse(content, path)
        glossary = cls(pairs, origin=os.path.basename(path))
        if not glossary:
            raise GlossaryError(
                f"В глоссарии нет ни одной пары «термин → перевод»: {os.path.basename(path)}"
            )
        return glossary

    @staticmethod
    def _parse(content: str, path: str) -> List[Tuple[str, str]]:
        if path.casefold().endswith(".json"):
            try:
                data = json.loads(content)
            except json.JSONDecodeError as error:
                raise GlossaryError(f"Некорректный JSON в глоссарии: {error}") from error
            if not isinstance(data, dict):
                raise GlossaryError("JSON-глоссарий должен быть объектом {\"термин\": \"перевод\"}")
            return [(str(source), str(target)) for source, target in data.items()]

        lines = [line for line in content.splitlines() if line.strip()]
        sample = lines[0] if lines else ""
        delimiter = _sniff_delimiter(sample)
        pairs: List[Tuple[str, str]] = []
        for row in csv.reader(io.StringIO(content), delimiter=delimiter):
            if not row:
                continue
            if len(row) < 2:
                # A bare line without a delimiter is a typo, not a pair.
                if row[0].strip() and not row[0].lstrip().startswith("#"):
                    raise GlossaryError(
                        "Строка глоссария без разделителя (нужен таб, «;» или «=»): "
                        f"{row[0][:60]}"
                    )
                continue
            pairs.append((row[0], row[1]))
        return pairs

    # --- querying -------------------------------------------------------
    def __len__(self) -> int:
        return len(self._terms)

    def __bool__(self) -> bool:
        return bool(self._terms)

    @property
    def pattern(self) -> Optional[Pattern]:
        """Regex that matches any term on word boundaries, longest first."""
        if self._pattern is None and self._terms:
            alternatives = "|".join(re.escape(term) for term in self._ordered)
            self._pattern = re.compile(
                rf"(?<![\w])(?:{alternatives})(?![\w])", re.IGNORECASE
            )
        return self._pattern

    def lookup(self, matched_text: str) -> str:
        """Return the pinned translation for an already matched term."""
        return self._terms.get(matched_text.casefold(), matched_text)

    def fingerprint(self) -> str:
        """Short stable id of the glossary contents, used in logs and reports."""
        import hashlib

        joined = "\n".join(f"{term}\t{value}" for term, value in sorted(self._terms.items()))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def load_glossary(path: Optional[str]) -> Optional[Glossary]:
    """Load a glossary if a path is given, otherwise return None."""
    if not path:
        return None
    glossary = Glossary.load(path)
    logger.info("Глоссарий загружен: %s (%d терминов)", glossary.origin, len(glossary))
    return glossary


__all__ = ["Glossary", "GlossaryError", "load_glossary"]
