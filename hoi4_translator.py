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
    r'(\$[^$]+\$|\[[^\]]*\]|§[A-Za-z!]|\\n|\\t|£\w+|@\w+[\[!]?)'
)

# FIX: VALUE_RE теперь матчит KEY: "text" и KEY:0 "text" и KEY:0"text"
VALUE_RE = re.compile(r'^(\s*\S+:\d*\s*)"(.+)"(.*)$')

CYRILLIC_RE = re.compile(r'[а-яёА-ЯЁ]')

# Лимит параллельных запросов к переводчику (защита от бана)
SEMAPHORE_LIMIT = 15

# ---------------------------------------------------------------------------
# 5. ПОСТОБРАБОТКА ПЕРЕВОДА
# ---------------------------------------------------------------------------

class PostProcessor:
    """
    Исправляет типичные косяки машинного перевода в HoI4 локализации.
    Все правила применяются последовательно после получения перевода.
    """

    # Пробел между цветовым кодом (не §W и не §!) и следующей буквой/цифрой
    # §W и §! — сброс цвета, пробел после них важен
    _COLOR_CODE_RE = re.compile(r'(§[A-VX-Za-vx-z])\s+(\S)')
    # Двойные пробелы
    # Двойные пробелы
    _DOUBLE_SPACE_RE = re.compile(r'  +')
    # Пробел перед знаком препинания (русская типографика)
    _SPACE_BEFORE_PUNCT_RE = re.compile(r' ([,;:!?»])')
    # Пробел после открывающей кавычки
    _SPACE_AFTER_OPEN_QUOTE_RE = re.compile(r'«\s+')
    # Пробел перед закрывающей кавычкой
    _SPACE_BEFORE_CLOSE_QUOTE_RE = re.compile(r'\s+»')
    # Английские кавычки → русские (только если нет плейсхолдеров)
    _ENG_QUOTE_RE = re.compile(r'"([^"]+)"')
    # Артефакт перевода: <<PH…>> который не восстановился (на всякий)
    _LEFTOVER_PH_RE = re.compile(r'<<PH\d+>>')
    # Пробелы вокруг \n (литерального)
    _SPACE_AROUND_NEWLINE_RE = re.compile(r'\s*\\n\s*')
    # Первая буква после §X должна быть заглавной (если это начало слова)
    _LOWER_AFTER_COLOR_RE = re.compile(r'(§[A-Za-z!])([а-яё])')

    @classmethod
    def process(cls, text: str, original: str) -> str:
        """
        Применяет все правила постобработки.
        original передаётся для проверок (например, сохранение регистра).
        """
        if not text:
            return text

        # 1. Убираем пробелы вокруг \\n (HoI4 literal newline)
        text = cls._SPACE_AROUND_NEWLINE_RE.sub(r'\\n', text)

        # 2. Двойные пробелы → одинарный (ДО §-правил — сохраняем пробельный контекст)
        text = cls._DOUBLE_SPACE_RE.sub(' ', text)

        # 3+4. Капитализация + удаление пробела после открывающего §X в одном шаге.
        #   §W и §! — сброс цвета, их не трогаем (пробел после них сохраняется).
        #   Условие: §X стоит в начале строки или после пробела (т.е. начинает слово).
        def _fix_color(m):
            code, space, letter = m.group(1), m.group(2), m.group(3)
            return code + letter.upper()  # пробел между кодом и буквой убираем, букву — заглавная
        text = re.sub(
            r'(?:(?<=\s)|(?<=^))(§[A-VX-Za-vx-z])(\s*)([а-яё])',
            _fix_color, text
        )
        # Для §X без пробела (вдруг уже слитно) — тоже убираем лишний пробел если есть
        text = re.sub(r'(§[A-VX-Za-vx-z])\s+(\S)', r'\1\2', text)

        # 5. Убираем пробел перед знаком препинания
        text = cls._SPACE_BEFORE_PUNCT_RE.sub(r'\1', text)

        # 6. Пробелы внутри кавычек-ёлочек
        text = cls._SPACE_AFTER_OPEN_QUOTE_RE.sub('«', text)
        text = cls._SPACE_BEFORE_CLOSE_QUOTE_RE.sub('»', text)

        # 7. Остаточные незакрытые PH-маркеры (аварийная очистка)
        text = cls._LEFTOVER_PH_RE.sub('', text)

        # 8. trim пробелов (\\n в HoI4 значимы — не трогаем)
        text = text.strip(' ')

        return text


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
        return PLACEHOLDER_RE.findall(text)

    @classmethod
    def validate(cls, original: str, translated: str) -> List[str]:
        """
        Возвращает список строк с описанием проблем.
        Пустой список = перевод валиден.
        """
        issues = []

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
        for ph, count in trans_counts.items():
            if ph not in orig_counts:
                issues.append(f"лишний: {ph!r}")

        return issues

    @classmethod
    def try_fix(cls, original: str, translated: str) -> str:
        """
        Пытается автоматически восстановить потерянные плейсхолдеры.
        Стратегия: дописываем потерянные в конец (лучше чем потерять совсем).
        """
        orig_phs = cls.extract_placeholders(original)
        trans_phs = cls.extract_placeholders(translated)

        orig_counts: Dict[str, int] = {}
        for ph in orig_phs:
            orig_counts[ph] = orig_counts.get(ph, 0) + 1

        trans_counts: Dict[str, int] = {}
        for ph in trans_phs:
            trans_counts[ph] = trans_counts.get(ph, 0) + 1

        missing = []
        for ph, count in orig_counts.items():
            deficit = count - trans_counts.get(ph, 0)
            missing.extend([ph] * deficit)

        if missing:
            translated = translated.rstrip() + ' ' + ' '.join(missing)

        return translated


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

class ValidationReport:
    """Собирает статистику валидации за сессию."""
    def __init__(self):
        self.total = 0
        self.valid = 0
        self.fixed = 0
        self.broken: List[Dict[str, Any]] = []  # {file, key, issues}

    def record(self, file: str, key: str, issues: List[str], auto_fixed: bool):
        self.total += 1
        if not issues:
            self.valid += 1
        elif auto_fixed:
            self.fixed += 1
        else:
            self.broken.append({'file': file, 'key': key, 'issues': issues})

    def summary(self) -> str:
        bad = len(self.broken)
        return (f"Валидация: {self.valid}/{self.total} OK | "
                f"авто-фикс: {self.fixed} | битых: {bad}")

# ---------------------------------------------------------------------------
# JAVA ИНТЕГРАЦИЯ
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
        else:
            logger.warning("JPype not installed. Java features disabled.")

    def translate_batch_java(self, texts: List[str], target_lang: str = "ru") -> List[str]:
        if not self.java_available:
            raise NotImplementedError("Java runtime not available.")
        logger.info(f"Calling Java service for {len(texts)} items...")
        time.sleep(0.1)
        return [f"[JAVA_TRANSLATED]{t}" for t in texts]

    def shutdown(self):
        # FIX: проверяем java_available ДО обращения к jpype
        if self.java_available and JAVA_OK:
            try:
                if jpype.isJVMStarted():
                    jpype.shutdownJVM()
                    logger.info("JVM shut down.")
            except Exception as e:
                logger.warning(f"JVM shutdown error: {e}")

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
            self._conn.commit()

    def _get_hash(self, text: str, src: str, tgt: str, engine: str) -> str:
        key_str = f"{text}|{src}|{tgt}|{engine}"
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()

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

    def get_stats(self) -> Dict[str, int]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute('SELECT COUNT(*) FROM cache')
            total = cur.fetchone()[0]
            cur.execute('SELECT COUNT(DISTINCT engine_name) FROM cache')
            engines = cur.fetchone()[0]
        return {'total': total, 'engines': engines}

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
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = TranslationCache(DB_FILE)
        self.java_service = JavaTranslatorService()
        self._semaphore: Optional[asyncio.Semaphore] = None
        # Статистика валидации за сессию
        self.validation_report = ValidationReport()

    async def __aenter__(self):
        if ASYNC_OK:
            connector = aiohttp.TCPConnector(limit=SEMAPHORE_LIMIT)
            self.session = aiohttp.ClientSession(connector=connector)
        self._semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
        self.java_service.shutdown()
        self.cache.close()

    async def translate_text_async(self, text: str, target_lang: str = "ru", retries: int = 3,
                                    _file_ctx: str = "", _key_ctx: str = "") -> str:
        """
        Переводит одну строку с постобработкой, валидацией и фолбэком.
        _file_ctx / _key_ctx используются только для отчёта валидации.
        """
        if not text or not text.strip():
            return text

        # Проверка кэша
        cached = self.cache.get(text, self.source_lang, target_lang, self.engine)
        if cached:
            logger.debug(f"Cache hit: {text[:30]}...")
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
                        logger.info(f"Фолбэк сработал: {self.engine} → {used_engine}")
                    break
                except Exception as e:
                    logger.warning(f"Движок {eng} упал: {e}. Пробуем следующий...")
                    if eng == engines_to_try[-1]:
                        # Все движки упали — возвращаем оригинал
                        logger.error(f"Все движки недоступны для: '{text[:40]}'")
                        return text

        # Восстанавливаем плейсхолдеры
        final_text = restore_placeholders(result_text, tokens)

        # --- 6. ВАЛИДАЦИЯ ---
        issues = PlaceholderValidator.validate(text, final_text)
        auto_fixed = False
        if issues:
            # Пробуем авто-фикс
            fixed = PlaceholderValidator.try_fix(text, final_text)
            fixed_issues = PlaceholderValidator.validate(text, fixed)
            if not fixed_issues:
                final_text = fixed
                auto_fixed = True
                logger.info(f"Авто-фикс плейсхолдеров: {issues}")
            else:
                logger.warning(f"Битый перевод [{_key_ctx}]: {issues}")

        self.validation_report.record(_file_ctx, _key_ctx, issues, auto_fixed)

        # --- 5. ПОСТОБРАБОТКА ---
        final_text = PostProcessor.process(final_text, text)

        if final_text and final_text != text:
            self.cache.set(text, final_text, self.source_lang, target_lang, used_engine)

        return final_text

    async def _call_engine(self, engine: str, protected: str, target: str, retries: int) -> str:
        """Вызов конкретного движка по имени."""
        if engine == "Google" and ASYNC_OK and self.session:
            return await self._google_async(protected, target, retries)
        elif engine == "DeepL" and self.api_key:
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor(max_workers=1) as pool:
                return await loop.run_in_executor(
                    pool, lambda: self._deepl_sync(protected, target, retries)
                )
        elif engine == "JavaSim":
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor(max_workers=1) as pool:
                res = await loop.run_in_executor(
                    pool, lambda: self.java_service.translate_batch_java([protected], target)
                )
                return res[0]
        else:
            # MyMemory или Google без aiohttp
            loop = asyncio.get_event_loop()
            eng_copy = engine  # замыкание
            with ThreadPoolExecutor(max_workers=1) as pool:
                return await loop.run_in_executor(
                    pool, lambda: self._sync_fallback(protected, target, retries, eng_copy)
                )

    async def _google_async(self, text: str, target: str, retries: int) -> str:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            'client': 'gtx', 'sl': self.source_lang, 'tl': target, 'dt': 't', 'q': text
        }
        for attempt in range(retries):
            try:
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return ''.join([s[0] for s in data[0] if s and s[0]])
                    elif resp.status == 429:
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
            except Exception as e:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
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
                    raise
        return text

    def _sync_fallback(self, text: str, target: str, retries: int, engine: Optional[str] = None) -> str:
        eng = engine or self.engine
        if eng == "MyMemory":
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

    async def translate_batch(self, texts: List[str], target_lang: str = "ru",
                               keys: Optional[List[str]] = None,
                               file_ctx: str = "") -> List[str]:
        tasks = [
            self.translate_text_async(
                t, target_lang,
                _file_ctx=file_ctx,
                _key_ctx=(keys[i] if keys else str(i))
            )
            for i, t in enumerate(texts)
        ]
        return await asyncio.gather(*tasks)

# ---------------------------------------------------------------------------
# ОБРАБОТКА ФАЙЛОВ
# ---------------------------------------------------------------------------

def has_cyrillic(text: str) -> bool:
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

def process_file_sync(
    src_path: str,
    dst_path: str,
    translator: AsyncTranslator,
    log_cb,
    skip_translated: bool,
    stop_event: threading.Event,
    loop: asyncio.AbstractEventLoop
) -> Optional[int]:

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    existing = load_existing_translations(dst_path) if skip_translated else {}

    if existing:
        log_cb(f"  Найдено переведённых строк в файле: {len(existing)}", 'info')

    with open(src_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    out_lines = list(lines)
    to_translate_idx = []
    to_translate_val = []
    to_translate_keys = []
    meta = []
    skipped = 0

    for i, line in enumerate(lines):
        if stop_event.is_set():
            return None

        if i == 0 and line.strip().startswith('l_'):
            out_lines[i] = re.sub(r'^(\s*)l_[a-z]+:', r'\1l_russian:', line)
            continue

        m = VALUE_RE.match(line)
        if not m:
            continue

        prefix, value, suffix = m.group(1), m.group(2), m.group(3)
        key = prefix.strip()

        if skip_translated and key in existing:
            out_lines[i] = f'{prefix}"{existing[key]}"{suffix}\n'
            skipped += 1
        else:
            to_translate_idx.append(i)
            to_translate_val.append(value)
            to_translate_keys.append(key)
            meta.append((prefix, suffix))

    total = len(to_translate_val)
    if total == 0:
        if skipped:
            log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')
        return 0

    rel_path = os.path.basename(src_path)

    async def run_translate():
        async with translator:
            return await translator.translate_batch(
                to_translate_val, "ru",
                keys=to_translate_keys,
                file_ctx=rel_path
            )

    try:
        future = asyncio.run_coroutine_threadsafe(run_translate(), loop)
        translated_vals = future.result(timeout=300)
    except Exception as e:
        log_cb(f"  Ошибка перевода: {e}", 'error')
        return None

    for line_idx, translated, (pfx, sfx) in zip(to_translate_idx, translated_vals, meta):
        out_lines[line_idx] = f'{pfx}"{translated}"{sfx}\n'

    with open(dst_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(out_lines)

    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')

    # Логируем проблемы валидации для этого файла
    broken_in_file = [b for b in translator.validation_report.broken if b['file'] == rel_path]
    if broken_in_file:
        log_cb(f"  ⚠ Битых строк: {len(broken_in_file)}", 'warning')
        for b in broken_in_file[:5]:  # Показываем не больше 5
            log_cb(f"    [{b['key']}]: {', '.join(b['issues'])}", 'warning')

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

def load_settings() -> dict:
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
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HOI4 Localisation Translator PRO v2.2")
        self.minsize(820, 720)
        self.configure(bg='#1a1a1a')

        self._stop_event = threading.Event()

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

        # Параллельность
        ttk.Label(row1, text="Параллельность:").pack(side='left', padx=(15, 0))
        self.semaphore_var = tk.IntVar(value=SEMAPHORE_LIMIT)
        self.sem_lbl = ttk.Label(row1, text=str(SEMAPHORE_LIMIT), width=3)
        self.sem_lbl.pack(side='right')
        ttk.Scale(
            row1, from_=1, to=50, variable=self.semaphore_var,
            command=lambda v: self.sem_lbl.config(text=str(int(float(v))))
        ).pack(side='right', padx=5)

        row2 = ttk.Frame(f_engine); row2.pack(fill='x', pady=(10, 0))
        self.api_lbl = ttk.Label(row2, text="DeepL API Key:", foreground='#555')
        self.api_lbl.pack(side='left')
        self.api_key_var = tk.StringVar()
        self.api_entry = ttk.Entry(row2, textvariable=self.api_key_var, show="*", width=40, state='disabled')
        self.api_entry.pack(side='left', padx=5)
        self.verify_btn = ttk.Button(row2, text="Проверить API", command=self.verify_deepl_api, state='disabled', width=12)
        self.verify_btn.pack(side='left', padx=5)
        self.api_status_var = tk.StringVar(value="")
        self.api_status_lbl = ttk.Label(row2, textvariable=self.api_status_var, foreground='#aaa', width=15)
        self.api_status_lbl.pack(side='left', padx=5)

        # Опции
        of = ttk.Frame(self); of.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Пропускать переведённое (файл + БД)", variable=self.skip_var).pack(side='left')
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Открыть папку по итогу", variable=self.open_var).pack(side='left', padx=20)

        # Кэш инфо
        self.cache_info_var = tk.StringVar(value="")
        ttk.Label(of, textvariable=self.cache_info_var, foreground='#74c0fc').pack(side='right')
        ttk.Button(of, text="Инфо кэша", command=self._show_cache_info, width=10).pack(side='right', padx=5)

        # Прогресс
        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=15, pady=(10, 2))

        f_status = ttk.Frame(self); f_status.pack(fill='x', padx=15)
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
        fb = ttk.Frame(self); fb.pack(fill='x', padx=15, pady=(0, 10))
        self.start_btn = ttk.Button(fb, text="▶ НАЧАТЬ ПЕРЕВОД", command=self.start)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 4), ipady=5)
        self.stop_btn = ttk.Button(fb, text="■ СТОП", state='disabled', command=self.stop)
        self.stop_btn.pack(side='left', fill='x', expand=True, padx=4, ipady=5)
        ttk.Button(fb, text="Очистить лог", command=self.clear_log).pack(side='left', fill='x', expand=True, padx=(4, 0), ipady=5)

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

    def _show_cache_info(self):
        try:
            cache = TranslationCache(DB_FILE)
            stats = cache.get_stats()
            cache.close()
            self.cache_info_var.set(f"БД: {stats['total']} записей")
            self.log_line(f"Кэш: {stats['total']} переводов, {stats['engines']} движков", 'info')
        except Exception as e:
            self.log_line(f"Ошибка чтения кэша: {e}", 'error')

    def verify_deepl_api(self):
        api_key = self.api_key_var.get().strip()
        if not api_key:
            self.api_status_var.set("Нет ключа")
            self.api_status_lbl.config(foreground='#ff6b6b')
            return

        self.api_status_var.set("Проверка...")
        self.api_status_lbl.config(foreground='#ffa500')
        self.verify_btn.config(state='disabled')

        def check():
            try:
                from deep_translator import DeepL
                translator = DeepL(api_key=api_key, source="en", target="ru", use_pro=False)
                test_result = translator.translate("test")
                if test_result:
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

        threading.Thread(target=lambda: on_done(*check()), daemon=True).start()

    def browse_src(self):
        d = filedialog.askdirectory(title="Выбери папку с оригинальной локализацией")
        if d:
            self.src_var.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(title="Выбери папку для перевода")
        if d:
            self.dst_var.set(d)

    def log_line(self, text: str, tag: Optional[str] = None):
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
        if s.get('semaphore'): self.semaphore_var.set(s['semaphore'])
        if s.get('skip') is not None: self.skip_var.set(s['skip'])
        if s.get('open') is not None: self.open_var.set(s['open'])
        self._toggle_api_field()

    def _on_close(self):
        save_settings({
            'src': self.src_var.get(),
            'dst': self.dst_var.get(),
            'api_key': self.api_key_var.get(),
            'engine': self.engine_var.get(),
            'lang': self.lang_var.get(),
            'semaphore': int(self.semaphore_var.get()),
            'skip': self.skip_var.get(),
            'open': self.open_var.get(),
        })
        # FIX: корректная остановка event loop при закрытии
        self._async_loop.call_soon_threadsafe(self._async_loop.stop)
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
            'semaphore': int(self.semaphore_var.get()),
            'skip': self.skip_var.get(), 'open': self.open_var.get(),
        })

        threading.Thread(target=self.run_translation, daemon=True).start()

    def run_translation(self):
        engine_name = self.engine_var.get()
        source_ln = self.lang_var.get()
        key = self.api_key_var.get()
        src = self.src_var.get()
        dst = self.dst_var.get()

        # Обновляем глобальный лимит семафора из UI
        global SEMAPHORE_LIMIT
        SEMAPHORE_LIMIT = int(self.semaphore_var.get())

        start_t = time.time()
        total_lines = 0
        errors = 0

        self.log_line(f"Запуск... Движок: {engine_name}, Параллельность: {SEMAPHORE_LIMIT}", 'info')

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
                self.status_var.set(f"Файл {i+1}/{total}: {os.path.basename(fp)}")
                self.log_line(f"[{i+1}/{total}] {rel}")

                dst_path = rename_dst(fp, src, dst)
                try:
                    # FIX: передаём общий loop вместо создания нового
                    n = process_file_sync(
                        fp, dst_path, translator,
                        self.log_line, self.skip_var.get(),
                        self._stop_event, self._async_loop
                    )
                    if n is None:
                        break
                    total_lines += n

                    elapsed = time.time() - start_t
                    if elapsed > 0 and total_lines > 0:
                        speed = total_lines / elapsed
                        self.speed_var.set(f"{speed:.1f} строк/сек")

                    self.log_line(f"  ✓ строк: {n}", 'success')
                except Exception as e:
                    self.log_line(f"  ОШИБКА: {e}", 'error')
                    logger.exception(f"File error: {fp}")
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
            self.log_line("=" * 50, 'info')
            self.log_line(f"  {summary}", 'success' if not errors else 'warning')

            # Итог валидации
            vr = translator.validation_report
            self.log_line(f"  {vr.summary()}", 'info' if not vr.broken else 'warning')
            if vr.broken:
                self.log_line(f"  Битые строки записаны в лог-файл: {LOG_FILE}", 'warning')
                for b in vr.broken:
                    logger.warning(f"BROKEN [{b['file']}] {b['key']}: {b['issues']}")

            self.log_line("=" * 50, 'info')

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

# ---------------------------------------------------------------------------
# ТОЧКА ВХОДА
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if not TRANSLATOR_OK:
        print("WARNING: pip install deep-translator")
    if not ASYNC_OK:
        print("INFO: pip install aiohttp  (для ускорения)")
    App().mainloop()
