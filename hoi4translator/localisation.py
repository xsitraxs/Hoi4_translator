"""HOI4 localisation format: parsing, language databases and output paths."""
import hashlib
import os
import re
from typing import Dict, List, Tuple

from .config import logger

# HOI4 requires UTF-8 with BOM, but many older mods are shipped in single-byte
# Windows code pages. Try UTF-8 first and only then the usual suspects, so a
# cp1251 mod is translated instead of crashing the whole batch.
SOURCE_ENCODINGS = ("utf-8-sig", "cp1251", "cp1252", "latin-1")


def read_localisation_lines(path: str, log_cb=None) -> Tuple[List[str], str]:
    """Read a localisation file, detecting its encoding.

    Returns the lines and the encoding that decoded them. latin-1 always
    succeeds, so the caller is told when a fallback was used.
    """
    for encoding in SOURCE_ENCODINGS:
        try:
            with open(path, encoding=encoding, newline="") as stream:
                lines = stream.readlines()
        except UnicodeDecodeError:
            continue
        if encoding != "utf-8-sig" and log_cb is not None:
            log_cb(
                f"  Файл прочитан как {encoding} (не UTF-8); результат будет "
                "пересохранён в UTF-8 BOM",
                "warning",
            )
        return lines, encoding

    raise ValueError(
        f"Не удалось прочитать {os.path.basename(path)}: кодировка не определена "
        f"({', '.join(SOURCE_ENCODINGS)}). Пересохраните файл в UTF-8."
    )


def normalize_key(prefix: str) -> str:
    """Localisation key without its revision number.

    ``KEY:0`` and ``KEY:12`` are the same key: the number after the colon only
    tracks revisions, and the game treats two such lines as duplicates. Comparing
    raw prefixes would miss that, so every key comparison uses this form.
    """
    return re.sub(r':\d*$', '', prefix.strip())


def source_text_hash(text: str) -> str:
    """Stable fingerprint of a source value, used to detect edited source lines."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Two section-sign forms in real files are not "a broken colour code":
#   * `§[...]` -- a dynamic colour taken from a game expression
#     (`§[IRE_loc_sf_ideology]Sinn Féin§!`) -- matched as one unit so the
#     expression is protected from the engine like any other placeholder;
#   * `§§` -- a literal section sign (a US legal citation in a Millennium Dawn
#     event), not a colour start at all.
PLACEHOLDER_RE = re.compile(
    r'(\$\$|\$[^$]+\$|\[[^\]]*\]|§\[[^\]]*\]|§§|§[A-Za-z0-9!]|\\+.|\\{2,}|£\w+(?:\|\d+)?|@\w+\[!?\]?)'
)

# HOI4 keys are ASCII identifiers; the version is optional and numeric only.
VALUE_PREFIX_RE = re.compile(r'^(\s*[A-Za-z0-9_.-]+:\d*\s*)')

# Kept for callers that only ask "is this a key/value line?" and never look at
# the groups; split_value_line() is the parser that survives real files.
VALUE_RE = re.compile(
    r'^(\s*[A-Za-z0-9_.-]+:\d*\s*)"((?:[^"\\]|\\.)*)"(.*)$'
)


# HOI4 permits trailing whitespace and line comments, but not arbitrary tokens
# after the closing quote.
VALUE_SUFFIX_RE = re.compile(r'^[ \t]*(?:#.*)?$')


def _is_escaped(text: str, index: int) -> bool:
    """True if the character at `index` is preceded by an odd number of backslashes."""
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def split_value_line(content: str):
    """Split ``KEY:0 "value"`` into ``(prefix, value, suffix)``, else ``None``.

    The value runs to the last quote that leaves only a comment or whitespace
    behind it, which is how the game reads a localisation line. That matters
    because real files are full of quotes that were never escaped:
    ``traits_l_english.yml`` (a vanilla file, shipped by several mods) has
    ``"§L"Shoot them again, they're not dead yet!"§!"``. Stopping at the first
    quote -- what a plain regex does -- cuts that value in half, and rejecting
    the line throws away the whole file. Scanning from the right and accepting
    only a quote whose suffix is a comment also keeps ``# a "quoted" comment``
    out of the value, and still reports a value that is never closed.
    """
    match = VALUE_PREFIX_RE.match(content)
    if not match:
        return None
    start = match.end()
    if content[start:start + 1] != '"':
        return None
    for position in range(len(content) - 1, start, -1):
        if content[position] != '"' or _is_escaped(content, position):
            continue
        suffix = content[position + 1:]
        if VALUE_SUFFIX_RE.fullmatch(suffix):
            return match.group(1), content[start + 1:position], suffix
    return None
    start = match.end()
    if content[start:start + 1] != '"':
        return None
    position = start + 1
    length = len(content)
    while position < length:
        char = content[position]
        if char == "\\":
            position += 2
            continue
        if char == '"':
            return match.group(1), content[start + 1:position], content[position + 1:]
        position += 1
    return None
LANGUAGE_HEADER_RE = re.compile(r'^[ \t]*l_([A-Za-z0-9_]+):[ \t]*(?:#.*)?$', re.IGNORECASE)

# Short translator code -> one of HOI4's built-in localization databases.
HOI4_LANGUAGE_NAMES = {
    "en": "english", "fr": "french", "de": "german", "es": "spanish",
    "pt": "braz_por", "pl": "polish", "ru": "russian", "ja": "japanese",
    "zh-CN": "simp_chinese", "ko": "korean",
}
HOI4_LANGUAGE_DIRECTORIES = {name.casefold() for name in HOI4_LANGUAGE_NAMES.values()}
HOI4_LANGUAGE_NAMES_BY_CODE = {
    code.casefold(): name for code, name in HOI4_LANGUAGE_NAMES.items()
}
# Display order is kept separate from the translator's language codes so the
# output target can only be one of the game's built-in localization databases.
HOI4_TARGET_LANGS = [
    ("en", "Английский"), ("fr", "Французский"), ("de", "Немецкий"),
    ("es", "Испанский"), ("pt", "Бразильский португальский"),
    ("pl", "Польский"), ("ru", "Русский"), ("ja", "Японский"),
    ("zh-CN", "Упрощённый китайский"), ("ko", "Корейский"),
]


def get_hoi4_language_name(target_lang: str) -> str:
    """Return the internal HOI4 database name or reject an unsupported target."""
    if isinstance(target_lang, str):
        language_name = HOI4_LANGUAGE_NAMES_BY_CODE.get(target_lang.casefold())
        if language_name:
            return language_name
    supported = ", ".join(HOI4_LANGUAGE_NAMES)
    raise ValueError(
        f"Неподдерживаемый целевой язык HOI4: {target_lang!r}. "
        f"Доступны: {supported}."
    )


SOURCE_LANGUAGE_NAMES = {
    **HOI4_LANGUAGE_NAMES,
    "uk": "ukrainian", "it": "italian", "tr": "turkish",
    "cs": "czech", "hu": "hungarian", "ro": "romanian",
}
SOURCE_LANGUAGE_NAMES_BY_CODE = {
    code.casefold(): name for code, name in SOURCE_LANGUAGE_NAMES.items()
}


def _split_line_ending(line: str):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1:]
    return line, ""


def _prepare_target_localisation_lines(lines, target_name, source_lang, log_cb):
    """Return a single-target-language file with the required first-line header.

    When a valid source file contains several language databases, keep only the
    database matching the selected source language rather than translating all
    of them into duplicate target keys.
    """
    header_rows = []
    for index, line in enumerate(lines):
        content, _ = _split_line_ending(line)
        match = LANGUAGE_HEADER_RE.fullmatch(content)
        if match:
            header_rows.append((index, match.group(1).casefold()))

    distinct_languages = list(dict.fromkeys(language for _, language in header_rows))
    selected_source_name = SOURCE_LANGUAGE_NAMES_BY_CODE.get(
        source_lang.casefold() if isinstance(source_lang, str) else ""
    )
    if len(distinct_languages) == 1 and selected_source_name:
        actual_source_name = distinct_languages[0]
        if selected_source_name != actual_source_name:
            log_cb(
                f"  Файл пропущен: заголовок l_{actual_source_name} не совпадает "
                f"с выбранным исходным языком {source_lang!r}; перевод из неверного "
                "исходного языка небезопасен",
                "error",
            )
            return None

    if len(distinct_languages) > 1:
        source_name = selected_source_name
        if source_name not in distinct_languages:
            found = ", ".join(f"l_{name}" for name in distinct_languages)
            requested = source_lang or "не задан"
            log_cb(
                f"  Файл содержит несколько языковых баз ({found}), "
                f"но выбранный исходный язык {requested!r} не совпадает ни с одной; "
                "файл пропущен",
                "error",
            )
            return None

        first_header = header_rows[0][0]
        selected_blocks = []
        for row_number, (start, language) in enumerate(header_rows):
            stop = header_rows[row_number + 1][0] if row_number + 1 < len(header_rows) else len(lines)
            if language == source_name:
                selected_blocks.extend(lines[start:stop])
        lines = list(lines[:first_header]) + selected_blocks
        log_cb(
            f"  В файле несколько языковых баз; оставлена l_{source_name} "
            f"(исходный язык {source_lang})",
            "info",
        )

    output = []
    header_indexes = set()
    structure_changed = False
    for index, line in enumerate(lines):
        content, ending = _split_line_ending(line)
        if LANGUAGE_HEADER_RE.fullmatch(content):
            output.append(f"l_{target_name}:{ending}")
            header_indexes.add(index)
            if content != f"l_{target_name}:":
                structure_changed = True
        else:
            output.append(line)

    if 0 not in header_indexes:
        line_ending = next(
            (ending for _, ending in (_split_line_ending(line) for line in output) if ending),
            "\n",
        )
        output.insert(0, f"l_{target_name}:{line_ending}")
        header_indexes = {0, *(index + 1 for index in header_indexes)}
        structure_changed = True

    return output, header_indexes, structure_changed


def load_existing_translations(dst_path: str, log_cb=None) -> Dict[str, str]:
    """Load non-empty values from an existing target-language file."""
    existing = {}
    if not os.path.exists(dst_path):
        return existing
    try:
        lines, _encoding = read_localisation_lines(dst_path, log_cb)
        for line in lines:
            content, _ = _split_line_ending(line)
            match = VALUE_RE.fullmatch(content)
            if (
                match
                and VALUE_SUFFIX_RE.fullmatch(match.group(3))
                and match.group(2).strip()
            ):
                existing[normalize_key(match.group(1))] = match.group(2)
    except Exception as e:
        logger.error("Error reading existing translations (%s): %s", dst_path, e)
    return existing


def rename_dst(src_path: str, src_root: str, dst_root: str, target_lang: str = "ru") -> str:
    """Build an output path using a valid HOI4 language directory and suffix."""
    rel = os.path.relpath(src_path, src_root)
    target_name = get_hoi4_language_name(target_lang)
    parent, filename = os.path.split(rel)
    stem, extension = os.path.splitext(filename)
    if extension.casefold() != ".yml":
        raise ValueError("Локализационные файлы HOI4 должны иметь расширение .yml")

    # Replace a previous internal-language suffix, or add one if the input is
    # a generic .yml name. HOI4 uses the language suffix to decide its database.
    stem = re.sub(r'_l_[a-z0-9_-]+$', '', stem, flags=re.IGNORECASE)
    filename = f"{stem}_l_{target_name}.yml"
    rel_renamed = os.path.join(parent, filename) if parent else filename

    # Folder names are organizational only, but update recognized language
    # directories to keep the conventional mod layout.
    parts = re.split(r'([\\/])', rel_renamed)
    for index, part in enumerate(parts):
        if part.casefold() in HOI4_LANGUAGE_DIRECTORIES:
            parts[index] = target_name
    return os.path.join(dst_root, ''.join(parts))

def validate_directory_relationships(src_root: str, dst_root: str) -> None:
    """Reject equal or nested source/output roots to prevent reprocessing or overwrites."""
    source = os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(src_root))))
    destination = os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(dst_root))))

    def contains(parent: str, child: str) -> bool:
        try:
            return os.path.normcase(os.path.commonpath((parent, child))) == parent
        except ValueError:
            # Different Windows drives cannot contain one another.
            return False

    if contains(source, destination) or contains(destination, source):
        raise ValueError(
            "Исходная папка и папка перевода пересекаются. "
            "Выберите отдельные соседние каталоги, чтобы не перезаписать исходники "
            "и не обрабатывать результаты повторно."
        )


def collect_yml_files(root: str) -> List[str]:
    res = []
    if not os.path.exists(root):
        return res
    for d, _, fns in os.walk(root):
        for f in sorted(fns):
            if f.endswith('.yml') and f.lower() != 'languages.yml':
                res.append(os.path.join(d, f))
    return res


def validate_output_path_collisions(source_destination_pairs: List[Tuple[str, str]]) -> None:
    """Reject distinct source files that would overwrite the same output path."""
    destinations: Dict[str, List[Tuple[str, str]]] = {}
    for source_path, destination_path in source_destination_pairs:
        normalized = os.path.normcase(
            os.path.realpath(os.path.abspath(os.path.expanduser(destination_path)))
        ).casefold()
        destinations.setdefault(normalized, []).append((source_path, destination_path))

    collisions = []
    for entries in destinations.values():
        unique_sources = list(dict.fromkeys(source for source, _ in entries))
        if len(unique_sources) > 1:
            collisions.append((entries[0][1], unique_sources))

    if collisions:
        details = "; ".join(
            f"{destination} ← {', '.join(sources)}"
            for destination, sources in collisions[:10]
        )
        remaining = len(collisions) - min(len(collisions), 10)
        more = f"; и ещё {remaining} коллизий" if remaining else ""
        raise ValueError(f"Коллизия выходных файлов: {details}{more}")
