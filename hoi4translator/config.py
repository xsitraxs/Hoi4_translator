"""Paths, logging, capability detection and low-level file IO."""
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler

# Keep in sync with the git tag of a release (CI compares them).
__version__ = "1.4.3"

# Default concurrency for a translation run. Per-run values are passed to
# AsyncTranslator instead of mutating this global from a worker thread.
SEMAPHORE_LIMIT = 15

# The unofficial Google endpoint (translate.googleapis.com) has no API key and
# no documented quota: 15 parallel lines at the default concurrency were enough
# to collect 429s on a 4 700-key mod, and the retries then kept the endpoint
# saturated. Requests are spread evenly at this rate instead (measured at ~5 rps,
# which survives a full Kaiserreich-sized run); 0 disables pacing.
GOOGLE_REQUESTS_PER_SECOND = 5.0

# After this many consecutive 429s the endpoint is treated as refusing
# everything: further requests fail fast for GOOGLE_RATE_LIMIT_COOLDOWN seconds,
# so the fallback chain takes over instead of retrying into a wall.
GOOGLE_RATE_LIMIT_BREAKER_THRESHOLD = 5
GOOGLE_RATE_LIMIT_COOLDOWN = 60.0

# Попытка импорта асинхронных библиотек для ускорения
try:
    import aiohttp
    ASYNC_OK = True
except ImportError:
    aiohttp = None
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
    DEEPL_IMPORT_ERROR = ""
except ImportError as error:
    deepl = None
    DEEPL_OK = False
    # The engine silently disappears from the list otherwise; the GUI shows
    # this text so "no DeepL" can be diagnosed without a console.
    DEEPL_IMPORT_ERROR = str(error)

# CustomTkinter draws the GUI; it needs tkinter, which some Python builds
# (headless Linux packages) do not ship.
try:
    import customtkinter
    CTK_OK = True
except ImportError:
    customtkinter = None
    CTK_OK = False


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

def _has_console() -> bool:
    """False in a windowed build (pythonw / PyInstaller --noconsole).

    There `sys.stderr` is None, so a StreamHandler has nothing to write to and
    every log record ends in a logging error that goes nowhere. The file handler
    stays, which is where a user reporting a bug actually looks.
    """
    stream = getattr(sys, "stderr", None)
    return stream is not None and hasattr(stream, "write")


def _build_log_handlers():
    """Rotating file handler (with a console mirror) that degrades gracefully."""
    handlers = []
    try:
        handlers.append(
            RotatingFileHandler(
                LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    except OSError:
        # A read-only or missing data directory must not prevent startup.
        pass
    if _has_console():
        handlers.append(logging.StreamHandler())
    if not handlers:
        handlers.append(logging.NullHandler())
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
