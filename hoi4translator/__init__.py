"""HoI4 localization translator.

The package keeps the domain logic (localisation, validation, translation,
pipeline, reporting) separate from the Tkinter GUI so that both can be tested
and reviewed on their own. This module is the public surface: every name
below is re-exported from the layer that owns it.

Layers:
    config        paths, logging, atomic writes, optional dependencies
    localisation  the Paradox YML format, language names, output paths
    text          placeholder protection and query splitting
    validation    placeholder/colour checks and the per-run validation report
    providers     engine metadata and the fallback chain
    java_service  the bundled Java translator
    storage       SQLite translation cache and source fingerprints
    translation   AsyncTranslator: the engine facade
    pipeline      per-file processing: translate, repair, write, report
    report        machine-readable run report (json/csv)
    settings      keyring-backed secrets and persisted UI settings
    gui           Tkinter windows (imported lazily: Tk may be unavailable)
"""
import sys
from typing import Optional, Sequence

from .config import (
    APP_DATA_DIR,
    ASYNC_OK,
    CACHE_FORMAT_VERSION,
    DB_FILE,
    DEEPL_OK,
    LOG_FILE,
    SEMAPHORE_LIMIT,
    SETTINGS_FILE,
    TRANSLATOR_OK,
    __version__,
    atomic_write_lines,
    atomic_write_text,
    logger,
)
from .errors import TranslationCancelled, TranslationFailed
from .java_service import (
    JAVA_OK,
    JavaTranslatorService,
    build_java_jar,
    resolve_java_jar,
)
from .localisation import (
    HOI4_LANGUAGE_NAMES,
    HOI4_LANGUAGE_NAMES_BY_CODE,
    HOI4_TARGET_LANGS,
    PLACEHOLDER_RE,
    VALUE_RE,
    collect_yml_files,
    get_hoi4_language_name,
    load_existing_translations,
    read_localisation_lines,
    rename_dst,
    source_text_hash,
    validate_directory_relationships,
    validate_output_path_collisions,
)
from .pipeline import (
    BROKEN_LINE_MODES,
    BrokenLineMode,
    FileProcessStatus,
    broken_line_label,
    broken_line_mode_from_label,
    build_output_lines,
    file_translation_timeout,
    process_file_sync,
)
from .providers import (
    FALLBACK_CHAIN,
    GOOGLE_ENDPOINT_ENGINE,
    provider_language_code,
)
from .report import RunReport
from .settings import (
    load_api_key,
    load_settings,
    save_settings,
    scrub_plaintext_api_keys,
    store_api_key,
)
from .storage import TranslationCache
from .text import (
    MAX_QUERY_CHARS,
    PostProcessor,
    protect_placeholders,
    restore_placeholders,
    split_long_text,
    zip_equal,
)
from .translation import AsyncTranslator
from .validation import (
    PlaceholderValidator,
    ValidationReport,
    validate_preview_translations,
)

__all__ = [
    # metadata
    "__version__", "APP_DATA_DIR", "DB_FILE", "LOG_FILE", "SETTINGS_FILE",
    # capability flags and defaults
    "ASYNC_OK", "DEEPL_OK", "JAVA_OK", "TRANSLATOR_OK", "SEMAPHORE_LIMIT",
    # logging and io
    "logger", "atomic_write_lines", "atomic_write_text",
    # errors
    "TranslationCancelled", "TranslationFailed",
    # localisation
    "HOI4_LANGUAGE_NAMES", "HOI4_LANGUAGE_NAMES_BY_CODE", "HOI4_TARGET_LANGS",
    "PLACEHOLDER_RE", "VALUE_RE", "collect_yml_files", "get_hoi4_language_name",
    "load_existing_translations", "read_localisation_lines", "rename_dst",
    "source_text_hash", "validate_directory_relationships",
    "validate_output_path_collisions",
    # text
    "MAX_QUERY_CHARS", "PostProcessor", "protect_placeholders",
    "restore_placeholders", "split_long_text", "zip_equal",
    # validation
    "PlaceholderValidator", "ValidationReport", "validate_preview_translations",
    # providers
    "FALLBACK_CHAIN", "GOOGLE_ENDPOINT_ENGINE", "provider_language_code",
    # java service
    "JavaTranslatorService", "build_java_jar", "resolve_java_jar",
    # storage
    "CACHE_FORMAT_VERSION", "TranslationCache",
    # translation
    "AsyncTranslator",
    # pipeline
    "BROKEN_LINE_MODES", "BrokenLineMode", "FileProcessStatus",
    "build_output_lines", "broken_line_label", "broken_line_mode_from_label",
    "file_translation_timeout", "process_file_sync",
    # reporting
    "RunReport",
    # settings
    "load_api_key", "load_settings", "save_settings",
    "scrub_plaintext_api_keys", "store_api_key",
    # entry point
    "USAGE", "main",
]

USAGE = f"""Hoi4 Translator v{__version__}

Использование:
  hoi4_translator.py            запустить графический интерфейс
  hoi4_translator.py --help     показать эту справку
  hoi4_translator.py --version  показать версию

Каталог данных (кэш, настройки, лог): {APP_DATA_DIR}
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. --help/--version exit before Tk is imported."""
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

    try:
        from .gui.app import App
    except ImportError as error:
        # customtkinter needs tkinter, and neither is guaranteed to be there.
        message = (
            f"Не удалось импортировать GUI: {error}\n\n"
            "Установите зависимости: python -m pip install -r requirements.txt"
        )
        print(message)
        try:
            # A windowed build has no console, so the print above is invisible.
            from tkinter import messagebox

            messagebox.showerror("Hoi4 Translator", message)
        except Exception:
            pass
        return 1

    if not TRANSLATOR_OK:
        print("WARNING: pip install deep-translator")
    if not ASYNC_OK:
        print("INFO: pip install aiohttp  (для ускорения)")
    try:
        App().mainloop()
    except Exception as error:
        # A windowed build has no console: without this the process would die
        # silently and the user would see no window and no error. The reason is
        # shown as well, otherwise "the GUI is down" is all anyone learns.
        from tkinter import messagebox

        logger.exception("Application startup failed")
        try:
            messagebox.showerror(
                "Hoi4 Translator",
                f"Не удалось запустить приложение.\n\n"
                f"{type(error).__name__}: {error}\n\n"
                f"Подробности — в логе:\n{LOG_FILE}",
            )
        except Exception:
            pass
        return 1
    return 0
