"""Per-file processing pipeline: translate, validate, write, report."""
import asyncio
import os
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple, Union

from .config import atomic_write_lines, logger
from .errors import TranslationCancelled
from .localisation import (
    VALUE_RE,
    VALUE_SUFFIX_RE,
    _prepare_target_localisation_lines,
    _split_line_ending,
    get_hoi4_language_name,
    load_existing_translations,
    normalize_key,
    read_localisation_lines,
    source_text_hash,
)
from .report import RunReport
from .text import zip_equal
from .validation import validate_pairs, validate_preview_translations

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .translation import AsyncTranslator


class FileProcessStatus(Enum):
    """Non-error file-processing outcomes that are not successful writes."""

    PREVIEW_CANCELLED = "preview_cancelled"


FileProcessResult = Union[int, FileProcessStatus, None]


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


class BrokenLineMode(Enum):
    """What to do with a translated value that breaks the HOI4 format."""

    # Refuse to write the file (the historic behaviour: never ship a file that
    # can crash or corrupt the game).
    REJECT_FILE = "reject_file"
    # Write the file, but keep the English text for the offending line and list
    # it in the run report.
    KEEP_ORIGINAL = "keep_original"


BROKEN_LINE_MODES: Tuple[Tuple[BrokenLineMode, str], ...] = (
    (BrokenLineMode.REJECT_FILE, "Не записывать файл"),
    (BrokenLineMode.KEEP_ORIGINAL, "Заменить битую строку оригиналом"),
)


def broken_line_mode_from_label(label: str) -> BrokenLineMode:
    for mode, mode_label in BROKEN_LINE_MODES:
        if mode_label == label:
            return mode
    return BrokenLineMode.REJECT_FILE


# A value that had to be repaired by writing the English text back is stored
# with an empty fingerprint. The stored text is knowingly not a translation,
# so the next run must ask the engine again instead of skipping it like an
# up-to-date line.
RECOVERED_HASH_MARKER = ""


def broken_line_label(mode: BrokenLineMode) -> str:
    for candidate, mode_label in BROKEN_LINE_MODES:
        if candidate == mode:
            return mode_label
    return BROKEN_LINE_MODES[0][1]


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
    broken_line_mode: BrokenLineMode = BrokenLineMode.REJECT_FILE,
    report: Optional[RunReport] = None,
    report_label: Optional[str] = None,
    vanilla=None,
) -> FileProcessResult:
    # The body fills `state`; the report is written from it whatever the outcome.
    state: Dict[str, Any] = {
        "status": "rejected",
        "message": "файл не обработан",
        "recovered": [],
        "broken": [],
        "written": 0,
        "skipped": 0,
        "stale": [],
        "repaired": [],
        "unverifiable": [],
        "reused": 0,
        "hashes": {},
        "report": report,
    }
    try:
        return _process_file(
            src_path, dst_path, translator, log_cb, skip_translated, stop_event,
            loop, target_lang, line_progress_cb, preview_cb, broken_line_mode,
            rel_path=os.path.basename(src_path), state=state, vanilla=vanilla,
        )
    finally:
        if report is not None:
            report.record_file(
                report_label or os.path.basename(src_path), dst_path,
                state["status"],
                translated=state["written"], skipped=state["skipped"],
                stale=len(state["stale"]) + len(state["repaired"]),
                recovered=len(state["recovered"]), broken=len(state["broken"]),
                reused=state["reused"], message=state["message"],
            )


def _process_file(
    src_path, dst_path, translator, log_cb, skip_translated, stop_event, loop,
    target_lang, line_progress_cb, preview_cb, broken_line_mode, *, rel_path, state,
    vanilla=None,
) -> FileProcessResult:
    """Body of process_file_sync; `state` is shared with the report recorder."""
    report = state.get("report")
    file_key_hashes = state["hashes"]
    recovered_keys = state["recovered"]
    broken_keys = state["broken"]
    repaired_keys = state["repaired"]

    def finish(status: str, message: str, result: FileProcessResult) -> FileProcessResult:
        state["status"] = status
        state["message"] = message
        return result

    target_name = get_hoi4_language_name(target_lang)
    required_suffix = f"_l_{target_name}.yml"
    if not os.path.basename(dst_path).casefold().endswith(required_suffix.casefold()):
        log_cb(
            f"  Файл пропущен: имя результата должно заканчиваться на {required_suffix}",
            'error',
        )
        return finish("rejected", "неверное имя результата", None)

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
        return finish("rejected", "не совпал исходный язык", None)
    lines, header_indexes, structure_changed = prepared

    out_lines = list(lines)
    to_translate_idx = []
    to_translate_val = []
    to_translate_keys = []
    meta = []
    skipped = 0
    stale_source_keys: List[str] = []
    unverifiable_keys: List[str] = []
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
            return finish("cancelled", "остановлено пользователем", None)

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
        # KEY:0 and KEY:1 are the same localisation key (the number is only a
        # revision marker), so comparisons and duplicate detection use the
        # version-less form.
        raw_key = prefix.strip()
        key = normalize_key(prefix)
        key_line_numbers.setdefault(key, []).append(i + 1)
        file_key_hashes[key] = source_text_hash(value)

        existing_value = existing.get(key) if skip_translated else None
        if existing_value is not None:
            # Hashes written by older versions are keyed by "KEY:0".
            known_hash = recorded_hashes.get(key, recorded_hashes.get(raw_key))
            source_changed = (
                known_hash is not None
                and known_hash != RECOVERED_HASH_MARKER
                and known_hash != file_key_hashes[key]
            )
            if known_hash == RECOVERED_HASH_MARKER:
                # The previous run repaired this line with English text; it is
                # not a translation and must be translated again.
                repaired_keys.append(key)
            elif source_changed:
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

        # The game already translates many strings the mod copies verbatim.
        # Reusing that translation is both better and cheaper than asking an
        # engine for a second, different rendering of the same sentence.
        if vanilla is not None:
            reused_value = vanilla.lookup(value)
            if reused_value:
                reused_issues = validate_preview_translations([value], [reused_value])
                if not reused_issues:
                    out_lines[i] = f'{prefix}"{reused_value}"{suffix}{line_ending}'
                    state["reused"] += 1
                    continue
                log_cb(
                    f"  Ванильный перевод [{key}] отклонён проверкой формата: "
                    f"{', '.join(reused_issues)}; строка будет переведена движком",
                    'warning',
                )

        to_translate_idx.append(i)
        to_translate_val.append(value)
        to_translate_keys.append(key)
        meta.append((prefix, suffix, line_ending))

    state["skipped"] = skipped
    state["stale"] = stale_source_keys
    state["unverifiable"] = unverifiable_keys

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
        return finish("rejected", "строки не соответствуют формату", None)

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
        return finish("rejected", "повторяющиеся ключи", None)

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
        if state["reused"]:
            log_cb(f"  Из ванильного перевода: {state['reused']}", 'info')
        return finish("written", "", 0)

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
        return finish("error", "превышено время перевода", None)
    except TranslationCancelled:
        log_cb("  Перевод файла остановлен; файл не записан", 'warning')
        return finish("cancelled", "остановлено пользователем", None)
    except Exception as e:
        log_cb(f"  Ошибка перевода: {e}", 'error')
        return finish("error", str(e), None)

    if stop_event.is_set():
        return finish("cancelled", "остановлено пользователем", None)

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
        return finish("rejected", "переносы строк в значениях", None)

    approved_values = list(translated_vals)
    if preview_cb:
        preview_result = preview_cb(
            to_translate_val, translated_vals, to_translate_idx, meta, out_lines, dst_path
        )
        if preview_result is False:
            if stop_event.is_set():
                return finish("cancelled", "остановлено пользователем", None)
            log_cb("  Предпросмотр отменён: файл не записан", 'warning')
            state["status"] = "cancelled"
            state["message"] = "предпросмотр отменён"
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
            return finish("rejected", "предпросмотр не подтвердил сохранение", None)

    # A single machine-translated line can break the format (the engine may
    # translate a marker, drop a bracket or insert a raw quote). Per-line
    # recovery keeps the other 2999 lines of the file instead of losing them.
    try:
        row_issues = validate_pairs(to_translate_val, approved_values)
    except ValueError as error:
        # The preview edited the list itself: report it like any other
        # format violation instead of raising out of the file loop.
        log_cb(f"  Файл не записан: {error}", 'error')
        return finish("rejected", "перевод нарушает формат", None)
    broken_indexes = [index for index, issues in enumerate(row_issues) if issues]
    if broken_indexes:
        if broken_line_mode is not BrokenLineMode.KEEP_ORIGINAL:
            issues = validate_preview_translations(to_translate_val, approved_values)
            log_cb(
                "  Файл не записан: перевод нарушает формат локализации: "
                + "; ".join(issues[:5]),
                'error',
            )
            return finish("rejected", "перевод нарушает формат", None)

        for index in broken_indexes:
            key = to_translate_keys[index]
            log_cb(
                f"  Строка [{key}] заменена оригиналом: "
                f"{', '.join(row_issues[index])}",
                'warning',
            )
            recovered_keys.append(key)
            approved_values[index] = to_translate_val[index]
            if report is not None:
                report.record_broken(
                    rel_path, key, row_issues[index], recovered=True
                )
            # The English text is now in the target file; without the marker the
            # next run would treat it as a finished translation and keep it.
            file_key_hashes[key] = RECOVERED_HASH_MARKER
        log_cb(
            f"  Битых строк заменено оригиналом: {len(broken_indexes)} "
            "(исправьте их вручную или переведите файл заново)",
            'warning',
        )

    broken_in_file = translator.validation_report.broken_for_file(rel_path)
    for entry in broken_in_file:
        if entry["key"] not in recovered_keys:
            broken_keys.append(entry["key"])
            if report is not None:
                report.record_broken(
                    rel_path, entry["key"], entry["issues"], recovered=False
                )

    write_localization_file(dst_path, out_lines, to_translate_idx, approved_values, meta)
    _record_source_hashes(file_cache, dst_path, file_key_hashes)
    state["written"] = total

    if stale_source_keys:
        log_cb(
            f"  Исходник изменился у {len(stale_source_keys)} строк; перевод обновлён",
            'info',
        )
    if repaired_keys:
        log_cb(
            f"  {len(repaired_keys)} строк, восстановленных в прошлом прогоне, "
            "переведены заново",
            'info',
        )
    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')
    if state["reused"]:
        log_cb(f"  Из ванильного перевода: {state['reused']}", 'info')

    if broken_in_file:
        log_cb(f"  ⚠ Битых строк: {len(broken_in_file)}", 'warning')
        for b in broken_in_file[:5]:
            log_cb(f"    [{b['key']}]: {', '.join(b['issues'])}", 'warning')

    return finish("written", "", total)
