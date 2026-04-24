import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import threading
import asyncio
import os
import re
import time
import json
import sqlite3
import logging
import hashlib
from datetime import datetime
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

# Попытка импорта асинхронных библиотек для ускорения
try:
    import aiohttp
    ASYNC_OK = True
except ImportError:
    ASYNC_OK = False

try:
    from deep_translator import GoogleTranslator, MyMemoryTranslator, DeepL
    TRANSLATOR_OK = True
except ImportError:
    TRANSLATOR_OK = False

# Попытка импорта JPype для Java-интеграции
try:
    import jpype
    import jpype.imports
    JAVA_OK = True
except ImportError:
    JAVA_OK = False

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ И ЛОГИРОВАНИЕ
# ---------------------------------------------------------------------------

LOG_FILE = os.path.join(os.path.dirname(__file__), 'hoi4_translator.log')
DB_FILE = os.path.join(os.path.dirname(__file__), 'translation_cache.db')
SETTINGS_FILE = os.path.join(os.path.expanduser('~'), '.hoi4_translator_settings.json')

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# КОНСТАНТЫ И РЕГУЛЯРКИ
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(
    r'(\$[^$]+\$|[\[\w\.\[\]]+\]|§[A-Za-z!]|\\n|\\t|£\w+|@\w+[\[!]?|\])'
)
VALUE_RE = re.compile(r'^(\s*\S+:\d*\s*)"(.+)"(.*)$')
CYRILLIC_RE = re.compile(r'[а-яёА-ЯЁ]')

# ---------------------------------------------------------------------------
# JAVA ИНТЕГРАЦИЯ (ЭМУЛЯЦИЯ / ЗАГОТОВКА)
# ---------------------------------------------------------------------------

class JavaTranslatorService:
    """
    Класс для взаимодействия с Java-сервисом перевода.
    В реальном проекте здесь будет инициализация JVM и вызов Java-методов.
    """
    def __init__(self):
        self.java_available = JAVA_OK
        if self.java_available:
            logger.info("Java integration module loaded (JPype available).")
            # Здесь была бы инициализация:
            # if not jpype.isJVMStarted():
            #     jpype.startJVM(jpype.getDefaultJVMPath(), '-Djava.class.path=translator.jar')
        else:
            logger.warning("JPype not installed. Java features disabled.")

    def translate_batch_java(self, texts: List[str], target_lang: str = "ru") -> List[str]:
        """
        Эмулирует вызов Java-метода для пакетного перевода.
        В реальности: вызывает static method JavaClass.translateBatch(...)
        """
        if not self.java_available:
            raise NotImplementedError("Java runtime not available.")

        # ЭМУЛЯЦИЯ: просто возвращаем заглушки, чтобы показать работу потока
        # В реальности тут был бы вызов через JNI/JPype
        logger.info(f"Calling Java service for {len(texts)} items...")
        time.sleep(0.1) # Имитация быстрого нативного вызова
        return [f"[JAVA_TRANSLATED]{t}" for t in texts]

    def shutdown(self):
        if self.java_available and jpype.isJVMStarted():
            jpype.shutdownJVM()
            logger.info("JVM shut down.")

# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ (КЭШИРОВАНИЕ)
# ---------------------------------------------------------------------------

class TranslationCache:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
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
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hash ON cache(hash_key)')
        conn.commit()
        conn.close()

    def _get_hash(self, text: str, src: str, tgt: str, engine: str) -> str:
        key_str = f"{text}|{src}|{tgt}|{engine}"
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()

    def get(self, text: str, src: str, tgt: str, engine: str) -> Optional[str]:
        h = self._get_hash(text, src, tgt, engine)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT translated_text FROM cache WHERE hash_key = ?', (h,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def set(self, text: str, translation: str, src: str, tgt: str, engine: str):
        h = self._get_hash(text, src, tgt, engine)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO cache
            (hash_key, original_text, translated_text, source_lang, target_lang, engine_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (h, text, translation, src, tgt, engine, time.time()))
        conn.commit()
        conn.close()

# ---------------------------------------------------------------------------
# ЛОГИКА ПЕРЕВОДА (ASYNC + PROTECTED)
# ---------------------------------------------------------------------------

def protect_placeholders(text: str):
    tokens = []
    def replacer(m):
        tokens.append(m.group(0))
        return f'<<PH{len(tokens)-1}>>'
    return PLACEHOLDER_RE.sub(replacer, text), tokens

def restore_placeholders(text: str, tokens: List[str]) -> str:
    def replacer(m):
        try:
            idx = int(m.group(1))
            return tokens[idx] if idx < len(tokens) else m.group(0)
        except Exception:
            return m.group(0)
    return re.sub(r'<<PH(\d+)>>', replacer, text)

class AsyncTranslator:
    def __init__(self, engine: str, api_key: Optional[str] = None, source_lang: str = "en"):
        self.engine = engine
        self.api_key = api_key
        self.source_lang = source_lang
        self.session = None
        self.cache = TranslationCache(DB_FILE)
        self.java_service = JavaTranslatorService()

    async def __aenter__(self):
        if ASYNC_OK:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
        self.java_service.shutdown()

    async def translate_text_async(self, text: str, target_lang: str = "ru", retries: int = 3) -> str:
        # Проверка кэша
        cached = self.cache.get(text, self.source_lang, target_lang, self.engine)
        if cached:
            logger.debug(f"Cache hit for: {text[:20]}...")
            return cached

        # Подготовка
        protected, tokens = protect_placeholders(text)
        result_text = protected

        # Логика по движкам
        try:
            if self.engine == "Google" and ASYNC_OK:
                result_text = await self._google_async(protected, target_lang, retries)
            elif self.engine == "DeepL" and self.api_key:
                # DeepL пока синхронно в отдельном потоке, т.к. оф. библиотека не async
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor() as pool:
                    result_text = await loop.run_in_executor(
                        pool,
                        lambda: self._deepl_sync(protected, target_lang, retries)
                    )
            elif self.engine == "JavaSim":
                 # Тест_java режима
                 loop = asyncio.get_event_loop()
                 with ThreadPoolExecutor() as pool:
                     res_list = await loop.run_in_executor(
                         pool,
                         lambda: self.java_service.translate_batch_java([protected], target_lang)
                     )
                     result_text = res_list[0]
            else:
                # Фоллбек на синхронный Google/MyMemory
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor() as pool:
                    result_text = await loop.run_in_executor(
                        pool,
                        lambda: self._sync_fallback(protected, target_lang, retries)
                    )

            final_text = restore_placeholders(result_text, tokens)

            # Сохранение в кэш
            if final_text != protected: # Кэшируем только если что-то изменилось
                self.cache.set(text, final_text, self.source_lang, target_lang, self.engine)

            return final_text

        except Exception as e:
            logger.error(f"Translation error for '{text}': {e}")
            return text # Возвращаем оригинал при ошибке

    async def _google_async(self, text: str, target: str, retries: int) -> str:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            'client': 'gtx', 'sl': self.source_lang, 'tl': target, 'dt': 't', 'q': text
        }
        for attempt in range(retries):
            try:
                async with self.session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return ''.join([sentence[0] for sentence in data[0] if sentence[0]])
                    elif resp.status == 429:
                        wait_time = (2 ** attempt) * 1.5
                        logger.warning(f"Rate limited. Waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        resp.raise_for_status()
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                await asyncio.sleep((2 ** attempt) * 1.0)
        return text

    def _deepl_sync(self, text: str, target: str, retries: int) -> str:
        translator = DeepL(api_key=self.api_key, source=self.source_lang, target=target, use_pro=False)
        for attempt in range(retries):
            try:
                res = translator.translate(text)
                return res if res else text
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    raise e
        return text

    def _sync_fallback(self, text: str, target: str, retries: int) -> str:
        if self.engine == "MyMemory":
            mm_map = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "pl": "pl-PL"}
            translator = MyMemoryTranslator(source=mm_map.get(self.source_lang, "en-US"), target="ru-RU")
        else:
            translator = GoogleTranslator(source=self.source_lang, target=target)

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

    async def translate_batch(self, texts: List[str], target_lang: str = "ru", batch_size: int = 10) -> List[str]:
        # Разбиваем на мелкие пачки для параллелизма внутри батча
        tasks = [self.translate_text_async(t, target_lang) for t in texts]
        # Ограничиваем одновременные запросы (semaphore можно добавить, но тут просто gather)
        return await asyncio.gather(*tasks)

# ---------------------------------------------------------------------------
# ОБРАБОТКА ФАЙЛОВ
# ---------------------------------------------------------------------------

def has_cyrillic(text):
    return bool(CYRILLIC_RE.search(text))

def load_existing_translations(dst_path: str) -> Dict[str, str]:
    existing = {}
    if not os.path.exists(dst_path):
        return existing
    try:
        with open(dst_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                m = VALUE_RE.match(line)
                if m and has_cyrillic(m.group(2)):
                    existing[m.group(1).strip()] = m.group(2)
    except Exception as e:
        logger.error(f"Error reading existing translations: {e}")
    return existing

def process_file_sync(src_path: str, dst_path: str, translator: AsyncTranslator,
                      log_cb, skip_translated: bool, stop_event: threading.Event) -> Optional[int]:

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    existing = load_existing_translations(dst_path) if skip_translated else {}

    if existing:
        log_cb(f"  Найдено переведённых строк в кэше файла: {len(existing)}", 'info')

    with open(src_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    out_lines = list(lines)
    to_translate_idx = []
    to_translate_val = []
    meta = []
    skipped = 0

    # Парсинг
    for i, line in enumerate(lines):
        if stop_event.is_set():
            return None

        if i == 0 and line.strip().startswith('l_'):
            out_lines[i] = re.sub(r'^(\s*)l_[a-z]+:', r'\1l_russian:', line)
            continue

        m = VALUE_RE.match(line)
        if not m:
            continue

        key, prefix, value, suffix = m.group(1).strip(), m.group(1), m.group(2), m.group(3)

        if skip_translated and key in existing:
            out_lines[i] = f'{prefix}"{existing[key]}"{suffix}\n'
            skipped += 1
        else:
            to_translate_idx.append(i)
            to_translate_val.append(value)
            meta.append((prefix, suffix))

    total = len(to_translate_val)
    if total == 0:
        return 0

    # Асинхронный запуск в отдельном потоке
    async def run_translate():
        async with translator:
            return await translator.translate_batch(to_translate_val, "ru")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        translated_vals = loop.run_until_complete(run_translate())
        loop.close()
    except Exception as e:
        log_cb(f"  Критическая ошибка асинхронности: {e}", 'error')
        return None

    # Запись результатов
    for line_idx, translated, (pfx, sfx) in zip(to_translate_idx, translated_vals, meta):
        out_lines[line_idx] = f'{pfx}"{translated}"{sfx}\n'

    with open(dst_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(out_lines)

    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')

    return total

def rename_dst(src_path: str, src_root: str, dst_root: str) -> str:
    rel = os.path.relpath(src_path, src_root)
    rel_renamed = re.sub(r'_l_[a-z]+\.yml$', '_l_russian.yml', rel)
    rel_renamed = re.sub(
        r'(?i)(^|[\\/])(english|german|french|spanish|polish)([\\/])',
        r'\1russian\3', rel_renamed
    )
    return os.path.join(dst_root, rel_renamed)

def collect_yml_files(root: str) -> List[str]:
    res = []
    if not os.path.exists(root):
        return res
    for d, _, fns in os.walk(root):
        for f in sorted(fns):
            if f.endswith('.yml') and f.lower() != 'languages.yml':
                res.append(os.path.join(d, f))
    return res

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Settings load error: {e}")
    return {}

def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Settings save error: {e}")

# ---------------------------------------------------------------------------
# GUI (ПРОДВИНУТЫЙ)
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HOI4 Localisation Translator PRO v2.0 (Async+Cache+Java)")
        self.minsize(800, 700)
        self.configure(bg='#1a1a1a')

        self._stop_event = threading.Event()
        self._setup_styles()
        self._build_ui()
        self._load_settings()

        if not TRANSLATOR_OK and not ASYNC_OK:
            self.log_line("ВНИМАНИЕ: deep-translator не найден. Работает только режим кэша.", 'error')

        if ASYNC_OK:
            self.log_line("Режим ускорения: AIOHTTP активен.", 'success')
        else:
            self.log_line("Режим ускорения: Стандартный (установите aiohttp для скорости).", 'info')

        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        style.map('TCombobox', fieldbackground=[('readonly', BG2)], selectbackground=[('readonly', BG2)], selectforeground=[('readonly', FG)])
        style.configure('green.Horizontal.TProgressbar', troughcolor=BG2, background='#4caf50')
        style.configure('TCheckbutton', background=BG, foreground=FG)
        style.map('TCheckbutton', background=[('active', BG)], foreground=[('active', FG)])
        style.configure('TScale', background=BG, troughcolor=BG2)

    def _build_ui(self):
        pad = {'padx': 15, 'pady': 5}
        ttk.Label(self, text="HOI4 Translator PRO", font=('Segoe UI', 18, 'bold'), foreground='#4caf50').pack(pady=10)

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
        f_engine = ttk.LabelFrame(self, text=" Движок и API ", padding=10)
        f_engine.pack(fill='x', **pad)

        row1 = ttk.Frame(f_engine); row1.pack(fill='x')
        ttk.Label(row1, text="Движок:").pack(side='left')
        self.engine_var = tk.StringVar(value="Google")
        engines = ["Google", "DeepL", "MyMemory"]
        if JAVA_OK:
            engines.append("JavaSim (Test)")

        self.engine_cb = ttk.Combobox(row1, textvariable=self.engine_var, values=engines, state="readonly", width=15)
        self.engine_cb.pack(side='left', padx=5)
        self.engine_cb.bind("<<ComboboxSelected>>", self._toggle_api_field)

        ttk.Label(row1, text="С языка:").pack(side='left', padx=(15, 0))
        self.lang_var = tk.StringVar(value="en")
        ttk.Combobox(row1, textvariable=self.lang_var, values=["en", "de", "fr", "es", "pl"], state="readonly", width=5).pack(side='left', padx=5)

        row2 = ttk.Frame(f_engine); row2.pack(fill='x', pady=(10, 0))
        self.api_lbl = ttk.Label(row2, text="DeepL API Key:", foreground='#555')
        self.api_lbl.pack(side='left')
        self.api_key_var = tk.StringVar()
        self.api_entry = ttk.Entry(row2, textvariable=self.api_key_var, show="*", width=40, state='disabled')
        self.api_entry.pack(side='left', padx=5)

        # Параметры
        f_params = ttk.Frame(self); f_params.pack(fill='x', **pad)
        ttk.Label(f_params, text="Параллельность (батч):").pack(side='left')
        self.batch_var = tk.IntVar(value=30)
        self.blbl = ttk.Label(f_params, text="30", width=3)
        self.blbl.pack(side='right')
        ttk.Scale(f_params, from_=1, to=100, variable=self.batch_var, command=lambda v: self.blbl.config(text=str(int(float(v))))).pack(side='right', fill='x', expand=True, padx=10)

        # Опции
        of = ttk.Frame(self); of.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Пропускать переведенное (файл+БД)", variable=self.skip_var).pack(side='left')
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Открыть папку по итогу", variable=self.open_var).pack(side='left', padx=20)

        # Прогресс
        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=15, pady=(10, 2))

        f_status = ttk.Frame(self); f_status.pack(fill='x', padx=15)
        self.status_var = tk.StringVar(value="Готов к работе")
        self.speed_var = tk.StringVar(value="Скорость: 0 строк/сек")
        ttk.Label(f_status, textvariable=self.status_var, foreground='#aaa').pack(side='left')
        ttk.Label(f_status, textvariable=self.speed_var, foreground='#4caf50').pack(side='right')

        # Лог
        self.log_widget = scrolledtext.ScrolledText(self, height=12, bg='#111', fg='#ccc', font=('Consolas', 9), state='disabled')
        self.log_widget.pack(fill='both', expand=True, padx=15, pady=(5, 5))
        self.log_widget.tag_config('error', foreground='#ff6b6b')
        self.log_widget.tag_config('success', foreground='#69db7c')
        self.log_widget.tag_config('info', foreground='#74c0fc')
        self.log_widget.tag_config('warning', foreground='#ffa500')

        # Кнопки
        fb = ttk.Frame(self); fb.pack(fill='x', padx=15, pady=(0, 10))
        self.start_btn = ttk.Button(fb, text="НАЧАТЬ ПЕРЕВОД", command=self.start)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 4), ipady=5)
        self.stop_btn = ttk.Button(fb, text="СТОП", state='disabled', command=self.stop)
        self.stop_btn.pack(side='left', fill='x', expand=True, padx=4, ipady=5)
        ttk.Button(fb, text="Очистить лог", command=self.clear_log).pack(side='left', fill='x', expand=True, padx=(4, 0), ipady=5)

    def _toggle_api_field(self, event=None):
        if self.engine_var.get() == "DeepL":
            self.api_entry.config(state='normal')
            self.api_lbl.config(foreground='#fff')
        else:
            self.api_entry.config(state='disabled')
            self.api_lbl.config(foreground='#555')

    def browse_src(self):
        d = filedialog.askdirectory(title="Выбери папку с оригинальной локализацией")
        if d:
            self.src_var.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(title="Выбери папку для перевода")
        if d:
            self.dst_var.set(d)

    def log_line(self, text, tag=None):
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

    def stop(self):
        self._stop_event.set()
        self.status_var.set("Остановка...")
        self.stop_btn.config(state='disabled')

    def _load_settings(self):
        s = load_settings()
        if s.get('src'): self.src_var.set(s['src'])
        if s.get('dst'): self.dst_var.set(s['dst'])
        if s.get('api_key'): self.api_key_var.set(s['api_key'])
        if s.get('engine'): self.engine_var.set(s['engine'])
        if s.get('lang'): self.lang_var.set(s['lang'])
        if s.get('batch'):
            self.batch_var.set(s['batch'])
            self.blbl.config(text=str(s['batch']))
        if s.get('skip') is not None: self.skip_var.set(s['skip'])
        if s.get('open') is not None: self.open_var.set(s['open'])
        self._toggle_api_field()

    def _on_close(self):
        save_settings({
            'src': self.src_var.get(), 'dst': self.dst_var.get(),
            'api_key': self.api_key_var.get(), 'engine': self.engine_var.get(),
            'lang': self.lang_var.get(), 'batch': int(self.batch_var.get()),
            'skip': self.skip_var.get(), 'open': self.open_var.get(),
        })
        self.destroy()

    def start(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not dst:
            self.log_line("Укажи обе папки!", 'error')
            return
        if not os.path.isdir(src):
            self.log_line(f"Папка не найдена: {src}", 'error')
            return
        if self.engine_var.get() == "DeepL" and not self.api_key_var.get():
            self.log_line("Ошибка: нужен DeepL API Key!", 'error')
            return

        self._stop_event.clear()
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.progress['value'] = 0

        save_settings({
            'src': src, 'dst': dst, 'api_key': self.api_key_var.get(),
            'engine': self.engine_var.get(), 'lang': self.lang_var.get(),
            'batch': int(self.batch_var.get()), 'skip': self.skip_var.get(),
            'open': self.open_var.get(),
        })

        threading.Thread(target=self.run_translation, daemon=True).start()

    def run_translation(self):
        engine_name = self.engine_var.get()
        source_ln = self.lang_var.get()
        key = self.api_key_var.get()
        batch_size = int(self.batch_var.get())
        src = self.src_var.get()
        dst = self.dst_var.get()

        start_t = time.time()
        total_lines = 0
        errors = 0

        self.log_line(f"Запуск процесса... Движок: {engine_name}, Батч: {batch_size}", 'info')

        try:
            translator = AsyncTranslator(engine_name, api_key=key, source_lang=source_ln)
            files = collect_yml_files(src)
            total = len(files)

            if total == 0:
                self.log_line("Файлы .yml не найдены!", 'error')
                self.status_var.set("Нет файлов")
                self.start_btn.config(state='normal')
                self.stop_btn.config(state='disabled')
                return

            self.progress['maximum'] = total

            for i, fp in enumerate(files):
                if self._stop_event.is_set():
                    self.log_line("[СТОП] Перевод остановлен пользователем.", 'warning')
                    break

                rel = os.path.relpath(fp, src)
                self.status_var.set(f"Файл {i+1}/{total}")
                self.log_line(f"[{i+1}/{total}] {rel}")

                dst_path = rename_dst(fp, src, dst)
                try:
                    n = process_file_sync(fp, dst_path, translator, self.log_line, self.skip_var.get(), self._stop_event)
                    if n is None:
                        break
                    total_lines += n

                    # Расчет скорости
                    elapsed = time.time() - start_t
                    if elapsed > 0:
                        speed = total_lines / elapsed
                        self.speed_var.set(f"Скорость: {speed:.1f} строк/сек")

                    self.log_line(f"  OK — строк: {n}", 'success')
                except Exception as e:
                    self.log_line(f"  ОШИБКА обработки файла: {e}", 'error')
                    errors += 1

                self.progress['value'] = i + 1
                self.update_idletasks()

            elapsed = int(time.time() - start_t)
            m_, s_ = divmod(elapsed, 60)
            summary = f"Готово! Файлов: {total} | строк: {total_lines} | {m_:02d}:{s_:02d}"
            if errors:
                summary += f" | ошибок: {errors}"

            self.status_var.set(summary)
            self.speed_var.set("")
            self.log_line(f"\n{'='*50}", 'info')
            self.log_line(f"  {summary}", 'success' if not errors else 'error')
            self.log_line(f"{'='*50}", 'info')

            if self.open_var.get() and not self._stop_event.is_set():
                try:
                    os.startfile(dst)
                except Exception:
                    pass

        except Exception as e:
            logger.exception("Critical error in translation thread")
            self.log_line(f"Критическая ошибка: {e}", 'error')
        finally:
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')

if __name__ == '__main__':
    # Проверка зависимостей при старте
    if not TRANSLATOR_OK:
        print("WARNING: deep-translator not found. Install via: pip install deep-translator")
    if not ASYNC_OK:
        print("INFO: aiohttp not found. Speed will be lower. Install via: pip install aiohttp")

    App().mainloop()
