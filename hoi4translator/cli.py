"""Headless mode: translate a folder without opening a window.

The GUI is the way most people use the tool; the CLI is the way a mod team
automates it — run it in CI, in a pre-commit hook, or over a whole workshop
folder on a server without a display.

    python hoi4_translator.py --cli --src MOD/localisation/english \\
        --dst OUT/localisation/russian --target ru --engine Google

Exit codes: 0 — everything written, 1 — at least one file failed, 2 — the
command line or the folders are wrong (nothing was started).
"""
import argparse
import asyncio
import os
import sys
import threading
from typing import Dict, List, Optional, Sequence

from .config import SEMAPHORE_LIMIT, __version__, logger
from .glossary import GlossaryError, load_glossary
from .localisation import (
    HOI4_TARGET_LANGS,
    collect_yml_files,
    rename_dst,
    validate_directory_relationships,
    validate_output_path_collisions,
)
from .pipeline import BrokenLineMode, process_file_sync
from .report import RunReport
from .vanilla import build_vanilla_index

ENGINES = ("Google", "DeepL", "MyMemory", "JavaSim")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hoi4_translator.py --cli",
        description="Перевод локализации мода без графического интерфейса.",
    )
    parser.add_argument("--src", required=True, help="папка мода или localisation с исходниками")
    parser.add_argument("--dst", required=True, help="куда писать перевод (тот же уровень корня)")
    parser.add_argument("--target", default="ru", help="язык перевода (ru, en, de, ...)")
    parser.add_argument("--source-lang", default="en", help="язык исходников (по умолчанию en)")
    parser.add_argument("--engine", default="Google", choices=ENGINES, help="движок перевода")
    parser.add_argument("--api-key", default=None, help="ключ DeepL (или переменная DEEPL_API_KEY)")
    parser.add_argument("--glossary", default=None, help="файл глоссария (TSV/CSV/JSON)")
    parser.add_argument(
        "--vanilla", default=None,
        help="папка localisation установленной игры: готовые официальные переводы "
             "переиспользуются вместо машинного перевода",
    )
    parser.add_argument("--concurrency", type=int, default=SEMAPHORE_LIMIT,
                        help=f"параллельных запросов (по умолчанию {SEMAPHORE_LIMIT})")
    parser.add_argument("--skip-translated", action="store_true",
                        help="не переводить строки, уже переведённые в папке результата")
    parser.add_argument("--broken-lines", choices=("reject", "keep-original"),
                        default="reject",
                        help="что делать со строкой, сломавшей формат локализации")
    parser.add_argument("--no-report", action="store_true", help="не создавать JSON/CSV отчёт")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только проверить файлы: разобрать, найти битые строки и дубликаты ключей, "
             "ничего не переводить и не писать (игра и интернет не нужны)",
    )
    parser.add_argument("--quiet", action="store_true", help="только итог, без строк файлов")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _translator_factory():
    # Imported lazily: a --version run must not pull in aiohttp and DeepL.
    from .translation import AsyncTranslator

    return AsyncTranslator


async def _translate(args, files: List[str], output_paths, glossary, vanilla) -> int:
    AsyncTranslator = _translator_factory()
    api_key = args.api_key or os.environ.get("DEEPL_API_KEY")
    translator = AsyncTranslator(
        args.engine, api_key=api_key, source_lang=args.source_lang,
        concurrency=args.concurrency, glossary=glossary,
    )
    await translator.__aenter__()

    report = None if args.no_report else RunReport(
        args.engine, args.source_lang, args.target, args.src, args.dst
    )
    stop_event = threading.Event()
    failures = 0
    written = 0

    def log_cb(message, tag=None):
        if args.quiet and not message.startswith(("  Ошибка", "  Файл", "  Превышено")):
            return
        prefix = {"error": "! ", "warning": "~ "}.get(tag or "", "  ")
        print(prefix + message, flush=True)

    try:
        loop = asyncio.get_running_loop()
        mode = (
            BrokenLineMode.REJECT_FILE if args.broken_lines == "reject"
            else BrokenLineMode.KEEP_ORIGINAL
        )

        def work():
            nonlocal failures, written
            for index, source in enumerate(files, start=1):
                relative = os.path.relpath(source, args.src)
                print(f"[{index}/{len(files)}] {relative}", flush=True)
                result = process_file_sync(
                    source, output_paths[source], translator, log_cb,
                    args.skip_translated, stop_event, loop,
                    target_lang=args.target,
                    broken_line_mode=mode,
                    report=report, report_label=relative,
                    vanilla=vanilla,
                )
                if isinstance(result, int) and result >= 0:
                    written += 1
                elif result is None:
                    failures += 1
            return failures

        failures = await asyncio.to_thread(work)
    finally:
        await translator.__aexit__(None, None, None)

    if report is not None:
        files_written = report.write(args.dst)
        for path in files_written or ():
            print(f"Отчёт: {path}", flush=True)

    print(
        f"Готово: файлов записано {written}, ошибок {failures}", flush=True
    )
    return 0 if failures == 0 else 1


def _dry_run(args, files: List[str], output_paths: Dict[str, str]) -> int:
    """Parse everything and report problems; no engine, no writes."""
    from .inspect import format_report, inspect_files

    inspections = inspect_files(files, output_paths)
    for line in format_report(inspections, args.src):
        print(line, flush=True)
    return 1 if any(inspection.has_problems for inspection in inspections) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    supported = {code for code, _name in HOI4_TARGET_LANGS}
    if args.target not in supported:
        return _fail(
            f"Неизвестный язык перевода: {args.target}. "
            f"Доступны: {', '.join(sorted(supported))}"
        )
    if not os.path.isdir(args.src):
        return _fail(f"Папка исходников не найдена: {args.src}")
    try:
        validate_directory_relationships(args.src, args.dst)
    except ValueError as error:
        return _fail(str(error))

    files = collect_yml_files(args.src)
    if not files:
        return _fail(f"В папке нет файлов .yml: {args.src}")
    pairs = [(path, rename_dst(path, args.src, args.dst, args.target)) for path in files]
    try:
        validate_output_path_collisions(pairs)
    except ValueError as error:
        return _fail(str(error))

    try:
        glossary = load_glossary(args.glossary)
    except GlossaryError as error:
        return _fail(str(error))

    if args.dry_run:
        return _dry_run(args, files, dict(pairs))

    vanilla = build_vanilla_index(
        args.vanilla, args.source_lang, args.target, log_cb=print
    )

    print(
        f"Hoi4 Translator {__version__} | {args.engine} | "
        f"{args.source_lang} → {args.target} | файлов: {len(files)}",
        flush=True,
    )
    if glossary:
        print(f"Глоссарий: {glossary.origin}, терминов: {len(glossary)}", flush=True)
    if vanilla:
        print(f"Ванильных переводов в индексе: {len(vanilla)}", flush=True)

    try:
        return asyncio.run(_translate(args, files, dict(pairs), glossary, vanilla))
    except KeyboardInterrupt:
        print("Прервано пользователем", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - the CLI must not traceback at users
        logger.exception("CLI run failed")
        print(f"Ошибка: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


__all__ = ["build_parser", "main"]
