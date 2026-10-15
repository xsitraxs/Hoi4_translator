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
from .preview import PreviewWindow

# The safe default: never write a file the game may fail to parse.
_BROKEN_LINE_DEFAULT = BrokenLineMode.REJECT_FILE


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
        self._last_report_files: List[str] = []

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

        # --- УЛУЧШЕНИЕ: что делать с битыми строками + отчёт о прогоне ---
        of_report = ttk.Frame(self)
        of_report.pack(fill='x', padx=15, pady=(5, 0))
        ttk.Label(of_report, text="Битые строки:").pack(side='left')
        self.broken_line_var = tk.StringVar(
            value=broken_line_label(_BROKEN_LINE_DEFAULT)
        )
        ttk.Combobox(
            of_report, textvariable=self.broken_line_var,
            values=[label for _, label in BROKEN_LINE_MODES],
            state="readonly", width=34,
        ).pack(side='left', padx=5)
        ttk.Button(
            of_report, text="Открыть отчёт", command=self.open_last_report, width=16
        ).pack(side='right')
        self.report_info_var = tk.StringVar(value="")
        ttk.Label(
            of_report, textvariable=self.report_info_var, foreground='#74c0fc'
        ).pack(side='right', padx=5)

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
