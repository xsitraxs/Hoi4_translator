"""Main application window."""
import asyncio
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional

from .. import report as report_module
from ..config import (
    ASYNC_OK,
    DB_FILE,
    DEEPL_IMPORT_ERROR,
    DEEPL_OK,
    LOG_FILE,
    SEMAPHORE_LIMIT,
    TRANSLATOR_OK,
    __version__,
    deepl,  # noqa: F401  (re-exported for the GUI)
    logger,
)
from ..java_service import JAVA_OK, resolve_java_jar
from ..localisation import (
    HOI4_LANGUAGE_NAMES_BY_CODE,
    HOI4_TARGET_LANGS,
    collect_yml_files,
    get_hoi4_language_name,
    rename_dst,
    validate_directory_relationships,
    validate_output_path_collisions,
)
from ..pipeline import (
    BROKEN_LINE_MODES,
    BrokenLineMode,
    FileProcessStatus,
    broken_line_label,
    broken_line_mode_from_label,
    process_file_sync,
)
from ..settings import (
    load_api_key,
    load_settings,
    save_settings,
    scrub_plaintext_api_keys,
    store_api_key,
)
from ..storage import TranslationCache
from ..translation import AsyncTranslator
from . import theme
from .preview import PreviewWindow

# The safe default: never write a file the game may fail to parse.
_BROKEN_LINE_DEFAULT = BrokenLineMode.REJECT_FILE


def _enable_dpi_awareness() -> None:
    """Stop Windows from stretching the window on HiDPI screens."""
    if os.name != "nt":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        # A missing/old shcore is not a reason to refuse to start.
        pass


class App(tk.Tk):
    WINDOW_WIDTH = 1000
    WINDOW_HEIGHT = 820

    def __init__(self):
        _enable_dpi_awareness()
        super().__init__()
        self.title(f"Hoi4 Translator v{__version__}")
        self.minsize(900, 720)

        self._ui_thread = threading.current_thread()
        self._ui_queue = queue.Queue()
        self._closing = False
        self._close_when_finished = False
        self._translation_thread: Optional[threading.Thread] = None
        self._secret_warning_shown = False
        self._last_report_files: List[str] = []
        # Created in _build_ui; the theme is applied before that.
        self.log_widget: Optional[Any] = None

        self._stop_event = threading.Event()
        self.java_jar_var = tk.StringVar(value="")  # Путь к JAR

        # FIX: один async event loop на весь сеанс, запускается в фоновом потоке
        self._async_loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_event_loop, daemon=True, name="AsyncLoop"
        )
        self._loop_thread.start()

        # The theme is read before the widgets exist: Tk colours cannot be
        # changed afterwards unless every widget uses a named style.
        saved_settings = load_settings()
        self._theme_name = str(saved_settings.get("theme") or theme.DEFAULT_THEME)
        self._palette = theme.resolve(self._theme_name)
        self._apply_theme()
        self._build_ui()
        self._load_settings(saved_settings)
        self._centre_window()

        if not TRANSLATOR_OK:
            self.log_line("ВНИМАНИЕ: deep-translator не найден. pip install deep-translator", 'error')
        if not DEEPL_OK:
            # The engine is not offered at all when the client is missing, so
            # the reason has to be visible somewhere.
            detail = f" ({DEEPL_IMPORT_ERROR})" if DEEPL_IMPORT_ERROR else ""
            self.log_line(
                "Движок DeepL недоступен: pip install deepl" + detail, 'warning'
            )
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

    # --- оформление -----------------------------------------------------

    def _apply_theme(self) -> None:
        """Apply the current palette. Safe to call again after a switch."""
        theme.apply_style(self, self._palette)
        self._refresh_log_colors()

    def _refresh_log_colors(self) -> None:
        log_widget = self.log_widget
        if log_widget is None:
            return
        for tag, colour in theme.log_tag_colors(self._palette).items():
            log_widget.tag_config(tag, foreground=colour)
        log_widget.configure(
            background=self._palette.log_bg,
            foreground=self._palette.log_fg,
            insertbackground=self._palette.fg,
        )

    def _update_theme_button(self) -> None:
        other = theme.resolve(theme.opposite(self._theme_name))
        icon = "☀" if other.name == "light" else "☾"
        self.theme_btn.configure(text=f"{icon} {other.label}")

    def _toggle_theme(self) -> None:
        self._theme_name = theme.opposite(self._theme_name)
        self._palette = theme.resolve(self._theme_name)
        self._apply_theme()
        self._update_theme_button()
        self._save_current_settings()
        self.log_line(f"Тема оформления: {self._palette.label}", 'info')

    def _centre_window(self) -> None:
        """Open in the middle of the screen; never a reason to fail."""
        try:
            self.update_idletasks()
            width, height = self.WINDOW_WIDTH, self.WINDOW_HEIGHT
            x = max(0, (self.winfo_screenwidth() - width) // 2)
            y = max(0, (self.winfo_screenheight() - height) // 3)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            logger.debug("Could not centre the window", exc_info=True)

    def browse_jar(self):
        f = filedialog.askopenfilename(title="Выберите hoi4_translator.jar", filetypes=[("JAR files", "*.jar")])
        if f:
            self.java_jar_var.set(f)
            self.log_line(f"📦 Выбран JAR: {os.path.basename(f)}", 'info')

    def _build_ui(self):
        # --- шапка ---
        header = ttk.Frame(self, style='Header.TFrame', padding=(15, 10))
        header.pack(fill='x')
        ttk.Label(header, text="Hoi4 Translator", style='Header.Title.TLabel').pack(side='left')
        ttk.Label(
            header, text=f"v{__version__}", style='Header.Subtitle.TLabel'
        ).pack(side='left', padx=(8, 0), pady=(8, 0))
        ttk.Label(
            header, text="· перевод локализации Hearts of Iron IV",
            style='Header.Subtitle.TLabel',
        ).pack(side='left', padx=(6, 0), pady=(8, 0))
        self.theme_btn = ttk.Button(
            header, style='Header.Tool.TButton', command=self._toggle_theme
        )
        self.theme_btn.pack(side='right')
        self._update_theme_button()
        ttk.Separator(self).pack(fill='x')

        body = ttk.Frame(self, padding=15)
        body.pack(fill='both', expand=True)

        # --- Папки ---
        f_folders = ttk.LabelFrame(body, text=" Папки ", padding=10)
        f_folders.pack(fill='x', pady=(0, 8))
        f_folders.columnconfigure(1, weight=1)

        self.src_var = tk.StringVar()
        ttk.Label(f_folders, text="Оригинал:", width=12).grid(row=0, column=0, sticky='w')
        ttk.Entry(f_folders, textvariable=self.src_var).grid(
            row=0, column=1, sticky='ew', padx=6, pady=3
        )
        ttk.Button(
            f_folders, text="Обзор", command=self.browse_src, width=9, style='Tool.TButton'
        ).grid(row=0, column=2, pady=3)

        self.dst_var = tk.StringVar()
        ttk.Label(f_folders, text="Перевод:", width=12).grid(row=1, column=0, sticky='w')
        ttk.Entry(f_folders, textvariable=self.dst_var).grid(
            row=1, column=1, sticky='ew', padx=6, pady=3
        )
        ttk.Button(
            f_folders, text="Обзор", command=self.browse_dst, width=9, style='Tool.TButton'
        ).grid(row=1, column=2, pady=3)

        # --- Движок и языки ---
        f_engine = ttk.LabelFrame(body, text=" Движок и языки ", padding=10)
        f_engine.pack(fill='x', pady=(0, 8))

        row_jar = ttk.Frame(f_engine)
        row_jar.pack(fill='x', pady=(0, 6))
        ttk.Label(row_jar, text="Движок:", width=12).pack(side='left')
        self.engine_var = tk.StringVar(value="Google")
        engines = ["Google", "MyMemory"]
        if DEEPL_OK:
            engines.insert(1, "DeepL")
        if JAVA_OK:
            engines.append("JavaSim")

        self.engine_cb = ttk.Combobox(
            row_jar, textvariable=self.engine_var, values=engines,
            state="readonly", width=15,
        )
        self.engine_cb.pack(side='left', padx=(0, 14))
        self.engine_cb.bind("<<ComboboxSelected>>", self._toggle_api_field)

        # --- УЛУЧШЕНИЕ 3: Исходный и целевой язык ---
        ttk.Label(row_jar, text="С языка:").pack(side='left')
        self.lang_var = tk.StringVar(value="en")
        ttk.Combobox(
            row_jar, textvariable=self.lang_var,
            values=["en", "de", "fr", "es", "pl", "pt", "ru", "it", "ja", "zh-CN", "ko"],
            state="readonly", width=7,
        ).pack(side='left', padx=5)

        ttk.Label(row_jar, text="→  На язык:").pack(side='left', padx=(10, 0))
        self.target_lang_var = tk.StringVar(value="ru")
        self._target_lang_map = {label: code for code, label in HOI4_TARGET_LANGS}
        self._target_lang_rev = dict(HOI4_TARGET_LANGS)
        self.target_lang_cb = ttk.Combobox(
            row_jar, values=[label for _, label in HOI4_TARGET_LANGS],
            state="readonly", width=23,
        )
        self.target_lang_cb.set(self._target_lang_rev["ru"])
        self.target_lang_cb.pack(side='left', padx=5)
        self.target_lang_cb.bind("<<ComboboxSelected>>", self._on_target_lang_change)

        row_jar2 = ttk.Frame(f_engine)
        row_jar2.pack(fill='x', pady=(0, 6))
        ttk.Label(row_jar2, text="Java JAR:", width=12).pack(side='left')
        self.java_jar_var = tk.StringVar(value="")
        ttk.Entry(row_jar2, textvariable=self.java_jar_var).pack(
            side='left', fill='x', expand=True, padx=6
        )
        ttk.Button(
            row_jar2, text="Обзор", command=self.browse_jar, width=9, style='Tool.TButton'
        ).pack(side='left')

        row_api = ttk.Frame(f_engine)
        row_api.pack(fill='x')
        self.api_lbl = ttk.Label(row_api, text="DeepL API Key:", width=12, style='Subtle.TLabel')
        self.api_lbl.pack(side='left')
        self.api_key_var = tk.StringVar()
        self.api_entry = ttk.Entry(
            row_api, textvariable=self.api_key_var, show="*", width=40, state='disabled'
        )
        self.api_entry.pack(side='left', padx=6)
        self.verify_btn = ttk.Button(
            row_api, text="Проверить API", command=self.verify_deepl_api,
            state='disabled', width=13, style='Tool.TButton',
        )
        self.verify_btn.pack(side='left')
        self.api_status_var = tk.StringVar(value="")
        self.api_status_lbl = ttk.Label(
            row_api, textvariable=self.api_status_var, style='Muted.TLabel', width=15
        )
        self.api_status_lbl.pack(side='left', padx=6)

        # Параллельность
        row_par = ttk.Frame(f_engine)
        row_par.pack(fill='x', pady=(6, 0))
        ttk.Label(row_par, text="Параллельность:", width=12).pack(side='left')
        self.semaphore_var = tk.IntVar(value=SEMAPHORE_LIMIT)
        self.sem_lbl = ttk.Label(
            row_par, text=str(SEMAPHORE_LIMIT), width=3, style='Muted.TLabel'
        )
        self.sem_lbl.pack(side='right')
        ttk.Scale(
            row_par, from_=1, to=50, variable=self.semaphore_var,
            command=lambda v: self.sem_lbl.config(text=str(int(float(v)))),
        ).pack(side='left', fill='x', expand=True, padx=6)

        ttk.Separator(body).pack(fill='x', pady=(8, 8))

        # --- Опции ---
        of = ttk.Frame(body)
        of.pack(fill='x', pady=(0, 4))
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Пропускать переведённое", variable=self.skip_var).pack(side='left')
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Открыть папку по итогу", variable=self.open_var).pack(
            side='left', padx=15
        )
        # --- УЛУЧШЕНИЕ 7: галочка предпросмотра ---
        self.preview_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            of, text="Предпросмотр перед записью", variable=self.preview_var
        ).pack(side='left')

        # Кэш инфо
        self.cache_info_var = tk.StringVar(value="")
        ttk.Label(of, textvariable=self.cache_info_var, style='Info.TLabel').pack(side='right')
        ttk.Button(
            of, text="Инфо кэша", command=self._show_cache_info, width=11, style='Tool.TButton'
        ).pack(side='right', padx=5)
        ttk.Button(
            of, text="Очистить кэш", command=self._clear_cache, width=13, style='Tool.TButton'
        ).pack(side='right', padx=5)

        # --- Битые строки + отчёт ---
        of_report = ttk.Frame(body)
        of_report.pack(fill='x', pady=(2, 4))
        ttk.Label(of_report, text="Битые строки:", width=12).pack(side='left')
        self.broken_line_var = tk.StringVar(
            value=broken_line_label(_BROKEN_LINE_DEFAULT)
        )
        ttk.Combobox(
            of_report, textvariable=self.broken_line_var,
            values=[label for _, label in BROKEN_LINE_MODES],
            state="readonly", width=26,
        ).pack(side='left', padx=6)
        ttk.Button(
            of_report, text="Открыть отчёт", command=self.open_last_report,
            width=15, style='Tool.TButton',
        ).pack(side='right')
        self.report_info_var = tk.StringVar(value="")
        ttk.Label(of_report, textvariable=self.report_info_var, style='Info.TLabel').pack(
            side='right', padx=6
        )

        # Кэш других движков: экономит трафик, но мешает сменить движок
        of2 = ttk.Frame(body)
        of2.pack(fill='x', pady=(0, 4))
        self.reuse_fallback_cache_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            of2,
            text="Экономить трафик: переиспользовать кэш других движков "
                 "(иначе смена движка переводит заново)",
            variable=self.reuse_fallback_cache_var,
        ).pack(side='left')

        ttk.Separator(body).pack(fill='x', pady=(4, 8))

        # --- УЛУЧШЕНИЕ 2: двойной прогресс-бар ---
        f_prog = ttk.Frame(body)
        f_prog.pack(fill='x', pady=(0, 4))
        ttk.Label(f_prog, text="Файлы:", style='Muted.TLabel', width=8).pack(side='left')
        self.progress = ttk.Progressbar(
            f_prog, style='Accent.Horizontal.TProgressbar', mode='determinate'
        )
        self.progress.pack(side='left', fill='x', expand=True)
        self.progress_file_lbl = ttk.Label(f_prog, text="0/0", style='Muted.TLabel', width=8)
        self.progress_file_lbl.pack(side='left', padx=(6, 0))

        f_prog2 = ttk.Frame(body)
        f_prog2.pack(fill='x', pady=(0, 8))
        ttk.Label(f_prog2, text="Строки:", style='Muted.TLabel', width=8).pack(side='left')
        self.progress_lines = ttk.Progressbar(
            f_prog2, style='Accent.Horizontal.TProgressbar', mode='determinate'
        )
        self.progress_lines.pack(side='left', fill='x', expand=True)
        self.progress_lines_lbl = ttk.Label(
            f_prog2, text="0/0", style='Muted.TLabel', width=8
        )
        self.progress_lines_lbl.pack(side='left', padx=(6, 0))

        # --- Журнал ---
        f_log = ttk.LabelFrame(body, text=" Журнал ", padding=8)
        f_log.pack(fill='both', expand=True)
        self.log_widget = scrolledtext.ScrolledText(
            f_log, height=10, wrap='word', font=(theme.FONT_MONO, 9),
            relief='flat', borderwidth=0, highlightthickness=0, padx=8, pady=6,
            state='disabled',
        )
        self.log_widget.pack(fill='both', expand=True)
        self._refresh_log_colors()

        # --- Кнопки ---
        fb = ttk.Frame(self, padding=(15, 8))
        fb.pack(fill='x')
        self.start_btn = ttk.Button(
            fb, text="▶ НАЧАТЬ ПЕРЕВОД", style='Accent.TButton', command=self.start
        )
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 6), ipady=4)
        self.stop_btn = ttk.Button(
            fb, text="■ СТОП", state='disabled', style='Danger.TButton', command=self.stop
        )
        self.stop_btn.pack(side='left', fill='x', expand=True, padx=6, ipady=4)
        ttk.Button(
            fb, text="Очистить лог", style='Tool.TButton', command=self.clear_log
        ).pack(side='left', fill='x', expand=True, padx=(6, 0), ipady=4)

        # --- Статус-бар ---
        ttk.Separator(self).pack(fill='x')
        status_bar = ttk.Frame(self, style='Panel.TFrame', padding=(15, 6))
        status_bar.pack(fill='x')
        self.status_var = tk.StringVar(value="Готов к работе")
        self.speed_var = tk.StringVar(value="")
        ttk.Label(
            status_bar, textvariable=self.status_var, style='Panel.Muted.TLabel'
        ).pack(side='left')
        ttk.Label(
            status_bar, textvariable=self.speed_var, style='Panel.Success.TLabel'
        ).pack(side='right')

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
            self.api_lbl.config(style='TLabel')
            self.verify_btn.config(state='normal')
        else:
            self.api_entry.config(state='disabled')
            self.api_lbl.config(style='Subtle.TLabel')
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

    def _write_run_report(self, report, dst: str) -> bool:
        """Write report.csv/json next to the translation folder."""
        try:
            written = report.write(dst)
        except Exception:
            logger.exception("Run report failed")
            return False
        if not written:
            return False
        self._last_report_files = list(written)
        self.log_line(
            f"Отчёт о прогоне: {os.path.basename(written[0])}", 'info'
        )
        self._post_ui(self.report_info_var.set, os.path.basename(written[0]))
        return True

    def open_last_report(self):
        """Open the newest run report; tell the user where it lives if none yet."""
        for path in self._last_report_files:
            if os.path.exists(path):
                try:
                    os.startfile(path)
                    return
                except Exception:
                    logger.exception("Report open failed: %s", path)
                    break
        self._open_output_folder()
        messagebox.showinfo(
            "Отчёт о прогоне",
            "Отчёт создаётся по завершении перевода и лежит рядом с папкой "
            "перевода (hoi4_translator_report_*.csv / .json).",
        )

    def _open_output_folder(self):
        directory = self.dst_var.get().strip()
        if not directory or not os.path.isdir(directory):
            return False
        try:
            os.startfile(directory)
        except Exception:
            logger.debug("Не удалось открыть папку перевода: %s", directory)
            return False
        return True

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
            self.api_status_lbl.config(style='Error.TLabel')
            return
        if not DEEPL_OK or deepl is None:
            self.api_status_var.set("Нет клиента")
            self.api_status_lbl.config(style='Error.TLabel')
            return

        self.api_status_var.set("Проверка...")
        self.api_status_lbl.config(style='Warning.TLabel')
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
                self.api_status_lbl.config(style='Success.TLabel')
                self.log_line("DeepL API ключ успешно проверен!", 'success')
            else:
                self.api_status_var.set(f"✗ {msg}")
                self.api_status_lbl.config(style='Error.TLabel')
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

    def _load_settings(self, settings: Optional[Dict[str, Any]] = None):
        s = settings if settings is not None else load_settings()
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
        if s.get('broken_lines'):
            # An unknown label falls back to the safe mode.
            self.broken_line_var.set(
                broken_line_label(
                    broken_line_mode_from_label(str(s['broken_lines']))
                )
            )
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
            'broken_lines': self.broken_line_var.get(),
            'theme': self._theme_name,
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
                detail = f" ({DEEPL_IMPORT_ERROR})" if DEEPL_IMPORT_ERROR else ""
                self.log_line(
                    "DeepL недоступен: установите deepl (pip install deepl)" + detail,
                    'error',
                )
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
            'broken_lines': self.broken_line_var.get(),
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
        broken_line_mode = broken_line_mode_from_label(
            config.get('broken_lines', '')
        )
        report = report_module.RunReport(engine_name, source_ln, target_ln, src, dst)
        report_written = False

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
                                        palette=self._palette,
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
                        broken_line_mode=broken_line_mode, report=report,
                        report_label=rel,
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

            report.set_fallbacks(getattr(translator, "fallback_counts", {}))
            report_written = self._write_run_report(report, dst)
            if open_output and completed_files and not self._stop_event.is_set():
                try:
                    os.startfile(dst)
                except Exception:
                    pass
        except Exception as e:
            logger.exception("Critical error in translation thread")
            self.log_line(f"Критическая ошибка: {e}", 'error')
        finally:
            if not report_written:
                # A crash or a stop still leaves a useful trail: what ran and
                # what failed is exactly what the report is for.
                self._write_run_report(report, dst)
            if 'translator' in locals():
                try:
                    asyncio.run_coroutine_threadsafe(
                        translator.__aexit__(None, None, None), self._async_loop
                    ).result()
                except Exception:
                    logger.exception("Ошибка при закрытии ресурсов")
            self._post_ui(self._translation_finished)
