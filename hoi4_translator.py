import asyncio
import getpass
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# Keep in sync with the git tag of a release (CI compares them).
__version__ = "1.3.8"

# Попытка импорта асинхронных библиотек для ускорения
try:
    import aiohttp
    ASYNC_OK = True
except ImportError:
    ASYNC_OK = False

try:
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    TRANSLATOR_OK = True
except ImportError:
    GoogleTranslator = None
    MyMemoryTranslator = None
    TRANSLATOR_OK = False

# deep-translator's DeepL wrapper still uses the retired GET/auth_key API.
# Use DeepL's official client, which sends POST requests with Authorization.
try:
    import deepl
    DEEPL_OK = True
except ImportError:
    deepl = None
    DEEPL_OK = False

# Попытка импорта JPype для Java-интеграции
# FIX #8: jpype.types импортируется внутри try, иначе крэш при старте если jpype не установлен
try:
    import jpype
    import jpype.imports
    JAVA_OK = True
except ImportError:
    JAVA_OK = False

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ И ЛОГИРОВАНИЕ
# ---------------------------------------------------------------------------

APP_NAME = "Hoi4Translator"


def get_app_data_dir() -> str:
    """Return a persistent, per-user data directory on each supported OS.

    Never raises: a windowed build has no console, so an exception raised while
    preparing the data directory would silently kill the whole application.
    """
    home = os.path.expanduser("~")
    candidates = []
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.append(os.path.join(base, APP_NAME))
    elif sys.platform == "darwin":
        candidates.append(os.path.join(home, "Library", "Application Support", APP_NAME))
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME", "")
        base = xdg_data_home if xdg_data_home and os.path.isabs(xdg_data_home) \
            else os.path.join(home, ".local", "share")
        candidates.append(os.path.join(base, APP_NAME))

    # Last-resort per-user location (still persistent, unlike PyInstaller's _MEIPASS).
    candidates.append(os.path.join(home, ".hoi4_translator"))

    for directory in candidates:
        try:
            os.makedirs(directory, exist_ok=True)
            return directory
        except OSError:
            continue
    return tempfile.gettempdir()


APP_DATA_DIR = get_app_data_dir()
LOG_FILE = os.path.join(APP_DATA_DIR, "hoi4_translator.log")
DB_FILE = os.path.join(APP_DATA_DIR, "translation_cache.db")
SETTINGS_FILE = os.path.join(APP_DATA_DIR, "settings.json")
LEGACY_SETTINGS_FILE = os.path.join(
    os.path.expanduser("~"), ".hoi4_translator_settings.json"
)
# Bump whenever translation/post-processing semantics change so stale cache
# entries cannot bypass newer validation or typography rules.
CACHE_FORMAT_VERSION = 2

try:
    import keyring
except ImportError:
    keyring = None

def _build_log_handlers():
    """Rotating file handler (with a console mirror) that degrades gracefully."""
    handlers = [logging.StreamHandler()]
    try:
        handlers.insert(
            0,
            RotatingFileHandler(
                LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
            ),
        )
    except OSError:
        # A read-only or missing data directory must not prevent startup.
        pass
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_build_log_handlers(),
)
logger = logging.getLogger(__name__)


def _fsync_directory(directory: str) -> None:
    """Persist the directory entry created by os.replace on POSIX systems."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: str, write_content, encoding: str) -> None:
    """Write a sibling temporary file and atomically replace the destination."""
    destination = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(destination) or os.curdir
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            descriptor = -1
            write_content(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.exists(destination):
            try:
                os.chmod(temporary, os.stat(destination).st_mode & 0o777)
            except OSError:
                pass
        os.replace(temporary, destination)
        _fsync_directory(parent)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    _atomic_write(path, lambda stream: stream.write(text), encoding)


def atomic_write_lines(path: str, lines) -> None:
    _atomic_write(path, lambda stream: stream.writelines(lines), "utf-8-sig")


# ---------------------------------------------------------------------------
# ЧТЕНИЕ ФАЙЛОВ И МЕЛКИЕ УТИЛИТЫ
# ---------------------------------------------------------------------------

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


def source_text_hash(text: str) -> str:
    """Stable fingerprint of a source value, used to detect edited source lines."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def zip_equal(*iterables):
    """zip() that refuses to silently drop the tail of a longer sequence."""
    lengths = [len(item) for item in iterables]
    if len(set(lengths)) > 1:
        raise ValueError(f"Несовпадение длин последовательностей: {lengths}")
    return zip(*iterables)


# ---------------------------------------------------------------------------
# СБОРКА JavaSim
# ---------------------------------------------------------------------------

JAVA_SOURCE_RELATIVE = os.path.join("java", "com", "hoi4", "translator", "HoI4Translator.java")
BUNDLED_JAR_NAMES = ("hoi4_translator.jar", "HoI4Translator.jar")


def build_java_jar(source_root: str, output_jar: str) -> Optional[str]:
    """Compile the bundled Java service into a JAR.

    Returns the JAR path, or None when no JDK is installed or compilation
    fails. The JAR is intentionally not committed: a binary can silently drift
    away from java/, and CI builds it for releases.
    """
    source_file = os.path.join(source_root, JAVA_SOURCE_RELATIVE)
    if not os.path.isfile(source_file):
        return None
    javac = shutil.which("javac")
    jar_tool = shutil.which("jar")
    if not javac or not jar_tool:
        logger.info("JDK не найден: JAR для JavaSim не будет собран автоматически")
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="hoi4-java-") as classes:
            compiled = subprocess.run(
                [javac, "-d", classes, source_file],
                capture_output=True, text=True, timeout=180,
            )
            if compiled.returncode != 0:
                logger.warning(
                    "Не удалось скомпилировать JavaSim: %s",
                    (compiled.stderr or "").strip()[:500],
                )
                return None
            os.makedirs(os.path.dirname(output_jar) or ".", exist_ok=True)
            packaged = subprocess.run(
                [jar_tool, "cf", output_jar, "-C", classes, "com"],
                capture_output=True, text=True, timeout=120,
            )
            if packaged.returncode != 0:
                logger.warning(
                    "Не удалось упаковать JAR JavaSim: %s",
                    (packaged.stderr or "").strip()[:500],
                )
                return None
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Сборка JAR JavaSim прервана: %s", error)
        return None

    logger.info("JAR JavaSim собран: %s", output_jar)
    return output_jar


def resolve_java_jar(configured: Optional[str], base_dir: Optional[str] = None) -> Optional[str]:
    """Find the JavaSim JAR: explicit path, bundled copy, or build from source."""
    if configured and os.path.exists(configured):
        return configured

    if base_dir is None:
        # PyInstaller unpacks bundled files into a temporary directory.
        base_dir = (
            sys._MEIPASS if hasattr(sys, "_MEIPASS")
            else os.path.dirname(os.path.abspath(__file__))
        )
    for name in BUNDLED_JAR_NAMES:
        candidate = os.path.join(base_dir, name)
        if os.path.exists(candidate):
            return candidate

    built_jar = os.path.join(APP_DATA_DIR, "hoi4_translator.jar")
    source_file = os.path.join(base_dir, JAVA_SOURCE_RELATIVE)
    if os.path.exists(built_jar) and os.path.isfile(source_file):
        try:
            if os.path.getmtime(built_jar) >= os.path.getmtime(source_file):
                return built_jar
        except OSError:
            pass
    return build_java_jar(base_dir, built_jar)


# ---------------------------------------------------------------------------
# КОНСТАНТЫ И РЕГУЛЯРКИ
# ---------------------------------------------------------------------------

# Tokens that must survive machine translation byte-for-byte.  In particular,
# HOI4 uses numeric gradient colours (§0..§9), icon frame suffixes (£icon|1),
# doubled dollars ($$), and backslash escapes such as \" and \n.
PLACEHOLDER_RE = re.compile(
    r'(\$\$|\$[^$]+\$|\[[^\]]*\]|§[A-Za-z0-9!]|\\+.|\\{2,}|£\w+(?:\|\d+)?|@\w+\[!?\]?)'
)

# HOI4 keys are ASCII identifiers; the version is optional and numeric only.
# The value parser understands escaped quotes and keeps comments/suffixes intact.
VALUE_RE = re.compile(
    r'^(\s*[A-Za-z0-9_.-]+:\d*\s*)"((?:[^"\\]|\\.)*)"(.*)$'
)
# HOI4 permits trailing whitespace and line comments, but not arbitrary tokens
# after the closing quote.
VALUE_SUFFIX_RE = re.compile(r'^[ \t]*(?:#.*)?$')
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


# Лимит параллельных запросов к переводчику (защита от бана)
SEMAPHORE_LIMIT = 15


class TranslationCancelled(Exception):
    """Raised when the user stops the active file translation."""


class TranslationFailed(Exception):
    """Raised when every configured translation engine fails for a string."""


class FileProcessStatus(Enum):
    """Non-error file-processing outcomes that are not successful writes."""

    PREVIEW_CANCELLED = "preview_cancelled"


FileProcessResult = Union[int, FileProcessStatus, None]


# ---------------------------------------------------------------------------
# 5. ПОСТОБРАБОТКА ПЕРЕВОДА
# ---------------------------------------------------------------------------

class PostProcessor:
    """Apply conservative, target-language-aware typography fixes."""

    _DOUBLE_SPACE_RE = re.compile(r'  +')
    _SPACE_BEFORE_PUNCT_RE = re.compile(r' +([,;:!?])')
    _FRENCH_SPACE_BEFORE_COMMA_RE = re.compile(r' +,')
    _FRENCH_SPACE_BEFORE_PUNCT_RE = re.compile(r' +([;:!?])')
    _QUOTED_PHRASE_RE = re.compile(r'"([^"\\]+)"')
    # Only trim horizontal spaces around the literal HOI4 escape; \s would
    # also consume physical CR/LF characters and hide a malformed translation.
    _SPACE_AROUND_NEWLINE_RE = re.compile(r'[ \t]*\\n[ \t]*')
    _LEFTOVER_PH_RE = re.compile(r'<<PH\d+>>')
    _QUOTE_STYLES = {
        "en": ("“", "”", ""), "fr": ("«", "»", "\u202f"),
        "de": ("„", "“", ""), "es": ("«", "»", ""),
        "pt": ("“", "”", ""), "pl": ("„", "”", ""),
        "ru": ("«", "»", ""), "ja": ("「", "」", ""),
        "zh-cn": ("「", "」", ""), "ko": ("“", "”", ""),
    }

    @classmethod
    def process(cls, text: str, target_lang: str = "ru") -> str:
        """Normalize basic punctuation without editing HOI4 tokens or casing."""
        if not text:
            return text

        # HOI4 newline escapes are literal two-character sequences in a value.
        text = cls._SPACE_AROUND_NEWLINE_RE.sub(r'\\n', text)

        tokens = []
        marker_prefix = "\uE000HOI4PP_"
        while marker_prefix in text:
            marker_prefix += "_"

        def protect_token(match):
            index = len(tokens)
            tokens.append(match.group(0))
            return f"{marker_prefix}{index}\uE001"

        protected = PLACEHOLDER_RE.sub(protect_token, text)
        language = target_lang.replace("_", "-").casefold()
        quote_open, quote_close, quote_space = cls._QUOTE_STYLES.get(
            language, ("“", "”", "")
        )
        protected = cls._QUOTED_PHRASE_RE.sub(
            lambda match: (
                f"{quote_open}{quote_space}{match.group(1)}"
                f"{quote_space}{quote_close}"
            ),
            protected,
        )
        protected = cls._DOUBLE_SPACE_RE.sub(' ', protected)

        if language == "fr":
            protected = cls._FRENCH_SPACE_BEFORE_COMMA_RE.sub(',', protected)
            protected = cls._FRENCH_SPACE_BEFORE_PUNCT_RE.sub(
                lambda match: "\u202f" + match.group(1), protected
            )
        else:
            protected = cls._SPACE_BEFORE_PUNCT_RE.sub(r'\1', protected)
        protected = protected.strip(' ')

        marker_re = re.compile(re.escape(marker_prefix) + r'(\d+)\uE001')
        protected = marker_re.sub(
            lambda match: tokens[int(match.group(1))]
            if int(match.group(1)) < len(tokens) else match.group(0),
            protected,
        )
        return cls._LEFTOVER_PH_RE.sub('', protected)


# ---------------------------------------------------------------------------
# 6. ВАЛИДАТОР ПЛЕЙСХОЛДЕРОВ
# ---------------------------------------------------------------------------

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




def validate_preview_translations(originals: List[str], translations: List[str]) -> List[str]:
    """Return preview rows that break the one-line value format or game tokens."""
    if len(originals) != len(translations):
        return [
            f"Количество строк изменилось: ожидалось {len(originals)}, получено {len(translations)}"
        ]

    problems = []
    for row_number, (original, translated) in enumerate(
        zip_equal(originals, translations), start=1
    ):
        issues = []
        if "\n" in translated or "\r" in translated:
            issues.append("значение должно занимать одну физическую строку; используйте \\n")
        if _has_unescaped_quote(translated):
            issues.append('внутренние кавычки нужно экранировать как \\"')
        issues.extend(PlaceholderValidator.validate(original, translated))
        if issues:
            problems.append(f"Строка {row_number}: {', '.join(issues)}")
    return problems


# ---------------------------------------------------------------------------
# 7. ФОЛБЭК-ЦЕПОЧКА ДВИЖКОВ
# ---------------------------------------------------------------------------

# Порядок фолбэка: если основной движок упал, пробуем следующие
FALLBACK_CHAIN = {
    "Google":   ["MyMemory"],       # Google упал → MyMemory
    "DeepL":    ["Google", "MyMemory"],  # DeepL упал → Google → MyMemory
    "MyMemory": ["Google"],         # MyMemory упал → Google
    "JavaSim":  ["Google", "MyMemory"],
}

# Коды языков, которые понимает каждый провайдер
# ---------------------------------------------------------------------------
# The asynchronous Google path talks to the unofficial endpoint directly, while
# the synchronous fallback goes through deep-translator, which validates the
# language against its own table (it knows "pt" but rejects "pt-BR").
GOOGLE_ENDPOINT_ENGINE = "GoogleEndpoint"
MAX_QUERY_CHARS = 400

# MyMemory only accepts full locales (keys are case-folded before lookup).
_MYMEMORY_LOCALES = {
    "en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES",
    "pl": "pl-PL", "ru": "ru-RU", "uk": "uk-UA", "it": "it-IT",
    "pt": "pt-BR", "zh-cn": "zh-CN", "zh": "zh-CN", "ja": "ja-JP", "ko": "ko-KR",
    "tr": "tr-TR", "cs": "cs-CZ", "hu": "hu-HU", "ro": "ro-RO",
}


def provider_language_code(engine: str, code: str, *, target: bool = True) -> str:
    """Map an application language code to the code a provider expects.

    Every engine has its own dialect of language codes; keeping the mapping in
    one place stops "Brazilian Portuguese" from silently meaning three different
    things on three different code paths.
    """
    if not code:
        return code
    if engine == "DeepL":
        return AsyncTranslator._deepl_language_code(code, target=target)
    normalized = code.replace("_", "-").casefold()
    if engine == "MyMemory":
        return _MYMEMORY_LOCALES.get(normalized, f"{code}-{code.upper()}")
    if engine == GOOGLE_ENDPOINT_ENGINE:
        # braz_por is a separate HOI4 database: ask for the Brazilian variant.
        if target and normalized in {"pt", "pt-br"}:
            return "pt-BR"
        return code
    # deep-translator's GoogleTranslator/MyMemory wrappers validate codes
    # themselves, so plain HOI4 codes are passed through unchanged.
    return code


_PH_MARKER_RE = re.compile(r'<<PH\d+>>')
_SENTENCE_TERMINATORS = ".!?。！？"


def _find_chunk_boundary(text: str, start: int, hard_end: int) -> int:
    """Pick a natural split point before hard_end that never cuts a marker."""
    safe_end = hard_end
    for marker in _PH_MARKER_RE.finditer(text):
        if marker.start() >= hard_end:
            break
        if marker.end() > hard_end:
            safe_end = marker.start() if marker.start() > start else marker.end()
            break

    sentence_boundary = -1
    word_boundary = -1
    for index in range(safe_end - 1, start, -1):
        if not text[index].isspace():
            continue
        if word_boundary < 0:
            word_boundary = index + 1
        previous = index - 1
        while previous >= start and text[previous].isspace():
            previous -= 1
        if previous >= start and text[previous] in _SENTENCE_TERMINATORS:
            sentence_boundary = index + 1
            break

    end = (
        sentence_boundary if sentence_boundary > start
        else word_boundary if word_boundary > start
        else safe_end
    )

    # The chosen boundary must also stay outside every protection marker.
    for marker in _PH_MARKER_RE.finditer(text):
        if marker.start() >= end:
            break
        if marker.end() > end:
            end = marker.start() if marker.start() > start else marker.end()
            break

    if end <= start:
        end = safe_end if safe_end > start else hard_end
    return end


def split_long_text(text: str, limit: int = MAX_QUERY_CHARS) -> List[str]:
    """Split a long value into translatable chunks, never dropping characters.

    Mirrors the Java service so both engines send comparable requests and are
    bounded by the same query length.
    """
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    start = 0
    length = len(text)
    while start < length:
        hard_end = min(start + limit, length)
        if hard_end == length:
            chunks.append(text[start:])
            break
        end = _find_chunk_boundary(text, start, hard_end)
        chunks.append(text[start:end])
        start = end
    return chunks

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

# ---------------------------------------------------------------------------
# JAVA ИНТЕГРАЦИЯ
# ---------------------------------------------------------------------------
class JavaTranslatorService:
    """Thin JPype wrapper around the bundled Java translation service."""

    # The JVM classpath is fixed once the JVM is running: keep the JAR that was
    # used so a later change of the JAR path can be reported honestly.
    _started_jar: Optional[str] = None
    _warned_about_outdated_jar = False

    def __init__(self, jar_path: Optional[str] = None):
        self.java_available = JAVA_OK and jar_path is not None and os.path.exists(jar_path)
        self.jar_path = jar_path
        self._jvm_started = False

        if self.java_available:
            self._start_jvm()
        else:
            if not JAVA_OK:
                logger.warning("JPype не установлен. pip install jpype1")
            elif not jar_path:
                logger.info("Путь к Java JAR не указан.")
            else:
                logger.error(f"Файл JAR не найден: {jar_path}")

    @staticmethod
    def _jvm_library_names() -> Tuple[str, ...]:
        if os.name == "nt":
            return ("jvm.dll",)
        if sys.platform == "darwin":
            return ("libjvm.dylib", "libjvm.so")
        return ("libjvm.so",)

    @classmethod
    def _jvm_candidates(cls, java_home: str) -> List[str]:
        """Build plausible JVM library paths for the current platform."""
        candidates = []
        for sub in ("bin", os.path.join("bin", "server"), os.path.join("bin", "client"),
                    os.path.join("jre", "bin", "server"), "lib", os.path.join("lib", "server")):
            for name in cls._jvm_library_names():
                candidates.append(os.path.join(java_home, sub, name))
        return candidates

    def _resolve_jvm_path(self) -> str:
        """Locate the JVM library, starting with JPype's own cross-platform lookup."""
        candidates: List[str] = []

        # 1. Explicit override (also honoured by JPype itself).
        override = os.environ.get("JPYPE_JVM_PATH")
        if override:
            candidates.append(override)

        # 2. JPype's own detection: correct on every supported platform, and it
        #    already returns a file path (a directory only on exotic setups).
        try:
            default_path = jpype.getDefaultJVMPath()
        except Exception as error:  # pragma: no cover - depends on the JVM install
            logger.debug("jpype.getDefaultJVMPath() failed: %s", error)
        else:
            if default_path:
                if os.path.isdir(default_path):
                    candidates.extend(
                        os.path.join(default_path, name)
                        for name in self._jvm_library_names()
                    )
                else:
                    candidates.append(default_path)

        # 3. Build candidates from JAVA_HOME as a last resort.
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            candidates.extend(self._jvm_candidates(java_home))

        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate

        searched = "; ".join(candidate for candidate in candidates if candidate) or "не найдено"
        raise FileNotFoundError(f"Не найдена библиотека JVM ({searched})")

    def _start_jvm(self):
        """Безопасный запуск JVM с приоритетом JAVA_HOME"""
        if jpype.isJVMStarted():
            self._jvm_started = True
            self._check_jar_matches_running_jvm()
            return

        try:
            jvm_path = self._resolve_jvm_path()

            logger.info(f" Запуск JVM: {jvm_path}")
            jpype.startJVM(jvm_path, classpath=[self.jar_path], convertStrings=False)
            self._jvm_started = True
            JavaTranslatorService._started_jar = self.jar_path
            logger.info("✅ JVM успешно запущен.")
        except Exception as e:
            logger.error(f"❌ Ошибка запуска JVM: {e}")
            self.java_available = False

    def _check_jar_matches_running_jvm(self):
        """Warn when a different JAR was requested but cannot be loaded."""
        started_jar = JavaTranslatorService._started_jar
        if not started_jar or not self.jar_path:
            return
        if (
            os.path.normcase(os.path.abspath(started_jar))
            != os.path.normcase(os.path.abspath(self.jar_path))
            and not JavaTranslatorService._warned_about_outdated_jar
        ):
            JavaTranslatorService._warned_about_outdated_jar = True
            logger.warning(
                "JVM уже запущена с JAR %s; новый JAR %s будет использован только "
                "после перезапуска приложения",
                started_jar, self.jar_path,
            )

    def translate_batch_java(self, texts: List[str], target_lang: str = "ru",
                             source_lang: Optional[str] = None) -> List[str]:
        if not self.java_available or not self._jvm_started:
            raise RuntimeError("JavaSim недоступен: JVM или JAR не загружен")

        try:
            # Пакет и класс должны точно совпадать с тем, что в .java файле
            JTranslator = jpype.JClass("com.hoi4.translator.HoI4Translator")
            translator = JTranslator()
            return [self._translate_one(translator, t, target_lang, source_lang) for t in texts]
        except Exception as e:
            logger.exception("Java translation error")
            raise RuntimeError(f"Ошибка JavaSim: {e}") from e

    @staticmethod
    def _translate_one(translator, text: str, target_lang: str, source_lang: Optional[str]) -> str:
        """Pass the selected source language when the JAR supports it."""
        if source_lang:
            try:
                return str(translator.translate(text, target_lang, source_lang))
            except TypeError:
                # An older JAR only exposes translate(text, targetLang), which
                # asks Google to auto-detect the source language.
                if not JavaTranslatorService._warned_about_outdated_jar:
                    JavaTranslatorService._warned_about_outdated_jar = True
                    logger.warning(
                        "Используется устаревший HoI4Translator.jar: исходный язык "
                        "не передаётся (sl=auto). Пересоберите JAR из java/."
                    )
            except Exception:
                # Any other failure means the overload exists but did not work.
                raise
        return str(translator.translate(text, target_lang))

    def shutdown(self):
        """Release references; the JVM itself cannot be restarted in-process.

        JPype never unloads a started JVM, so this intentionally does not
        pretend to stop it. It only records the state for the logs.
        """
        if self._jvm_started and jpype.isJVMStarted():
            logger.info("JavaSim завершил работу (JVM остаётся запущенной до выхода).")
        self._jvm_started = False
# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ (КЭШИРОВАНИЕ)
# FIX: единое персистентное соединение вместо connect/close на каждый запрос
# ---------------------------------------------------------------------------

class TranslationCache:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # FIX: одно соединение на весь сеанс, check_same_thread=False для многопоточки
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()

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
        try:
            self._conn.close()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# ЛОГИКА ПЕРЕВОДА
# ---------------------------------------------------------------------------

def protect_placeholders(text: str):
    tokens = []
    def replacer(m):
        tokens.append(m.group(0))
        return f'<<PH{len(tokens)-1}>>'

    # 1. Защищаем маркеры
    protected = PLACEHOLDER_RE.sub(replacer, text)
    # 2. Вставляем невидимый разделитель между соседними маркерами, чтобы движок не склеил их в одно слово
    # FIX: \u200B в строке замены re.sub вызывает bad escape — используем lambda
    protected = re.sub(r'(<<PH\d+>>)(<<PH\d+>>)', lambda m: m.group(1) + '\u200B' + m.group(2), protected)
    return protected, tokens

def restore_placeholders(text: str, tokens: List[str]) -> str:
    # Убираем невидимый разделитель
    text = text.replace('\u200B', '')

    def replacer(m):
        try:
            # Извлекаем только цифры, игнорируя пробелы и регистр (<< PH 0 >> -> 0)
            idx_str = re.sub(r'\D', '', m.group(0))
            idx = int(idx_str)
            return tokens[idx] if idx < len(tokens) else m.group(0)
        except Exception:
            return m.group(0)
    #Матчим любой вариант: <<PH0>>, << PH 0 >>, <<ph0>>, <<Ph 0 >> и т.д.
    return re.sub(r'<<\s*ph\s*\d+\s*>>', replacer, text, flags=re.IGNORECASE)

class AsyncTranslator:
    def __init__(self, engine: str, api_key: Optional[str] = None,
                 source_lang: str = "en", java_jar_path: Optional[str] = None,
                 concurrency: Optional[int] = None, reuse_fallback_cache: bool = False,
                 fallback_notifier=None):
        self.engine = engine
        self.api_key = api_key
        self.source_lang = source_lang
        self.session: Optional[aiohttp.ClientSession] = None
        # Concurrency is per run: a worker thread must never mutate a global
        # that other runs are reading.
        self.concurrency = max(1, int(concurrency or SEMAPHORE_LIMIT))
        self.reuse_fallback_cache = reuse_fallback_cache
        self.fallback_notifier = fallback_notifier
        self.fallback_counts: Dict[str, int] = {}
        self._deepl_client = None
        self._deepl_lock = threading.Lock()
        self.cache = TranslationCache(DB_FILE)
        # 👇 Передаём путь в сервис
        self.java_service = JavaTranslatorService(jar_path=java_jar_path)
        self._semaphore: Optional[asyncio.Semaphore] = None
        self.validation_report = ValidationReport()

    async def __aenter__(self):
        if ASYNC_OK:
            connector = aiohttp.TCPConnector(limit=self.concurrency)
            self.session = aiohttp.ClientSession(connector=connector)
        self._semaphore = asyncio.Semaphore(self.concurrency)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
        client = self._deepl_client
        self._deepl_client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception:
                    logger.debug("DeepL client close failed", exc_info=True)
        self.java_service.shutdown()
        self.cache.close()

    def _cache_engines(self) -> List[str]:
        """Engines whose cached translations may answer this request.

        Sharing cache entries across the fallback chain saves traffic, but it
        also means a user who deliberately switched from Google to DeepL keeps
        receiving the old Google text forever. That trade-off is opt-in.
        """
        if self.reuse_fallback_cache:
            return [self.engine, *FALLBACK_CHAIN.get(self.engine, [])]
        return [self.engine]

    def _note_fallback(self, used_engine: str) -> None:
        """Count and report a silent engine substitution (DeepL → Google)."""
        pair = f"{self.engine} → {used_engine}"
        count = self.fallback_counts.get(pair, 0) + 1
        self.fallback_counts[pair] = count
        if self.fallback_notifier is not None:
            try:
                self.fallback_notifier(self.engine, used_engine, count)
            except Exception:
                logger.exception("Fallback notifier failed")

    async def translate_text_async(self, text: str, target_lang: str = "ru", retries: int = 3,
                                    _file_ctx: str = "", _key_ctx: str = "") -> str:
        """
        Переводит одну строку с постобработкой, валидацией и фолбэком.
        _file_ctx / _key_ctx используются только для отчёта валидации.
        """
        if not text or not text.strip():
            return text

        # By default only the selected engine may answer from the cache; see
        # _cache_engines() for why cross-engine reuse is opt-in.
        for cached_engine in self._cache_engines():
            cached = self.cache.get(text, self.source_lang, target_lang, cached_engine)
            if cached is not None:
                cached_issues = validate_preview_translations([text], [cached])
                if cached_issues:
                    logger.warning(
                        "Invalid cached translation ignored (%s): %s",
                        cached_engine, "; ".join(cached_issues),
                    )
                    continue
                logger.debug("Cache hit (%s)", cached_engine)
                return cached

        protected, tokens = protect_placeholders(text)

        clean = re.sub(r'<<PH\d+>>', '', protected).strip()
        if not clean:
            return text

        # --- 7. ФОЛБЭК-ЦЕПОЧКА ---
        result_text = protected
        used_engine = self.engine
        engines_to_try = [self.engine] + FALLBACK_CHAIN.get(self.engine, [])

        async with self._semaphore:
            for eng in engines_to_try:
                try:
                    result_text = await self._call_engine(eng, protected, target_lang, retries)
                    used_engine = eng
                    if used_engine != self.engine:
                        # A paid engine silently degrading to a free one must be
                        # visible in the UI, not only in the log file.
                        logger.info(f"Фолбэк сработал: {self.engine} → {used_engine}")
                        self._note_fallback(used_engine)
                    break
                except Exception as e:
                    logger.warning(f"Движок {eng} упал: {e}. Пробуем следующий...")
                    if eng == engines_to_try[-1]:
                        # Never write untranslated source text as if translation
                        # succeeded; fail the file so the UI reports the outage.
                        logger.error("Все движки недоступны для ключа %s", _key_ctx or "?")
                        raise TranslationFailed(
                            "Все доступные движки перевода завершились ошибкой"
                        ) from e

        # Восстанавливаем плейсхолдеры
        final_text = restore_placeholders(result_text, tokens)

        # --- 6. ВАЛИДАЦИЯ ---
        issues = validate_preview_translations([text], [final_text])
        if issues:
            # A missing token cannot be safely relocated to the end of the
            # sentence. Leave it visible to validation/preview instead.
            logger.warning(f"Битый перевод [{_key_ctx}]: {issues}")

        # --- 5. ПОСТОБРАБОТКА ---
        final_text = PostProcessor.process(final_text, target_lang)
        final_issues = validate_preview_translations([text], [final_text])
        issues = final_issues
        if final_issues:
            # Post-processing must not silently damage a token that was valid
            # immediately after translation/restoration.
            logger.warning(f"Постобработка нарушила формат [{_key_ctx}]: {final_issues}")

        self.validation_report.record(_file_ctx, _key_ctx, issues, auto_fixed=False)

        if final_text and final_text != text and not issues:
            self.cache.set(text, final_text, self.source_lang, target_lang, used_engine)

        return final_text

    async def _call_engine(self, engine: str, protected: str, target: str, retries: int) -> str:
        """Вызов конкретного движка по имени."""
        if engine == "Google" and ASYNC_OK and self.session:
            return await self._google_async(protected, target, retries)
        elif engine == "DeepL":
            if not self.api_key:
                raise RuntimeError("DeepL API key is missing")
            if not DEEPL_OK or deepl is None:
                raise RuntimeError("Official DeepL client is not installed")
            return await asyncio.to_thread(self._deepl_sync, protected, target, retries)
        elif engine == "JavaSim":
            res = await asyncio.to_thread(
                self.java_service.translate_batch_java, [protected], target,
                self.source_lang,
            )
            return res[0]
        else:
            # MyMemory или Google без aiohttp
            return await asyncio.to_thread(
                self._sync_fallback, protected, target, retries, engine
            )

    async def _google_async(self, text: str, target: str, retries: int) -> str:
        url = "https://translate.googleapis.com/translate_a/single"
        google_target = provider_language_code(GOOGLE_ENDPOINT_ENGINE, target, target=True)
        chunks = split_long_text(text)
        results = []
        for chunk in chunks:
            params = {
                'client': 'gtx', 'sl': self.source_lang, 'tl': google_target,
                'dt': 't', 'q': chunk,
            }
            results.append(await self._google_request(url, params, retries, chunk))
        return "".join(results)

    async def _google_request(self, url: str, params: dict, retries: int, fallback: str) -> str:
        for attempt in range(retries):
            try:
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return ''.join([s[0] for s in data[0] if s and s[0]])
                    elif resp.status == 429:
                        if attempt == retries - 1:
                            raise RuntimeError(
                                "Google Translate rate-limited the request after all retries"
                            )
                        wait = (2 ** attempt) * 2.0
                        logger.warning(f"Rate limited (429). Ждём {wait:.1f}s...")
                        await asyncio.sleep(wait)
                    else:
                        resp.raise_for_status()
            except asyncio.TimeoutError:
                logger.warning(f"Google timeout, попытка {attempt+1}/{retries}")
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
            except Exception:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
        return fallback

    @staticmethod
    def _deepl_language_code(language: str, *, target: bool) -> str:
        """Map app language codes to DeepL's API codes and variants."""
        normalized = language.replace("_", "-").casefold()
        if normalized in {"zh", "zh-cn", "zh-hans"}:
            # Simplified Chinese is target-only as ZH-HANS; DeepL expects the
            # unqualified ZH code when Chinese is the source language.
            return "ZH-HANS" if target else "ZH"
        if target and normalized in {"pt", "pt-br"}:
            return "PT-BR"
        return normalized.upper()

    def _get_deepl_client(self):
        """Create the official DeepL client once per session.

        Building a client per line opened a new HTTP session (and TLS handshake)
        for every single string in a mod.
        """
        with self._deepl_lock:
            if self._deepl_client is None:
                self._deepl_client = deepl.DeepLClient(self.api_key)
            return self._deepl_client

    def _deepl_sync(self, text: str, target: str, retries: int) -> str:
        if deepl is None:
            raise RuntimeError("Official DeepL client is not installed")
        client = self._get_deepl_client()
        source_code = self._deepl_language_code(self.source_lang, target=False)
        target_code = self._deepl_language_code(target, target=True)
        for attempt in range(retries):
            try:
                result = client.translate_text(
                    text, source_lang=source_code, target_lang=target_code
                )
                return result.text if result and result.text else text
            except Exception:
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    raise
        return text

    def _sync_fallback(self, text: str, target: str, retries: int, engine: Optional[str] = None) -> str:
        eng = engine or self.engine
        if eng == "MyMemory":
            # MyMemory only accepts full locales; the mapping lives in
            # provider_language_code() so every path stays consistent.
            src_locale = provider_language_code("MyMemory", self.source_lang, target=False)
            tgt_locale = provider_language_code("MyMemory", target, target=True)
            translator = MyMemoryTranslator(source=src_locale, target=tgt_locale)
        else:
            # deep-translator validates the code itself, so plain codes pass through.
            translator = GoogleTranslator(
                source=provider_language_code("Google", self.source_lang, target=False),
                target=provider_language_code("Google", target, target=True),
            )

        for attempt in range(retries):
            try:
                res = translator.translate(text)
                return res if res else text
            except Exception:
                if attempt < retries - 1:
                    time.sleep(1.0)
                else:
                    raise
        return text

    async def translate_batch(self, texts: List[str], target_lang: str = "ru",
                               keys: Optional[List[str]] = None,
                               file_ctx: str = "",
                               line_progress_cb=None,
                               stop_event: Optional[threading.Event] = None) -> List[str]:
        """Переводит ограниченными пачками и отменяет незавершённые запросы по Stop."""
        total = len(texts)
        if total == 0:
            return []

        results: List[Optional[str]] = [None] * total
        done_count = 0
        progress_lock = asyncio.Lock()
        # One batch in flight per unit of concurrency; the semaphore absorbs
        # retries and fallback attempts inside a batch.
        batch_size = max(1, self.concurrency)

        async def translate_one(i: int, text: str):
            nonlocal done_count
            result = await self.translate_text_async(
                text, target_lang,
                _file_ctx=file_ctx,
                _key_ctx=(keys[i] if keys else str(i))
            )
            if line_progress_cb:
                async with progress_lock:
                    done_count += 1
                    line_progress_cb(done_count, total)
            return i, result

        for first in range(0, total, batch_size):
            if stop_event and stop_event.is_set():
                raise TranslationCancelled("Перевод остановлен пользователем")

            last = min(first + batch_size, total)
            pending = {
                asyncio.create_task(translate_one(i, texts[i]))
                for i in range(first, last)
            }
            try:
                while pending:
                    if stop_event and stop_event.is_set():
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        raise TranslationCancelled("Перевод остановлен пользователем")

                    finished, pending = await asyncio.wait(
                        pending, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in finished:
                        index, translation = task.result()
                        results[index] = translation
            except BaseException:
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                raise

        return [value if value is not None else texts[i] for i, value in enumerate(results)]

# ---------------------------------------------------------------------------
# ОБРАБОТКА ФАЙЛОВ
# ---------------------------------------------------------------------------

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
                existing[match.group(1).strip()] = match.group(2)
    except Exception as e:
        logger.error("Error reading existing translations (%s): %s", dst_path, e)
    return existing


def build_output_lines(out_lines: Sequence[str], indexes: Sequence[int],
                       values: Sequence[str], meta: Sequence[Tuple[str, str, str]]) -> List[str]:
    """Replace the translated rows of a file, keeping every other byte intact."""
    lines = list(out_lines)
    for line_index, translated, (prefix, suffix, line_ending) in zip_equal(
        indexes, values, meta
    ):
        lines[line_index] = f'{prefix}"{translated}"{suffix}{line_ending}'
    return lines


def write_localization_file(dst_path: str, out_lines: Sequence[str], indexes: Sequence[int],
                            values: Sequence[str],
                            meta: Sequence[Tuple[str, str, str]]) -> None:
    """Single writer for translated files: preview and no-preview paths share it."""
    atomic_write_lines(dst_path, build_output_lines(out_lines, indexes, values, meta))


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


class _CombinedStopEvent:
    """A stop flag that also fires when this file's own timeout expires.

    translate_batch() only polls is_set(), so a per-file timeout needs a way to
    ask the running batch to stop without touching the global Stop button.
    """

    def __init__(self, *events: threading.Event):
        self._events = events
        self._cancelled = threading.Event()

    def is_set(self) -> bool:
        return self._cancelled.is_set() or any(event.is_set() for event in self._events)

    def cancel(self) -> None:
        self._cancelled.set()


def file_translation_timeout(line_count: int) -> float:
    """Scale the per-file budget with the number of lines to translate.

    A fixed 300 s cap silently dropped big files: retries with exponential
    backoff can legitimately cost tens of seconds for a single line.
    """
    return max(120.0, 60.0 + 0.35 * max(line_count, 0))


def _record_source_hashes(cache, dst_path: str, hashes: Dict[str, str]) -> None:
    """Persist which source text produced every value of a written file."""
    if cache is None or not hashes:
        return
    try:
        cache.set_source_hashes(dst_path, hashes)
    except Exception:
        logger.warning("Could not record source hashes for %s", dst_path, exc_info=True)


def process_file_sync(
    src_path: str,
    dst_path: str,
    translator: 'AsyncTranslator',
    log_cb,
    skip_translated: bool,
    stop_event: threading.Event,
    loop: asyncio.AbstractEventLoop,
    target_lang: str = "ru",
    line_progress_cb=None,
    preview_cb=None,
) -> FileProcessResult:
    target_name = get_hoi4_language_name(target_lang)
    required_suffix = f"_l_{target_name}.yml"
    if not os.path.basename(dst_path).casefold().endswith(required_suffix.casefold()):
        log_cb(
            f"  Файл пропущен: имя результата должно заканчиваться на {required_suffix}",
            'error',
        )
        return None

    _dst_parent = os.path.dirname(dst_path)
    if _dst_parent:
        os.makedirs(_dst_parent, exist_ok=True)
    existing = load_existing_translations(dst_path, log_cb) if skip_translated else {}

    if existing:
        log_cb(f"  Найдено переведённых строк в файле: {len(existing)}", 'info')

    source_lines, _source_encoding = read_localisation_lines(src_path, log_cb)

    prepared = _prepare_target_localisation_lines(
        source_lines, target_name, getattr(translator, "source_lang", None), log_cb
    )
    if prepared is None:
        return None
    lines, header_indexes, structure_changed = prepared

    out_lines = list(lines)
    to_translate_idx = []
    to_translate_val = []
    to_translate_keys = []
    meta = []
    skipped = 0
    stale_source_keys: List[str] = []
    unverifiable_keys: List[str] = []
    file_key_hashes: Dict[str, str] = {}
    malformed_lines = []
    key_line_numbers: Dict[str, List[int]] = {}

    # Remember which source text produced every written value, so an edited
    # source line is re-translated instead of answered with a stale translation.
    file_cache = getattr(translator, "cache", None)
    recorded_hashes: Dict[str, str] = {}
    if skip_translated and file_cache is not None:
        recorded_hashes = file_cache.get_source_hashes(dst_path)

    for i, line in enumerate(lines):
        if stop_event.is_set():
            return None

        if i in header_indexes:
            continue

        line_content, line_ending = _split_line_ending(line)
        match = VALUE_RE.fullmatch(line_content)
        if not match:
            stripped = line_content.strip()
            if stripped and not stripped.startswith('#') and ':' in stripped:
                malformed_lines.append(i + 1)
            continue

        prefix, value, suffix = match.group(1), match.group(2), match.group(3)
        if not VALUE_SUFFIX_RE.fullmatch(suffix):
            malformed_lines.append(i + 1)
            continue
        key = prefix.strip()
        key_line_numbers.setdefault(key, []).append(i + 1)
        file_key_hashes[key] = source_text_hash(value)

        existing_value = existing.get(key) if skip_translated else None
        if existing_value is not None:
            known_hash = recorded_hashes.get(key)
            source_changed = (
                known_hash is not None and known_hash != file_key_hashes[key]
            )
            if source_changed:
                # The mod author edited the English line: the stored value
                # describes the previous text and must not be reused.
                stale_source_keys.append(key)
            else:
                if known_hash is None:
                    # Written before fingerprints existed, or translated by
                    # hand. Reusing it is safe, but it can never be proven
                    # up to date, so the file-level log says so.
                    unverifiable_keys.append(key)
                existing_issues = validate_preview_translations([value], [existing_value])
                if existing_issues:
                    log_cb(
                        f"  Сохранённый перевод [{key}] отклонён: "
                        f"{', '.join(existing_issues)}; строка будет переведена заново",
                        'warning',
                    )
                else:
                    out_lines[i] = f'{prefix}"{existing_value}"{suffix}{line_ending}'
                    skipped += 1
                    continue

        to_translate_idx.append(i)
        to_translate_val.append(value)
        to_translate_keys.append(key)
        meta.append((prefix, suffix, line_ending))

    if unverifiable_keys:
        log_cb(
            f"  {len(unverifiable_keys)} строк из предыдущего результата приняты без "
            "сверки с исходником (нет контрольных сумм); при изменении исходника они "
            "не обновятся — снимите «Пропускать переведённое», чтобы перевести файл "
            "заново",
            'warning',
        )

    if malformed_lines:
        rows = ", ".join(map(str, malformed_lines[:10]))
        remaining = len(malformed_lines) - min(len(malformed_lines), 10)
        more = f" и ещё {remaining}" if remaining else ""
        log_cb(
            f"  Файл пропущен: строки {rows}{more} не соответствуют формату "
            'локализации (ключ:"значение"); проверьте ключи, кавычки и версию',
            'error',
        )
        return None

    duplicate_keys = {
        key: row_numbers for key, row_numbers in key_line_numbers.items()
        if len(row_numbers) > 1
    }
    if duplicate_keys:
        details = "; ".join(
            f"{key} (строки {', '.join(map(str, row_numbers))})"
            for key, row_numbers in list(duplicate_keys.items())[:10]
        )
        remaining = len(duplicate_keys) - min(len(duplicate_keys), 10)
        more = f"; и ещё {remaining} дубликатов" if remaining else ""
        log_cb(
            f"  Файл не записан: повторяющиеся localization keys: {details}{more}",
            'error',
        )
        return None

    total = len(to_translate_val)
    if total == 0:
        if structure_changed:
            atomic_write_lines(dst_path, out_lines)
        _record_source_hashes(file_cache, dst_path, file_key_hashes)
        if stale_source_keys:
            log_cb(
                f"  Исходник изменился у {len(stale_source_keys)} строк; "
                "перевод обновлён",
                'info',
            )
        if skipped:
            log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')
        return 0

    rel_path = os.path.basename(src_path)
    file_stop_event = _CombinedStopEvent(stop_event)

    async def run_translate():
        return await translator.translate_batch(
            to_translate_val, target_lang,
            keys=to_translate_keys,
            file_ctx=rel_path,
            line_progress_cb=line_progress_cb,
            stop_event=file_stop_event,
        )

    timeout_s = file_translation_timeout(total)
    try:
        future = asyncio.run_coroutine_threadsafe(run_translate(), loop)
        translated_vals = future.result(timeout=timeout_s)
    except FuturesTimeoutError:
        # future.result(timeout=...) does not stop the coroutine. Without an
        # explicit cancel the abandoned batch keeps running on the shared loop,
        # holding the semaphore and the network while the next file is processed.
        file_stop_event.cancel()
        future.cancel()
        log_cb(
            f"  Превышено время перевода файла ({timeout_s:.0f} с на {total} строк); "
            "файл не записан",
            'error',
        )
        return None
    except TranslationCancelled:
        log_cb("  Перевод файла остановлен; файл не записан", 'warning')
        return None
    except Exception as e:
        log_cb(f"  Ошибка перевода: {e}", 'error')
        return None

    if stop_event.is_set():
        return None

    multiline_rows = [
        index + 1 for index, value in zip_equal(to_translate_idx, translated_vals)
        if "\n" in value or "\r" in value
    ]
    if multiline_rows:
        rows = ", ".join(map(str, multiline_rows[:10]))
        log_cb(
            f"  Файл не записан: перевод вернул физические переносы строк "
            f"в значениях (строки {rows}); используйте литеральный \\n",
            'error',
        )
        return None

    approved_values = list(translated_vals)
    if preview_cb:
        preview_result = preview_cb(
            to_translate_val, translated_vals, to_translate_idx, meta, out_lines, dst_path
        )
        if preview_result is False:
            if stop_event.is_set():
                return None
            log_cb("  Предпросмотр отменён: файл не записан", 'warning')
            return FileProcessStatus.PREVIEW_CANCELLED
        if preview_result is True:
            # Legacy callbacks wrote the file themselves; writing the same
            # content again keeps both paths identical from here on.
            approved_values = list(translated_vals)
        elif isinstance(preview_result, (list, tuple)):
            # The preview returns the (edited) values; writing stays here so
            # both the preview and no-preview paths share one writer.
            approved_values = list(preview_result)
        else:
            log_cb(
                "  Файл не записан: предпросмотр не подтвердил сохранение",
                'error',
            )
            return None

    issues = validate_preview_translations(to_translate_val, approved_values)
    if issues:
        log_cb(
            "  Файл не записан: перевод нарушает формат локализации: "
            + "; ".join(issues[:5]),
            'error',
        )
        return None

    write_localization_file(dst_path, out_lines, to_translate_idx, approved_values, meta)
    _record_source_hashes(file_cache, dst_path, file_key_hashes)

    if stale_source_keys:
        log_cb(
            f"  Исходник изменился у {len(stale_source_keys)} строк; перевод обновлён",
            'info',
        )
    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')

    broken_in_file = translator.validation_report.broken_for_file(rel_path)
    if broken_in_file:
        log_cb(f"  ⚠ Битых строк: {len(broken_in_file)}", 'warning')
        for b in broken_in_file[:5]:
            log_cb(f"    [{b['key']}]: {', '.join(b['issues'])}", 'warning')

    return total


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

# ---------------------------------------------------------------------------
# НАСТРОЙКИ И ЗАЩИЩЁННОЕ ХРАНЕНИЕ API-КЛЮЧА
# ---------------------------------------------------------------------------

KEYRING_SERVICE = "Hoi4Translator"


def _read_settings_file(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as settings_file:
            data = json.load(settings_file)
        if isinstance(data, dict):
            return data
        logger.error("Settings file must contain a JSON object: %s", path)
    except Exception as error:
        logger.error("Settings load error (%s): %s", path, error)
    return None


def _secure_keyring():
    """Use only OS-backed keyring providers, never a plaintext file backend."""
    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
        backend_type = type(backend)
        module_name = backend_type.__module__.casefold()
        class_name = backend_type.__name__.casefold()
        trusted_modules = (
            "keyring.backends.windows",
            "keyring.backends.macos",
            "keyring.backends.secretservice",
            "keyring.backends.kwallet",
        )
        if not getattr(backend, "priority", 0) or not module_name.startswith(trusted_modules):
            logger.warning("No supported OS credential store is available; API key will not be persisted")
            return None
        if "plaintext" in class_name or "plaintext" in module_name:
            logger.warning("Refusing to store the API key in a plaintext keyring backend")
            return None
        return keyring
    except Exception as error:
        logger.warning("Could not access the OS credential store: %s", error)
        return None


def _keyring_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "default"


def load_api_key() -> str:
    secure_store = _secure_keyring()
    if secure_store is None:
        return ""
    try:
        return secure_store.get_password(KEYRING_SERVICE, _keyring_username()) or ""
    except Exception as error:
        logger.warning("Could not read API key from the OS credential store: %s", error)
        return ""


def store_api_key(api_key: str) -> bool:
    secure_store = _secure_keyring()
    if secure_store is None:
        return False
    username = _keyring_username()
    try:
        if api_key:
            secure_store.set_password(KEYRING_SERVICE, username, api_key)
        else:
            existing = secure_store.get_password(KEYRING_SERVICE, username)
            if existing is not None:
                secure_store.delete_password(KEYRING_SERVICE, username)
        return True
    except Exception as error:
        logger.warning("Could not update the API key in the OS credential store: %s", error)
        return False


def scrub_plaintext_api_keys() -> None:
    """Remove API keys written by older versions from legacy JSON settings."""
    paths = {SETTINGS_FILE, LEGACY_SETTINGS_FILE}
    for path in paths:
        data = _read_settings_file(path)
        if data is None or "api_key" not in data:
            continue
        data.pop("api_key", None)
        try:
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as error:
            logger.error("Could not remove a plaintext API key from %s: %s", path, error)


def load_settings() -> dict:
    current = _read_settings_file(SETTINGS_FILE)
    legacy = _read_settings_file(LEGACY_SETTINGS_FILE)
    settings = current if current is not None else (legacy or {})

    legacy_key = settings.pop("api_key", "")
    if not legacy_key and legacy is not None:
        legacy_key = legacy.get("api_key", "")
    if legacy_key:
        settings["_legacy_api_key"] = legacy_key

    migrated = current is None and legacy is not None
    contains_plaintext_key = bool(legacy_key)
    if migrated or contains_plaintext_key:
        save_settings(settings)
    return settings


def save_settings(data: dict) -> None:
    # The API key must only be stored in the OS credential store, never in JSON.
    safe_data = {
        name: value for name, value in data.items()
        if name not in {"api_key", "_legacy_api_key"}
    }
    try:
        atomic_write_text(
            SETTINGS_FILE, json.dumps(safe_data, ensure_ascii=False, indent=2)
        )
    except Exception as error:
        logger.error("Settings save error: %s", error)

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Hoi4 Translator v{__version__}")
        self.minsize(820, 720)
        self.configure(bg='#1a1a1a')

        self._ui_thread = threading.current_thread()
        self._ui_queue = queue.Queue()
        self._closing = False
        self._close_when_finished = False
        self._translation_thread: Optional[threading.Thread] = None
        self._secret_warning_shown = False

        self._stop_event = threading.Event()
        self.java_jar_var = tk.StringVar(value="")  # Путь к JAR

        # FIX: один async event loop на весь сеанс, запускается в фоновом потоке
        self._async_loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_event_loop, daemon=True, name="AsyncLoop"
        )
        self._loop_thread.start()

        self._setup_styles()
        self._build_ui()
        self._load_settings()

        if not TRANSLATOR_OK:
            self.log_line("ВНИМАНИЕ: deep-translator не найден. pip install deep-translator", 'error')
        if ASYNC_OK:
            self.log_line("Режим ускорения: AIOHTTP активен ✓", 'success')
        else:
            self.log_line("Режим ускорения: Стандартный (pip install aiohttp для скорости)", 'info')

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._ui_poll_id = self.after(25, self._drain_ui_queue)

    def _post_ui(self, callback, *args, **kwargs) -> bool:
        """Run a UI callback now on Tk's thread, or enqueue it from a worker.

        Returns whether the callback was accepted: a worker that waits for the
        callback to happen must not wait for one that was silently dropped.
        """
        if self._closing:
            return False
        if threading.current_thread() is self._ui_thread:
            callback(*args, **kwargs)
        else:
            self._ui_queue.put((callback, args, kwargs))
        return True

    def _drain_ui_queue(self):
        if self._closing:
            return
        while True:
            try:
                callback, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args, **kwargs)
            except tk.TclError:
                # The window may have been closed while a queued notification was pending.
                if not self._closing:
                    logger.exception("Tk callback failed")
            except Exception:
                logger.exception("UI callback failed")
            if self._closing:
                return
        self._ui_poll_id = self.after(25, self._drain_ui_queue)

    def _run_event_loop(self):
        """Фоновый поток с постоянным async event loop"""
        asyncio.set_event_loop(self._async_loop)
        self._async_loop.run_forever()

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        BG, BG2, BG3, FG = '#1a1a1a', '#2d2d2d', '#3d3d3d', '#ffffff'

        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.configure('TLabelframe', background=BG, foreground=FG)
        style.configure('TLabelframe.Label', background=BG, foreground='#4caf50', font=('Segoe UI', 10, 'bold'))
        style.configure('TButton', background=BG2, foreground=FG, font=('Segoe UI', 10), borderwidth=1)
        style.map('TButton', background=[('active', BG3), ('disabled', BG2)], foreground=[('disabled', '#666')])
        style.configure('TEntry', fieldbackground=BG2, foreground=FG, insertcolor=FG)
        style.configure('TCombobox', fieldbackground=BG2, background=BG2, foreground=FG, arrowcolor=FG)
        style.map(
            'TCombobox', fieldbackground=[('readonly', BG2)],
            selectbackground=[('readonly', BG2)], selectforeground=[('readonly', FG)],
        )
        style.configure('green.Horizontal.TProgressbar', troughcolor=BG2, background='#4caf50')
        style.configure('TCheckbutton', background=BG, foreground=FG)
        style.map('TCheckbutton', background=[('active', BG)], foreground=[('active', FG)])
        style.configure('TScale', background=BG, troughcolor=BG2)

    def browse_jar(self):
        f = filedialog.askopenfilename(title="Выберите hoi4_translator.jar", filetypes=[("JAR files", "*.jar")])
        if f:
            self.java_jar_var.set(f)
            self.log_line(f"📦 Выбран JAR: {os.path.basename(f)}", 'info')

    def _build_ui(self):
        pad = {'padx': 15, 'pady': 5}
        ttk.Label(self, text="Hoi4 Translator", font=('Segoe UI', 18, 'bold'), foreground='#4caf50').pack(pady=10)

        # Папки
        f_folders = ttk.LabelFrame(self, text=" Папки ", padding=10)
        f_folders.pack(fill='x', **pad)

        self.src_var = tk.StringVar()
        ttk.Label(f_folders, text="Оригинал:").grid(row=0, column=0, sticky='w')
        ttk.Entry(f_folders, textvariable=self.src_var).grid(row=0, column=1, sticky='ew', padx=5)
        ttk.Button(f_folders, text="Обзор", command=self.browse_src, width=8).grid(row=0, column=2)

        self.dst_var = tk.StringVar()
        ttk.Label(f_folders, text="Перевод:").grid(row=1, column=0, sticky='w', pady=5)
        ttk.Entry(f_folders, textvariable=self.dst_var).grid(row=1, column=1, sticky='ew', padx=5)
        ttk.Button(f_folders, text="Обзор", command=self.browse_dst, width=8).grid(row=1, column=2)
        f_folders.columnconfigure(1, weight=1)

        # Движок
        f_engine = ttk.LabelFrame(self, text=" Движок и языки ", padding=10)
        f_engine.pack(fill='x', **pad)

        row1 = ttk.Frame(f_engine)
        row1.pack(fill='x')
        ttk.Label(row1, text="Движок:").pack(side='left')
        self.engine_var = tk.StringVar(value="Google")
        engines = ["Google", "MyMemory"]
        if DEEPL_OK:
            engines.insert(1, "DeepL")
        if JAVA_OK:
            engines.append("JavaSim")

        self.engine_cb = ttk.Combobox(row1, textvariable=self.engine_var, values=engines, state="readonly", width=15)
        self.engine_cb.pack(side='left', padx=5)
        self.engine_cb.bind("<<ComboboxSelected>>", self._toggle_api_field)

        # --- УЛУЧШЕНИЕ 3: Исходный и целевой язык ---
        ttk.Label(row1, text="С языка:").pack(side='left', padx=(15, 0))
        self.lang_var = tk.StringVar(value="en")
        ttk.Combobox(
            row1, textvariable=self.lang_var,
            values=["en", "de", "fr", "es", "pl", "pt", "ru", "it", "ja", "zh-CN", "ko"],
            state="readonly", width=7
        ).pack(side='left', padx=5)

        ttk.Label(row1, text="→  На язык:").pack(side='left', padx=(10, 0))
        self.target_lang_var = tk.StringVar(value="ru")
        self._target_lang_map = {label: code for code, label in HOI4_TARGET_LANGS}
        self._target_lang_rev = dict(HOI4_TARGET_LANGS)
        self.target_lang_cb = ttk.Combobox(
            row1, values=[label for _, label in HOI4_TARGET_LANGS],
            state="readonly", width=23
        )
        self.target_lang_cb.set(self._target_lang_rev["ru"])
        self.target_lang_cb.pack(side='left', padx=5)
        self.target_lang_cb.bind("<<ComboboxSelected>>", self._on_target_lang_change)

        # Параллельность
        ttk.Label(row1, text="Параллельность:").pack(side='left', padx=(15, 0))
        self.semaphore_var = tk.IntVar(value=SEMAPHORE_LIMIT)
        self.sem_lbl = ttk.Label(row1, text=str(SEMAPHORE_LIMIT), width=3)
        self.sem_lbl.pack(side='right')
        ttk.Scale(
            row1, from_=1, to=50, variable=self.semaphore_var,
            command=lambda v: self.sem_lbl.config(text=str(int(float(v))))
        ).pack(side='right', padx=5)
        row_jar = ttk.Frame(f_engine)
        row_jar.pack(fill='x', pady=(10,0))
        ttk.Label(row_jar, text="Java JAR: ").pack(side='left')
        ttk.Entry(row_jar, textvariable=self.java_jar_var, width=45).pack(side='left', padx=5)
        ttk.Button(row_jar, text="Обзор", command=self.browse_jar, width=8).pack(side='left')

        row2 = ttk.Frame(f_engine)
        row2.pack(fill='x', pady=(10, 0))
        self.api_lbl = ttk.Label(row2, text="DeepL API Key:", foreground='#555')
        self.api_lbl.pack(side='left')
        self.api_key_var = tk.StringVar()
        self.api_entry = ttk.Entry(row2, textvariable=self.api_key_var, show="*", width=40, state='disabled')
        self.api_entry.pack(side='left', padx=5)
        self.verify_btn = ttk.Button(
            row2, text="Проверить API", command=self.verify_deepl_api,
            state='disabled', width=12,
        )
        self.verify_btn.pack(side='left', padx=5)
        self.api_status_var = tk.StringVar(value="")
        self.api_status_lbl = ttk.Label(row2, textvariable=self.api_status_var, foreground='#aaa', width=15)
        self.api_status_lbl.pack(side='left', padx=5)

        # Опции
        of = ttk.Frame(self)
        of.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Пропускать переведённое", variable=self.skip_var).pack(side='left')
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Открыть папку по итогу", variable=self.open_var).pack(side='left', padx=15)
        # --- УЛУЧШЕНИЕ 7: галочка предпросмотра ---
        self.preview_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Предпросмотр перед записью", variable=self.preview_var).pack(side='left')

        # Кэш инфо
        self.cache_info_var = tk.StringVar(value="")
        ttk.Label(of, textvariable=self.cache_info_var, foreground='#74c0fc').pack(side='right')
        ttk.Button(of, text="Инфо кэша", command=self._show_cache_info, width=10).pack(side='right', padx=5)
        ttk.Button(of, text="Очистить кэш", command=self._clear_cache, width=12).pack(side='right', padx=5)

        # Кэш других движков: экономит трафик, но мешает сменить движок
        of2 = ttk.Frame(self)
        of2.pack(fill='x', padx=15)
        self.reuse_fallback_cache_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            of2,
            text="Экономить трафик: переиспользовать кэш других движков "
                 "(иначе смена движка переводит заново)",
            variable=self.reuse_fallback_cache_var,
        ).pack(side='left')

        # --- УЛУЧШЕНИЕ 2: двойной прогресс-бар ---
        f_prog = ttk.Frame(self)
        f_prog.pack(fill='x', padx=15, pady=(10, 0))
        ttk.Label(f_prog, text="Файлы:", foreground='#aaa', width=7).pack(side='left')
        self.progress = ttk.Progressbar(f_prog, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(side='left', fill='x', expand=True)
        self.progress_file_lbl = ttk.Label(f_prog, text="0/0", foreground='#aaa', width=8)
        self.progress_file_lbl.pack(side='left', padx=(5, 0))

        f_prog2 = ttk.Frame(self)
        f_prog2.pack(fill='x', padx=15, pady=(3, 2))
        ttk.Label(f_prog2, text="Строки:", foreground='#aaa', width=7).pack(side='left')
        self.progress_lines = ttk.Progressbar(f_prog2, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress_lines.pack(side='left', fill='x', expand=True)
        self.progress_lines_lbl = ttk.Label(f_prog2, text="0/0", foreground='#aaa', width=8)
        self.progress_lines_lbl.pack(side='left', padx=(5, 0))

        f_status = ttk.Frame(self)
        f_status.pack(fill='x', padx=15)
        self.status_var = tk.StringVar(value="Готов к работе")
        self.speed_var = tk.StringVar(value="")
        ttk.Label(f_status, textvariable=self.status_var, foreground='#aaa').pack(side='left')
        ttk.Label(f_status, textvariable=self.speed_var, foreground='#4caf50').pack(side='right')

        # Лог
        self.log_widget = scrolledtext.ScrolledText(
            self, height=12, bg='#111', fg='#ccc',
            font=('Consolas', 9), state='disabled'
        )
        self.log_widget.pack(fill='both', expand=True, padx=15, pady=(5, 5))
        self.log_widget.tag_config('error', foreground='#ff6b6b')
        self.log_widget.tag_config('success', foreground='#69db7c')
        self.log_widget.tag_config('info', foreground='#74c0fc')
        self.log_widget.tag_config('warning', foreground='#ffa500')

        # Кнопки
        fb = ttk.Frame(self)
        fb.pack(fill='x', padx=15, pady=(0, 10))
        self.start_btn = ttk.Button(fb, text="▶ НАЧАТЬ ПЕРЕВОД", command=self.start)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 4), ipady=5)
        self.stop_btn = ttk.Button(fb, text="■ СТОП", state='disabled', command=self.stop)
        self.stop_btn.pack(side='left', fill='x', expand=True, padx=4, ipady=5)
        ttk.Button(fb, text="Очистить лог", command=self.clear_log).pack(
            side='left', fill='x', expand=True, padx=(4, 0), ipady=5
        )

    def _on_target_lang_change(self, event=None):
        label = self.target_lang_cb.get()
        code = self._target_lang_map.get(label, "ru")
        self.target_lang_var.set(code)

    def _get_target_lang(self) -> str:
        code = self.target_lang_var.get()
        return code if code.casefold() in HOI4_LANGUAGE_NAMES_BY_CODE else "ru"

    def _toggle_api_field(self, event=None):
        if self.engine_var.get() == "DeepL":
            self.api_entry.config(state='normal')
            self.api_lbl.config(foreground='#fff')
            self.verify_btn.config(state='normal')
        else:
            self.api_entry.config(state='disabled')
            self.api_lbl.config(foreground='#555')
            self.verify_btn.config(state='disabled')
            self.api_status_var.set("")

    def _note_fallback_engine(self, requested: str, used: str, count: int):
        """Report a silent engine substitution (e.g. DeepL quota → Google)."""
        if count == 1 or count % 25 == 0:
            self.log_line(
                f"Движок {requested} недоступен, используется {used} "
                f"(строк: {count})",
                'warning',
            )

    def _show_cache_info(self):
        try:
            cache = TranslationCache(DB_FILE)
            stats = cache.get_stats()
            cache.close()
            self.cache_info_var.set(f"БД: {stats['total']} записей")
            self.log_line(
                f"Кэш: {stats['total']} переводов, {stats['engines']} движков, "
                f"файлов под контролем: {stats['files']}",
                'info',
            )
        except Exception as e:
            self.log_line(f"Ошибка чтения кэша: {e}", 'error')

    def _clear_cache(self):
        """Drop cached translations of the selected engine / language pair."""
        if self._translation_thread and self._translation_thread.is_alive():
            self.log_line("Дождитесь окончания перевода: кэш занят", 'warning')
            return
        engine = self.engine_var.get()
        source_lang = self.lang_var.get()
        target_lang = self._get_target_lang()
        if not messagebox.askyesno(
            "Очистить кэш",
            f"Удалить сохранённые переводы для {engine} ({source_lang} → {target_lang})?\n"
            "Исходные файлы не изменяются.",
        ):
            return
        try:
            cache = TranslationCache(DB_FILE)
            removed = cache.clear(engine=engine, source_lang=source_lang, target_lang=target_lang)
            cache.close()
        except Exception as e:
            self.log_line(f"Ошибка очистки кэша: {e}", 'error')
            return
        self.log_line(f"Кэш очищен: удалено записей — {removed} ({engine})", 'success')
        self._show_cache_info()

    def verify_deepl_api(self):
        api_key = self.api_key_var.get().strip()
        if not api_key:
            self.api_status_var.set("Нет ключа")
            self.api_status_lbl.config(foreground='#ff6b6b')
            return
        if not DEEPL_OK or deepl is None:
            self.api_status_var.set("Нет клиента")
            self.api_status_lbl.config(foreground='#ff6b6b')
            return

        self.api_status_var.set("Проверка...")
        self.api_status_lbl.config(foreground='#ffa500')
        self.verify_btn.config(state='disabled')

        def check():
            try:
                client = deepl.DeepLClient(api_key)
                test_result = client.translate_text(
                    "test", source_lang="EN", target_lang="RU"
                )
                if test_result and test_result.text:
                    return True, "OK"
                return False, "Ошибка ответа"
            except Exception as e:
                err = str(e).lower()
                if "authorization" in err or "403" in err or "invalid" in err:
                    return False, "Неверный ключ"
                elif "quota" in err or "429" in err:
                    return False, "Лимит исчерпан"
                elif "connection" in err or "timeout" in err:
                    return False, "Нет сети"
                else:
                    return False, "Ошибка API"

        def on_done(success, msg):
            if success:
                self.api_status_var.set("✓ Валиден")
                self.api_status_lbl.config(foreground='#69db7c')
                self.log_line("DeepL API ключ успешно проверен!", 'success')
            else:
                self.api_status_var.set(f"✗ {msg}")
                self.api_status_lbl.config(foreground='#ff6b6b')
                self.log_line(f"DeepL API проверка не пройдена: {msg}", 'error')
            self.verify_btn.config(state='normal')

        def check_and_post():
            self._post_ui(on_done, *check())

        threading.Thread(target=check_and_post, daemon=True).start()

    def browse_src(self):
        d = filedialog.askdirectory(title="Выбери папку с оригинальной локализацией")
        if d:
            self.src_var.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(title="Выбери папку для перевода")
        if d:
            self.dst_var.set(d)

    def log_line(self, text: str, tag: Optional[str] = None):
        if threading.current_thread() is not self._ui_thread:
            self._post_ui(self.log_line, text, tag)
            return
        self.log_widget.config(state='normal')
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_text = f"[{timestamp}] {text}"
        if tag:
            self.log_widget.insert('end', full_text + '\n', tag)
        else:
            self.log_widget.insert('end', full_text + '\n')
        self.log_widget.see('end')
        self.log_widget.config(state='disabled')

    def clear_log(self):
        self.log_widget.config(state='normal')
        self.log_widget.delete('1.0', 'end')
        self.log_widget.config(state='disabled')

    def _update_line_progress(self, done: int, total: int):
        self.progress_lines.configure(maximum=max(total, 1), value=done)
        self.progress_lines_lbl.configure(text=f"{done}/{total}")

    def stop(self):
        self._stop_event.set()
        self.status_var.set("Остановка текущей пачки…")
        self.stop_btn.config(state='disabled')

    def _load_settings(self):
        s = load_settings()
        legacy_api_key = s.pop("_legacy_api_key", "")
        api_key = load_api_key()
        if not api_key and legacy_api_key:
            api_key = legacy_api_key
            if store_api_key(legacy_api_key):
                self.log_line("DeepL-ключ перенесён в системное хранилище учётных данных", 'info')
            else:
                self._secret_warning_shown = True
                self.log_line(
                    "Системное хранилище ключей недоступно: ключ будет сохранён только до закрытия программы",
                    'warning',
                )
        if api_key:
            self.api_key_var.set(api_key)
        scrub_plaintext_api_keys()

        if s.get('src'):
            self.src_var.set(s['src'])
        if s.get('dst'):
            self.dst_var.set(s['dst'])
        if s.get('engine'):
            saved_engine = "JavaSim" if s['engine'] == "JavaSim (Test)" else s['engine']
            valid_engines = {"Google", "MyMemory"}
            if DEEPL_OK:
                valid_engines.add("DeepL")
            if JAVA_OK:
                valid_engines.add("JavaSim")
            if saved_engine not in valid_engines:
                saved_engine = "Google"
            self.engine_var.set(saved_engine)
        if s.get('lang'):
            self.lang_var.set(s['lang'])
        # A saved concurrency of 0 is a valid (if slow) choice, so test for None.
        if s.get('semaphore') is not None:
            self.semaphore_var.set(int(s['semaphore']))
        if s.get('skip') is not None:
            self.skip_var.set(s['skip'])
        if s.get('open') is not None:
            self.open_var.set(s['open'])
        if s.get('preview') is not None:
            self.preview_var.set(s['preview'])
        if s.get('reuse_fallback_cache') is not None:
            self.reuse_fallback_cache_var.set(s['reuse_fallback_cache'])
        if s.get('java_jar'):
            self.java_jar_var.set(s['java_jar'])
        if s.get('target_lang'):
            saved_code = str(s['target_lang'])
            code = next(
                (supported for supported, _ in HOI4_TARGET_LANGS
                 if supported.casefold() == saved_code.casefold()),
                "ru",
            )
            if code.casefold() != saved_code.casefold():
                self.log_line(
                    f"Сохранённый целевой язык {saved_code!r} больше не поддерживается HOI4; "
                    "выбран русский",
                    'warning',
                )
            self.target_lang_var.set(code)
            self.target_lang_cb.set(self._target_lang_rev[code])
        self._toggle_api_field()

    def _save_current_settings(self):
        api_key = self.api_key_var.get().strip()
        if api_key:
            if not store_api_key(api_key) and not self._secret_warning_shown:
                self._secret_warning_shown = True
                self.log_line(
                    "DeepL-ключ не сохранён: нет доступного системного хранилища; "
                    "он останется только в памяти до закрытия программы",
                    'warning',
                )
        else:
            store_api_key("")
        scrub_plaintext_api_keys()
        save_settings({
            'src': self.src_var.get(),
            'dst': self.dst_var.get(),
            'engine': self.engine_var.get(),
            'lang': self.lang_var.get(),
            'target_lang': self._get_target_lang(),
            'semaphore': int(self.semaphore_var.get()),
            'skip': self.skip_var.get(),
            'open': self.open_var.get(),
            'preview': self.preview_var.get(),
            'reuse_fallback_cache': self.reuse_fallback_cache_var.get(),
            'java_jar': self.java_jar_var.get(),
        })

    def _on_close(self):
        if self._closing:
            return
        if self._translation_thread and self._translation_thread.is_alive():
            self._close_when_finished = True
            self.stop()
            self.status_var.set("Закрою окно после остановки перевода…")
            return
        self._finish_close()

    def _finish_close(self):
        if self._closing:
            return
        self._save_current_settings()
        self._closing = True
        try:
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
            self._loop_thread.join(timeout=1.0)
            if not self._loop_thread.is_alive():
                self._async_loop.close()
        except (RuntimeError, AttributeError):
            pass
        self.destroy()

    def _translation_finished(self):
        if self._closing:
            return
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
        if self._close_when_finished:
            self._finish_close()

    def _engine_is_usable(self, engine: str) -> bool:
        """Refuse to start when the selected engine cannot issue one request.

        Otherwise every file fails with the same TranslationFailed error and the
        user only sees a wall of identical messages.
        """
        if engine == "DeepL":
            if not DEEPL_OK:
                self.log_line("DeepL недоступен: установите deepl (pip install deepl)", 'error')
                return False
            return True
        if engine == "JavaSim":
            if not JAVA_OK:
                self.log_line("JavaSim недоступен: установите jpype1 (pip install jpype1)", 'error')
                return False
            # The JAR itself is resolved on the worker thread: it may have to
            # be compiled from java/, which must not freeze the UI.
            return True
        if engine == "MyMemory":
            if not TRANSLATOR_OK:
                self.log_line(
                    "MyMemory недоступен: установите deep-translator "
                    "(pip install deep-translator)",
                    'error',
                )
                return False
            return True
        # Google: the fast path needs aiohttp, the synchronous one deep-translator.
        if not ASYNC_OK and not TRANSLATOR_OK:
            self.log_line(
                "Google недоступен: установите aiohttp или deep-translator "
                "(pip install -r requirements.txt)",
                'error',
            )
            return False
        return True

    def start(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        engine = self.engine_var.get()
        api_key = self.api_key_var.get()
        if not src or not dst:
            self.log_line("Укажи обе папки!", 'error')
            return
        if not os.path.isdir(src):
            self.log_line(f"Папка не найдена: {src}", 'error')
            return
        if os.path.exists(dst) and not os.path.isdir(dst):
            self.log_line(f"Путь назначения не является папкой: {dst}", 'error')
            return
        try:
            validate_directory_relationships(src, dst)
        except ValueError as error:
            self.log_line(str(error), 'error')
            return
        if engine == "DeepL" and not DEEPL_OK:
            self.log_line("Ошибка: официальный клиент DeepL не установлен", 'error')
            return
        if engine == "DeepL" and not api_key:
            self.log_line("Ошибка: нужен DeepL API Key!", 'error')
            return
        try:
            get_hoi4_language_name(self._get_target_lang())
        except ValueError as error:
            self.log_line(str(error), 'error')
            return
        if self.lang_var.get().strip().casefold() == self._get_target_lang().casefold():
            # Translating a database into itself only spends quota and rewrites
            # the mod with an unchanged (but machine-round-tripped) text.
            self.log_line(
                "Исходный и целевой язык совпадают: выберите другой целевой язык",
                'error',
            )
            return
        if not self._engine_is_usable(engine):
            return

        self._stop_event.clear()
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.progress['value'] = 0

        config = {
            'engine': engine,
            'api_key': api_key,
            'source_lang': self.lang_var.get(),
            'target_lang': self._get_target_lang(),
            'src': src,
            'dst': dst,
            'java_jar': self.java_jar_var.get().strip(),
            'skip_translated': self.skip_var.get(),
            'preview': self.preview_var.get(),
            'open_output': self.open_var.get(),
            'concurrency': int(self.semaphore_var.get()),
            'reuse_fallback_cache': self.reuse_fallback_cache_var.get(),
        }
        self._save_current_settings()

        self._translation_thread = threading.Thread(
            target=self.run_translation, args=(config,), daemon=True, name="TranslationWorker"
        )
        self._translation_thread.start()

    def run_translation(self, config: Dict[str, Any]):
        # Take a snapshot on Tk's thread in start(); the worker never reads Tk variables.
        engine_name = config['engine']
        source_ln = config['source_lang']
        target_ln = config['target_lang']
        key = config['api_key']
        src = config['src']
        dst = config['dst']
        use_preview = config['preview']
        skip_translated = config['skip_translated']
        open_output = config['open_output']
        jar_path = config['java_jar']
        # Concurrency belongs to this run: a worker thread must not mutate a
        # global another run is reading.
        concurrency = max(1, int(config['concurrency']))
        reuse_fallback_cache = config.get('reuse_fallback_cache', False)

        start_t = time.time()
        total_lines = 0
        processed_files = 0
        completed_files = 0
        preview_cancellations = 0
        errors = 0
        self.log_line(
            f"Запуск... Движок: {engine_name} | {source_ln} → {target_ln} | "
            f"Параллельность: {concurrency}", 'info'
        )

        try:
            validate_directory_relationships(src, dst)
            files = collect_yml_files(src)
            total_files = len(files)
            if total_files == 0:
                self.log_line("Файлы .yml не найдены!", 'error')
                self._post_ui(self.status_var.set, "Нет файлов")
                return

            output_pairs = [(fp, rename_dst(fp, src, dst, target_ln)) for fp in files]
            try:
                validate_output_path_collisions(output_pairs)
            except ValueError as error:
                self.log_line(str(error) + ". Ни один файл не был записан.", 'error')
                self._post_ui(self.status_var.set, "Коллизия выходных файлов")
                return
            output_paths = dict(output_pairs)

            jar_path = resolve_java_jar(jar_path)
            if engine_name == "JavaSim" and not jar_path:
                self.log_line(
                    "JavaSim недоступен: JAR не найден и не может быть собран "
                    "(нужен JDK 11+). Укажите JAR вручную или соберите его: "
                    "javac -d java java/com/hoi4/translator/HoI4Translator.java",
                    'error',
                )
                self._post_ui(self.status_var.set, "JavaSim недоступен")
                return

            translator = AsyncTranslator(
                engine_name, api_key=key, source_lang=source_ln, java_jar_path=jar_path,
                concurrency=concurrency, reuse_fallback_cache=reuse_fallback_cache,
                fallback_notifier=self._note_fallback_engine,
            )
            asyncio.run_coroutine_threadsafe(
                translator.__aenter__(), self._async_loop
            ).result()

            self._post_ui(self.progress.configure, maximum=total_files, value=0)
            self._post_ui(self.progress_lines.configure, maximum=1, value=0)
            self._post_ui(self.progress_file_lbl.configure, text=f"0/{total_files}")

            for i, fp in enumerate(files):
                if self._stop_event.is_set():
                    self.log_line("[СТОП] Перевод остановлен пользователем.", 'warning')
                    break

                rel = os.path.relpath(fp, src)
                self._post_ui(
                    self.status_var.set, f"Файл {i+1}/{total_files}: {os.path.basename(fp)}"
                )
                self.log_line(f"[{i+1}/{total_files}] {rel}")
                self._post_ui(self.progress_file_lbl.configure, text=f"{i+1}/{total_files}")
                self._post_ui(self.progress_lines.configure, maximum=1, value=0)
                self._post_ui(self.progress_lines_lbl.configure, text="0/?")

                dst_path = output_paths[fp]

                def make_line_cb():
                    def cb(done, total):
                        self._post_ui(self._update_line_progress, done, total)
                    return cb

                preview_cb = None
                if use_preview:
                    def make_preview_cb(fp_=fp):
                        def cb(orig_vals, trans_vals, idxs, meta, out_lines, dst_path_arg):
                            result = {'approved': False, 'translations': list(trans_vals)}
                            done_evt = threading.Event()

                            def open_preview():
                                if self._closing:
                                    done_evt.set()
                                    return
                                def accept_preview(edited):
                                    result.update({'approved': True, 'translations': edited})
                                    done_evt.set()

                                try:
                                    PreviewWindow(
                                        self, fp_, orig_vals, trans_vals,
                                        on_accept=accept_preview,
                                        on_cancel=done_evt.set,
                                    )
                                except Exception:
                                    # A crash while building the window used to
                                    # leave the worker blocked on done_evt
                                    # forever, with no way to close the app.
                                    logger.exception("Preview window failed")
                                    done_evt.set()

                            if not self._post_ui(open_preview):
                                # The callback was dropped while closing.
                                return False

                            # Never block forever: the window can be destroyed
                            # by the OS, or the run can be stopped meanwhile.
                            while not done_evt.wait(timeout=0.25):
                                if self._closing or self._stop_event.is_set():
                                    return False

                            if result['approved'] and not self._stop_event.is_set():
                                return result['translations']
                            return False
                        return cb
                    preview_cb = make_preview_cb()

                try:
                    count = process_file_sync(
                        fp, dst_path, translator, self.log_line, skip_translated,
                        self._stop_event, self._async_loop, target_lang=target_ln,
                        line_progress_cb=make_line_cb(), preview_cb=preview_cb,
                    )
                    processed_files = i + 1
                    if count is FileProcessStatus.PREVIEW_CANCELLED:
                        preview_cancellations += 1
                        if self._stop_event.is_set():
                            break
                        self._post_ui(self.progress.configure, value=i + 1)
                        self._post_ui(
                            self.progress_file_lbl.configure,
                            text=f"{i+1}/{total_files}",
                        )
                        continue
                    if count is None:
                        if self._stop_event.is_set():
                            break
                        errors += 1
                        # A malformed/ambiguous file should not prevent the
                        # remaining independent localization files from running.
                        self._post_ui(self.progress.configure, value=i + 1)
                        self._post_ui(
                            self.progress_file_lbl.configure,
                            text=f"{i+1}/{total_files}",
                        )
                        continue
                    total_lines += count
                    completed_files += 1

                    elapsed = time.time() - start_t
                    if elapsed > 0 and total_lines > 0:
                        self._post_ui(
                            self.speed_var.set, f"{total_lines / elapsed:.1f} строк/сек"
                        )
                    self.log_line(f"  ✓ строк: {count}", 'success')
                except Exception as e:
                    self.log_line(f"  ОШИБКА: {e}", 'error')
                    logger.exception("File error: %s", fp)
                    errors += 1

                self._post_ui(self.progress.configure, value=i + 1)
                self._post_ui(
                    self.progress_file_lbl.configure, text=f"{i+1}/{total_files}"
                )

            elapsed = int(time.time() - start_t)
            minutes, seconds = divmod(elapsed, 60)
            if self._stop_event.is_set():
                state = "Остановлено"
            elif preview_cancellations:
                state = "Завершено с отменами предпросмотра"
            else:
                state = "Готово"
            summary = (
                f"{state}! Файлов: {processed_files}/{total_files} | "
                f"строк: {total_lines} | {minutes:02d}:{seconds:02d}"
            )
            if preview_cancellations:
                summary += f" | отмен предпросмотра: {preview_cancellations}"
            if errors:
                summary += f" | ошибок: {errors}"
            fallbacks = getattr(translator, "fallback_counts", {})
            if fallbacks:
                # A paid engine that degraded to a free one must show up here:
                # the status line still says the engine the user selected.
                details = ", ".join(
                    f"{pair}: {count}" for pair, count in sorted(fallbacks.items())
                )
                summary += f" | фолбэки: {details}"

            self._post_ui(self.status_var.set, summary)
            self._post_ui(self.speed_var.set, "")
            self.log_line("=" * 50, 'info')
            result_tag = (
                'success'
                if not errors and not preview_cancellations and not fallbacks
                and not self._stop_event.is_set()
                else 'warning'
            )
            self.log_line(summary, result_tag)

            validation = translator.validation_report
            self.log_line(
                validation.summary(),
                'info' if not validation.broken_count else 'warning',
            )
            if validation.broken_count:
                self.log_line(f"  Битые строки → {LOG_FILE}", 'warning')
                for broken in validation.broken:
                    logger.warning(
                        "BROKEN [%s] %s: %s",
                        broken['file'], broken['key'], broken['issues']
                    )
                if validation.untracked_broken:
                    logger.warning(
                        "BROKEN: и ещё %s строк не поместились в отчёт",
                        validation.untracked_broken,
                    )
            self.log_line("=" * 50, 'info')

            if open_output and completed_files and not self._stop_event.is_set():
                try:
                    os.startfile(dst)
                except Exception:
                    pass
        except Exception as e:
            logger.exception("Critical error in translation thread")
            self.log_line(f"Критическая ошибка: {e}", 'error')
        finally:
            if 'translator' in locals():
                try:
                    asyncio.run_coroutine_threadsafe(
                        translator.__aexit__(None, None, None), self._async_loop
                    ).result()
                except Exception:
                    logger.exception("Ошибка при закрытии ресурсов")
            self._post_ui(self._translation_finished)

# ---------------------------------------------------------------------------
# УЛУЧШЕНИЕ 7: ОКНО ПРЕДПРОСМОТРА
# ---------------------------------------------------------------------------

class PreviewWindow(tk.Toplevel):
    """
    Модальное окно предпросмотра перевода.
    Показывает таблицу Оригинал | Перевод, каждую строку перевода можно отредактировать.
    on_accept(edited_list) — вызывается с финальным списком строк при нажатии «Сохранить».
    on_cancel() — вызывается при отмене.
    """

    # Two widgets per row: an unlimited preview froze the UI on large mods.
    MAX_PREVIEW_ROWS = 1000

    def __init__(self, parent, filename: str, originals: List[str], translations: List[str],
                 on_accept, on_cancel):
        super().__init__(parent)
        self.title(f"Предпросмотр — {os.path.basename(filename)}")
        self.configure(bg='#1a1a1a')
        self.resizable(True, True)
        self.minsize(900, 500)

        self._on_accept = on_accept
        self._on_cancel = on_cancel
        self._originals = list(originals)
        self._translations = list(translations)
        self._entries: List[tk.StringVar] = []

        # Делаем модальным
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        try:
            self._build(filename, originals, translations)

            # Центрируем относительно родителя
            self.update_idletasks()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(
                f"{max(w,900)}x{max(h,550)}+{px + (pw-max(w,900))//2}+{py + (ph-max(h,550))//2}"
            )
        except Exception:
            # A half-built modal window would keep the grab and freeze the UI.
            logger.exception("Preview window could not be built")
            try:
                self.destroy()
            except Exception:
                pass
            raise

    def _build(self, filename, originals, translations):
        BG = '#1a1a1a'

        # Заголовок
        hdr = tk.Frame(self, bg='#2d2d2d', pady=8)
        hdr.pack(fill='x')
        tk.Label(hdr, text=f"Предпросмотр: {os.path.basename(filename)}",
                 bg='#2d2d2d', fg='#4caf50', font=('Segoe UI', 11, 'bold')).pack(side='left', padx=15)
        shown = min(len(originals), self.MAX_PREVIEW_ROWS)
        count_text = (
            f"показаны первые {shown} из {len(originals)} строк"
            if shown < len(originals) else f"{len(originals)} строк"
        )
        tk.Label(hdr, text=count_text,
                 bg='#2d2d2d', fg='#aaa', font=('Segoe UI', 10)).pack(side='right', padx=15)

        # Заголовки колонок
        col_hdr = tk.Frame(self, bg='#252525')
        col_hdr.pack(fill='x', padx=10, pady=(8, 0))
        tk.Label(col_hdr, text="  Оригинал", bg='#252525', fg='#74c0fc',
                 font=('Segoe UI', 9, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Label(col_hdr, text="  Перевод (можно редактировать)", bg='#252525', fg='#69db7c',
                 font=('Segoe UI', 9, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)

        # Scrollable таблица
        container = tk.Frame(self, bg=BG)
        container.pack(fill='both', expand=True, padx=10, pady=4)

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
        self._scroll_frame = tk.Frame(canvas, bg=BG)

        self._scroll_frame.bind(
            '<Configure>',
            lambda _event: canvas.configure(scrollregion=canvas.bbox('all'))
        )
        canvas.create_window((0, 0), window=self._scroll_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        # Привязка колёсика мыши: Windows/macOS шлют <MouseWheel>, X11 — <Button-4/5>
        def _on_wheel(event):
            if getattr(event, "num", 0) == 4:
                canvas.yview_scroll(-1, 'units')
            elif getattr(event, "num", 0) == 5:
                canvas.yview_scroll(1, 'units')
            else:
                canvas.yview_scroll(-1 * (event.delta // 120), 'units')

        def _bind_wheel(_event=None):
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                canvas.bind_all(sequence, _on_wheel)

        def _unbind_wheel(_event=None):
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                canvas.unbind_all(sequence)

        canvas.bind('<Enter>', _bind_wheel)
        canvas.bind('<Leave>', _unbind_wheel)

        # Строки таблицы. На строку создаётся два виджета, поэтому на больших
        # файлах окно строилось минутами: редактируемую часть ограничиваем.
        for i, (orig, trans) in enumerate(
            zip_equal(
                originals[:self.MAX_PREVIEW_ROWS], translations[:self.MAX_PREVIEW_ROWS]
            )
        ):
            row_bg = BG if i % 2 == 0 else '#202020'
            row = tk.Frame(self._scroll_frame, bg=row_bg)
            row.pack(fill='x', pady=1)

            # Оригинал (только для чтения)
            orig_lbl = tk.Text(row, bg=row_bg, fg='#aaa', font=('Consolas', 9),
                               height=1, relief='flat', wrap='none', bd=0,
                               cursor='arrow', state='normal')
            orig_lbl.insert('1.0', orig[:120] + ('…' if len(orig) > 120 else ''))
            orig_lbl.config(state='disabled')
            orig_lbl.pack(side='left', fill='x', expand=True, padx=(4, 2), pady=2)

            # Перевод (редактируемый)
            var = tk.StringVar(value=trans)
            self._entries.append(var)
            entry = ttk.Entry(row, textvariable=var, font=('Consolas', 9))
            entry.pack(side='left', fill='x', expand=True, padx=(2, 4), pady=2)

        # Подсказка
        tk.Label(self, text="💡 Отредактируй любую строку перевода, затем нажми «Сохранить»",
                 bg=BG, fg='#555', font=('Segoe UI', 9)).pack(pady=(0, 4))

        # Кнопки
        btn_frame = tk.Frame(self, bg='#2d2d2d', pady=8)
        btn_frame.pack(fill='x')

        ttk.Button(btn_frame, text="✔ Сохранить и записать файл",
                   command=self._accept).pack(side='left', padx=15, ipadx=10, ipady=4)
        ttk.Button(btn_frame, text="✘ Отмена (не записывать)",
                   command=self._cancel).pack(side='left', ipadx=10, ipady=4)
        tk.Label(btn_frame, text=f"Файл: {os.path.basename(filename)}",
                 bg='#2d2d2d', fg='#555', font=('Segoe UI', 9)).pack(side='right', padx=15)

    def _accept(self):
        # Only the visible rows are editable; the rest keep the engine output.
        edited = [var.get() for var in self._entries]
        edited.extend(self._translations[len(edited):])
        issues = validate_preview_translations(self._originals, edited)
        if issues:
            shown_issues = issues[:15]
            details = "\n".join(shown_issues)
            if len(issues) > len(shown_issues):
                details += f"\n… и ещё {len(issues) - len(shown_issues)} строк(и)"
            messagebox.showerror(
                "Нарушены игровые плейсхолдеры",
                "Исправьте отмеченные строки перед сохранением:\n\n" + details,
                parent=self,
            )
            return
        self.destroy()
        self._on_accept(edited)

    def _cancel(self):
        self.destroy()
        self._on_cancel()


# ---------------------------------------------------------------------------
# ТОЧКА ВХОДА
# ---------------------------------------------------------------------------

USAGE = f"""Hoi4 Translator v{__version__}

Использование:
  hoi4_translator.py            запустить графический интерфейс
  hoi4_translator.py --help     показать эту справку
  hoi4_translator.py --version  показать версию

Каталог данных (кэш, настройки, лог): {APP_DATA_DIR}
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: --help/--version exit before Tk is touched."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--help" in arguments or "-h" in arguments:
        print(USAGE)
        return 0
    if "--version" in arguments:
        print(__version__)
        return 0
    if arguments:
        print(f"Неизвестные аргументы: {' '.join(arguments)}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    if not TRANSLATOR_OK:
        print("WARNING: pip install deep-translator")
    if not ASYNC_OK:
        print("INFO: pip install aiohttp  (для ускорения)")
    try:
        App().mainloop()
    except Exception:
        # A windowed build has no console: without this the process would die
        # silently and the user would see no window and no error.
        logger.exception("Application startup failed")
        try:
            messagebox.showerror(
                "Hoi4 Translator",
                "Не удалось запустить приложение. Подробности — в логе:\n" + LOG_FILE,
            )
        except Exception:
            pass
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
