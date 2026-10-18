"""The translation engine: providers, cache, validation and the fallback chain."""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Dict, List, Optional

from .config import (
    ASYNC_OK,
    DB_FILE,
    DEEPL_OK,
    SEMAPHORE_LIMIT,
    GoogleTranslator,
    MyMemoryTranslator,
    aiohttp,
    deepl,
    logger,
)
from .errors import (
    TranslationCancelled,
    TranslationFailed,
    TranslationValidationFailed,
)
from .java_service import JavaTranslatorService
from .providers import (
    FALLBACK_CHAIN,
    GOOGLE_ENDPOINT_ENGINE,
    deepl_language_code,
    provider_language_code,
)
from .storage import TranslationCache
from .text import (
    PostProcessor,
    protect_placeholders,
    restore_placeholders,
    split_long_text,
)
from .validation import ValidationReport, validate_line


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

        Фолбэк срабатывает не только на ошибке движка, но и на ответе, который
        нельзя записать в игру (потерян маркер, нарушен формат): такой ответ
        считается неудачной попыткой, и строка уходит к следующему движку.
        _file_ctx / _key_ctx используются только для отчёта валидации.
        """
        if not text or not text.strip():
            return text

        # By default only the selected engine may answer from the cache; see
        # _cache_engines() for why cross-engine reuse is opt-in.
        for cached_engine in self._cache_engines():
            cached = self.cache.get(text, self.source_lang, target_lang, cached_engine)
            if cached is not None:
                cached_issues = validate_line(text, cached)
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

        # --- 7. ФОЛБЭК-ЦЕПОЧКА (по одной строке) ---
        engines_to_try = [self.engine] + FALLBACK_CHAIN.get(self.engine, [])
        attempts: List[str] = []
        issues: List[str] = []
        answered = False
        last_error: Optional[Exception] = None

        async with self._semaphore:
            for eng in engines_to_try:
                try:
                    raw_text = await self._call_engine(eng, protected, target_lang, retries)
                except Exception as e:
                    attempts.append(f"{eng}: {e}")
                    last_error = e
                    logger.warning(f"Движок {eng} упал: {e}. Пробуем следующий...")
                    continue

                answered = True

                # Restore the protected markers and judge the answer we got: a
                # lost marker or a broken one-line format is a failed attempt,
                # not a translation we are allowed to hand to the pipeline.
                candidate = restore_placeholders(raw_text, tokens)
                issues = validate_line(text, candidate)
                if not issues:
                    candidate = PostProcessor.process(candidate, target_lang)
                    issues = validate_line(text, candidate)
                    if issues:
                        # Post-processing must not silently damage a token that
                        # was valid immediately after the translation itself.
                        attempts.append(
                            f"{eng}: постобработка нарушила формат ({'; '.join(issues)})"
                        )
                else:
                    attempts.append(f"{eng}: {'; '.join(issues)}")

                if issues:
                    logger.warning(
                        f"Движок {eng} вернул битый перевод [{_key_ctx}]: {'; '.join(issues)}"
                    )
                    continue

                if eng != self.engine:
                    # A paid engine silently degrading to a free one must be
                    # visible in the UI, not only in the log file.
                    logger.info(f"Фолбэк сработал: {self.engine} → {eng}")
                    self._note_fallback(eng)

                self.validation_report.record(_file_ctx, _key_ctx, [], auto_fixed=False)
                if candidate and candidate != text:
                    # Only a value that passed validation may be reused later.
                    self.cache.set(text, candidate, self.source_lang, target_lang, eng)
                return candidate

        if not answered:
            # Never write untranslated source text as if translation succeeded;
            # fail the file so the UI reports the outage.
            logger.error("Все движки недоступны для ключа %s", _key_ctx or "?")
            raise TranslationFailed(
                "Все доступные движки перевода завершились ошибкой"
            ) from last_error

        # Every engine answered, but none of the answers can be written.
        self.validation_report.record(
            _file_ctx, _key_ctx, issues or ["перевод не прошёл проверку"], auto_fixed=False
        )
        raise TranslationValidationFailed(
            f"Ни один движок не вернул корректный перевод [{_key_ctx or '?'}]: "
            + "; ".join(attempts),
            source=text, candidate=candidate, issues=issues, attempts=attempts,
        )

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
        return deepl_language_code(language, target=target)

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
            key = keys[i] if keys else str(i)
            try:
                result = await self.translate_text_async(
                    text, target_lang, _file_ctx=file_ctx, _key_ctx=key
                )
            except TranslationValidationFailed as error:
                # One unusable line must not cost the rest of the file. The row
                # is already in the validation report, and the value below is
                # handed to the pipeline unvalidated on purpose: it validates
                # every row itself and decides between refusing the file and
                # keeping the English text for this line. Nothing was cached.
                logger.warning("Строка [%s] не переведена: %s", key, error)
                result = error.candidate or text
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
