# ПРОМПТ ДЛЯ ИСПРАВЛЕНИЯ АУДИТА Hoi4_translator v1.4.3

Скопируй целиком в ChatGPT / Claude / Jules / Codex — он исправит все 25 пунктов аудита.

---

## ПРОМПТ (копировать с этой строки)

Ты — senior Python разработчик. Твоя задача — исправить все слабые места проекта **Hoi4_translator**, найденные в ходе двух аудитов.

### Контекст проекта
- Репозиторий: `xsitraxs/Hoi4_translator`, ветка `main`, Python 3.9+, `tkinter`+`CustomTkinter` GUI, `aiohttp`+`deep_translator`+`deepl`+`JPype` движки.
- Назначение: пакетный перевод `.yml` локализации Hearts of Iron IV, сохранение плейсхолдеров `$VAR$`, `§R…§!`, `£icon`, `[GetTag]`, `\\n`.
- Главный файл: `hoi4_translator.py` → re-экспорт из пакета `hoi4translator/`
- Модули: `config`, `localisation`, `text`, `validation`, `providers`, `java_service`, `storage`, `translation`, `glossary`, `vanilla`, `pipeline`, `inspect`, `cli`, `report`, `settings`, `gui/app`, `gui/preview`, `gui/theme`, `java/HoI4Translator.java`
- Тесты: 177 тестов в `tests/`, `python -m unittest discover -s tests -v` должен остаться OK (1 skipped), `python -m compileall -q hoi4translator` должен проходить.
- Документация Paradox (норма): https://hoi4.paradoxwikis.com/Localisation и https://hoi4.paradoxwikis.com/Modding — файл обязан быть UTF-8 BOM, имя `*_l_<lang>.yml`, первая строка `l_english:`, ключ `KEY:0 "Value"` в одну строку в `"` кавычках, цвета `§X…§!` (валидные: `C L W B G R b g Y H T O 0-9 t !` + `§[expr]` + `§§` literal), иконки `£`, переменные `$`, неймспейсы `[]`.

### Что уже есть
В корне лежат два отчёта:
- `AUDIT_REPORT.md` — 22 внутренние находки (архитектура, потоки, безопасность, CI)
- `AUDIT_REPORT_PARADOX.md` — 6 дополнительных сверок с документацией Paradox (whitelist цветов, кросс-файловые дубликаты, `replace/` приоритет)

Прочитай ОБА файла полностью перед правками.

### Задачи (приоритет по фазам)

#### ФАЗА 1 — Безопасность данных (сделать ОБЯЗАТЕЛЬНО, 1-2 дня)

1. **Мёртвый код `localisation.py:115`**: после `return None` в `split_value_line` лежит недостижимая вторая реализация (scan слева-направо `while position < length`). Удали 10 строк мёртвого кода, оставь только рабочую реализацию справа-налево. Проверь `ruff`.

2. **Рассинхрон парсеров (КРИТИЧНО)**: `load_existing_translations` (localisation.py:273) использует старый `VALUE_RE.fullmatch`, а `pipeline.py`/`inspect.py` — `split_value_line`. На raw-кавычках `§L"Shoot..."§!` старый RE ломается → `skip_translated=True` считает файл пустым → повторный перевод. Унифицируй: `load_existing_translations` должен использовать `split_value_line` (как везде). Добавь тест: файл записанный `build_output_lines` читается `load_existing_translations` без потерь.

3. **SQLite WAL/timeout (`storage.py`)**: сейчас `sqlite3.connect(db_path, check_same_thread=False)` без WAL и timeout. Сделай:
   ```python
   self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0, isolation_level=None)
   self._conn.execute("PRAGMA journal_mode=WAL")
   self._conn.execute("PRAGMA synchronous=NORMAL")
   ```
   Убедись что `close()` не гоняется с `get()` — добавь refcount или перенеси `close` в shutdown после завершения всех `process_file_sync`.

4. **CLI двойной `load_glossary` (`cli.py:225,232`)**: при `--dry-run` глоссарий грузится дважды. Удали второй вызов, оставь первый до `if args.dry_run`.

5. **Цвета whitelist (№23, Paradox)**: `validation.py` `PLACEHOLDER_RE`/`_COLOR_CODE_CHAR_RE` сейчас `[A-Za-z0-9!]` (любая буква = валидно). По спеке Paradox валидны только `C L W B G R b g Y H T O 0 1 2 3 4 5 6 7 8 9 t !` (+ `§[expr]` + `§§`). Замени на whitelist:
   ```python
   VALID_COLORS = set("CLWBG RbgYHTO0123456789t!".replace(" ",""))
   ```
   и проверяй `_invalid_sections` против него. Добавь тесты: `§M` должен быть invalid, `§R`/`§0`/`§t`/`§!` — valid.

6. **Кросс-файловые дубликаты (№25, Paradox)**: сейчас `pipeline.py`/`inspect.py` ловят дубликаты только внутри одного файла (`key_line_numbers`). Добавь `validate_cross_file_duplicates(all_files: list[str])` — собрать `normalize_key` по всем файлам, вернуть warning в `RunReport` если один ключ в разных файлах (учесть `replace/` приоритет). Вызывай в `cli.py` и `gui/app.py` перед стартом перевода.

#### ФАЗА 2 — Надёжность сети (2-3 дня)

7. **Google rate-limit thundering herd (`translation.py:263`, `java/HoI4Translator.java`)**: 15 параллельных запросов без jitter → 429 одновременно → ретрай одновременно. Добавь:
   - jitter: `wait * (0.5 + random.random())`
   - глобальный token bucket (5 rps) для `https://translate.googleapis.com`
   - circuit breaker на 429 (если 3×429 подряд — пауза 10с)
   - ретраи в Java-клиенте (сейчас их нет)

8. **Семантика параллелизма (`translation.py:365`)**: `batch_size = concurrency` и `semaphore = concurrency` → семафор не ограничивает (15 задач уже запущены). Исправь: убери батчинг ИЛИ сделай `semaphore = concurrency` а `batch_size = concurrency*2` для pipeline. Убери busy-poll `await asyncio.wait(timeout=0.1)` — замени на `FIRST_COMPLETED` без таймаута + отдельный `wait` на `stop_event`.

#### ФАЗА 3 — UX/Архитектура (неделя)

9. **God Object `App` (1305 строк)**: разбей `gui/app.py` на `TranslationController` (бизнес-логика), `SettingsStore`, `UiLogger`; `App` только строит виджеты. Убери чтение `tk.StringVar` из worker-потока — snapshot в `start()` уже есть, но `config['glossary']` читается повторно. Добавь передачу через `config` dict.

10. **Preview truncation (`gui/preview.py:18`)**: `MAX_PREVIEW_ROWS=1000` режет молча. Добавь красный баннер поверх таблицы: "Показаны первые 1000 из 4700 — остальные будут записаны без просмотра" (крупно, не мелкий `count_text`).

11. **Отчёт имя (`report.py:129`)**: `hoi4_translator_report_%Y%m%d-%H%M%S` — коллизия в одну секунду. Добавь микросекунды или `uuid4()[:6]`: `f"hoi4_translator_report_{stamp}_{uuid4().hex[:6]}"`.

#### ФАЗА 4 — Гигиена

12. **Path traversal (`localisation.py:328`)**: `collect_yml_files` идёт по `os.walk` без `realpath`, `rename_dst` делает `relpath` без санитарии. Добавь `os.path.realpath` + проверку `../` в `rel_renamed`, игнорируй symlink на `/etc`.

13. **`latin-1` fallback**: любой `.png` переименованный в `.yml` читается как latin-1 и уходит в Google. Добавь эвристику: если первые 4KB не содержат `:` и печатных символов — отбой.

14. **Два event loop (`gui/app.py:104` vs `cli.py:257`)**: GUI держит глобальный loop, CLI — `asyncio.run()`. Если GUI импортируется в CLI, `get_running_loop()` вернёт не тот. Передай в `pipeline.process_file_sync` не `loop` а `runner: Callable[[Coroutine], Future]`.

15. **JVM смена JAR (`java_service.py:169`)**: JPype не перезапускается, UI не объясняет. Добавь в GUI явное сообщение: "JVM уже запущена с {old}. Новый JAR вступит после перезапуска приложения" — сейчас только лог.

16. **Глоссарий границы (`glossary.py:47`)**: `(?<![\w])(?:terms)(?![\w])` ломается на `co-op`, `Sinn Féin`. Замени на границы с учётом дефиса/апострофа: `(?<![\w'-])` / `(?![\w'-])` и добавь тест. Sniffing по первой строке — расширь до первых 5 строк.

17. **CI (`build.yml`)**: подними `fail-under` с 40 до 60 + `branch=True`, запинь `ruff==0.17.0` в `ruff.toml`, добавь smoke `pyinstaller --help` в PR.

18. **Логи (`config.py:108`)**: `StreamHandler` в windowed EXE пишет в никуда. Замени на `NullHandler` если `sys.executable.endswith(".exe")` и нет консоли.

19. **Зависимости (`requirements.txt`)**: запинь версии через `pip-compile` (`aiohttp==3.9.*` и т.д.), вынеси `keyring` в `extras` для headless.

20. **Инспекция первая строка БД (№24)**: добавь в `inspect_file` warning если файл содержит ключи но не содержит `l_english:` → риск crash `0xC0000005`.

### Ограничения
- Не ломай публичный API (`hoi4translator/__init__.py` `__all__`), не переименовывай файлы, не трогай `.git`/`.gitignore` кроме `build.yml`/`requirements.txt`.
- Сохрани поведение: запись всегда `utf-8-sig` (с BOM), `l_russian:` хедер, `§[expr]` и `§§` как один токен, глоссарий longest-first.
- Каждый фикс покрывай тестом или расширяй существующий.
- `python -m unittest discover -s tests -v` → 177 OK (1 skipped), `python -m compileall -q` → без ошибок.
- Пиши комментарии на русском где логика Paradox-специфична, на английском — где общая.

### Критерии приёмки
- [ ] `load_existing_translations` читает raw-кавычки
- [ ] мёртвый код удалён
- [ ] `PRAGMA journal_mode=WAL` в `storage.py`
- [ ] `VALID_COLORS` whitelist, `§M` → invalid
- [ ] кросс-файловые дубликаты → warning
- [ ] jitter + rate limiter
- [ ] тесты зелёные, отчёты `AUDIT_REPORT*.md` обновлены статусом Done

### Формат ответа
1. Кратко перечисли что исправил по фазам
2. Покажи `git diff --stat`
3. Укажи что осталось TODO

--- КОНЕЦ ПРОМПТА ---

## Как использовать
- Вставь в любую LLM с доступом к коду (Cursor, Copilot, Jules)
- Или скажи мне "исправь по промпту" — я начну с Фазы 1 прямо в этой ветке
- Промпт покрывает 25 пунктов (22 внутренних + 3 из сверки с Paradox)

