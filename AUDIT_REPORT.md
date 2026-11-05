# 🔍 Аудит проекта Hoi4_translator v1.4.3

**Дата:** 2026-11-05  
**Ветка:** `arena/1486f252-hoi4-translator` (от `main` 6c76993)  
**Объём:** 15 модулей, ~4200 строк Python + 300 строк Java, 177 тестов  
**Метод:** статический анализ, review архитектуры, трассировка потоков данных, проверка сборки/CI, ручная симуляция краевых случаев.

> Краткий вывод: проект зрелый, основная логика (защита плейсхолдеров, валидация, атомарная запись, переиспользование ванили) сделана аккуратно. Критических дыр с потерей данных или RCE нет. Но есть 22 слабых места — от мёртвого кода и несогласованных парсеров до рисков блокировки IP и скрытых гонок в GUI/Async слое. Ниже — приоритизированный список.

---

## 1. Сводная таблица рисков

| № | Категория | Проблема | Тяжесть | Вероятность | Где |
|---|-----------|----------|---------|-------------|-----|
| 1 | **Корректность** | Мёртвый код в `split_value_line` — вторая реализация после `return None` никогда не исполняется | 🟡 Средняя | 100% (уже) | `localisation.py:115-135` |
| 2 | **Корректность** | Два парсера одного формата: `split_value_line` vs `VALUE_RE` в `load_existing_translations` — расходятся на raw-кавычках | 🔴 Высокая | Высокая | `localisation.py:71,273` vs `91` |
| 3 | **Безопасность** | `collect_yml_files` + `rename_dst` не санитизируют путь; `latin-1` как fallback читает любые бинарники как текст | 🟡 Средняя | Средняя | `localisation.py:328-335,23-36` |
| 4 | **Надёжность** | SQLite кэш: один `sqlite3.Connection` с `check_same_thread=False`, без WAL/timeout; `close()` из async-потока гоняется с `get` | 🔴 Высокая | Средняя | `storage.py:14-22,156` |
| 5 | **Секреты** | DeepL-ключ в `tk.StringVar` живёт в памяти как plain-text, логируется при `logger.exception`; keyring fallback только предупреждает | 🟡 Средняя | Средняя | `settings.py`, `gui/app.py:672` |
| 6 | **Конкурентность** | Семантика двойная: `batch_size == concurrency` и `semaphore == concurrency` → фактический параллелизм в 2× меньше заявленного; polling `stop_event` 100мс добавляет лаг стопа | 🟠 Средняя | Высокая | `translation.py:365-408` |
| 7 | **Конкурентность** | GUI держит один глобальный `asyncio` loop в фоновом потоке, CLI — `asyncio.run()`; второй запуск CLI в том же процессе упадёт | 🟡 Средняя | Низкая | `gui/app.py:104, cli.py:257` |
| 8 | **JVM** | JPype JVM не перезапускается — смена JAR в настройках требует перезапуска приложения, но UI это не объясняет (только лог) | 🟡 Средняя | Высокая | `java_service.py:169-215` |
| 9 | **Глоссарий** | `pattern = (?<![\w])(?:terms)(?![\w])` ломается на терминах с дефисом/апострофом/не-ASCII (`Sinn Féin`, `co-op`) + sniffing по первой строке | 🟡 Средняя | Средняя | `glossary.py:47,92` |
| 10 | **Ваниль-индекс** | Lookup по точному `value` без нормализации пробелов/регистра; короткие строки отфильтрованы порогом 3, но цифры типа `"100"` тоже могут совпасть | 🟢 Низкая | Средняя | `vanilla.py:72-118` |
| 11 | **Валидация** | `§§§` и `§[expr` без `]` трактуются как ошибка, хотя в модах это встречается; `_invalid_sections` помечает одиночный `§` в конце строки всегда как invalid | 🟢 Низкая | Низкая | `validation.py:31-48` |
| 12 | **Типографика** | `PostProcessor` использует префикс `\uE000HOI4PP_` для защиты маркеров — коллизия теоретически возможна; французский NBSP вставляется даже внутри `£icon` | 🟢 Низкая | Крайне низкая | `text.py:45-69` |
| 13 | **GUI — God Object** | `App` = 1305 строк: UI, бизнес-логика, threading, настройки, отчёты — невозможно тестировать изоляционно; `load_settings` дергает `messagebox` | 🟠 Высокая | Постоянно | `gui/app.py` |
| 14 | **GUI — Preview** | `MAX_PREVIEW_ROWS=1000` обрезает молча; пользователь думает что правит весь файл | 🟡 Средняя | Высокая на больших модах | `gui/preview.py:18,64` |
| 15 | **Файлы — отчёт** | Имя отчёта `hoi4_translator_report_%Y%m%d-%H%M%S` — коллизия при двух прогонах в одну секунду; нет проверки свободного места | 🟢 Низкая | Низкая | `report.py:129-147` |
| 16 | **CLI — двойной `load_glossary`** | При `--dry-run` глоссарий грузится дважды (строки 225 и 232) — лишний I/O и двойное сообщение | 🟢 Низкая | 100% при dry-run | `cli.py:225-233` |
| 17 | **Сборка/CI** | `coverage --fail-under=40` — порог слишком низкий, маскирует падение покрытия; `ruff==0.17.0` несовместим с `ruff.toml` без версии | 🟡 Средняя | Постоянно | `.github/workflows/build.yml:58` |
| 18 | **Логирование** | `StreamHandler()` пишет в stderr даже в windowed EXE где консоли нет; `RotatingFileHandler` 1МБ×3 может расти без очистки годами | 🟢 Низкая | Постоянно | `config.py:108-119` |
| 19 | **Внешние API** | Google `translate.googleapis.com` без ключа: 429 бьёт по всем параллельным запросам, backoff без jitter усиливает thundering herd | 🔴 Высокая | Высокая при больших модах | `translation.py:263-295, java/HoI4Translator.java` |
| 20 | **Атомарность** | `_atomic_write` копирует `chmod` но не `chown`; `_fsync_directory` пропускается на Windows — при сбое питания файл может пропасть | 🟢 Низкая | Крайне низкая | `config.py:128-176` |
| 21 | **UX — ошибки** | `validate_directory_relationships` запрещает вложенные папки, но сообщение не предлагает валидный пример; `browse_*` не проверяет что путь содержит `localisation` | 🟢 Низкая | Средняя | `localisation.py:308, gui/app.py:723` |
| 22 | **Зависимости** | Нет `pip-compile`/`poetry lock`; `requirements.txt` без пинов (`>=`) → невоспроизводимая сборка; `keyring` тянет `SecretService` D-Bus который падает в headless CI | 🟡 Средняя | Средняя | `requirements.txt` |

---

## 2. Критичные и высокоприоритетные (разбор)

### 🔴 №2 — Рассинхрон парсеров (главный риск потери данных)

**Суть:** `pipeline.py` и `inspect.py` используют `split_value_line()` — умный сканер справа-налево, который корректно держит raw-кавычки `§L"Shoot them again..."§!`. А `load_existing_translations()` в `localisation.py:273` всё ещё использует старый `VALUE_RE` (захват до первой кавычки). Итог: файл, ранее успешно переведённый и записанный `build_output_lines`, при следующем запуске с `skip_translated=True` не парсится и считается пустым — все строки переводятся заново, тратится квота, глоссарий применяется повторно.

```python
# localisation.py:271 — сейчас
match = VALUE_RE.fullmatch(content)  # ломается на raw-кавычках
# нужно: split_value_line(content) как везде
```

**Фикс:** унифицировать — везде `split_value_line`; добавить тест что `load_existing_translations` читает файл записанный `build_output_lines`.

### 🔴 №4 — SQLite без WAL

**Суть:** `storage.py` держит одно соединение на весь процесс. При `check_same_thread=False` SQLite требует внешней синхронизации — она есть (`_lock`), но:

* нет `PRAGMA journal_mode=WAL` → при падении посреди `commit` БД может остаться в `DELETE` режиме с потерянным журналом;
* нет `timeout` → при `database is locked` (например, второй экземпляр EXE) будет исключение вместо ожидания;
* `close()` вызывается из `translator.__aexit__` который бежит на async-loop потоке, тогда как `get()` может бежать на worker-потоке — закрытие посреди транзакции.

**Рекомендация:**

```python
self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0, isolation_level=None)
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA synchronous=NORMAL")
```

И не закрывать соединение в `__aexit__` если есть активные `process_file_sync` — refcount.

### 🔴 №19 — Google rate-limit + thundering herd

**Суть:** все три движка (Python async, Python sync, Java) бьют в один и тот же неофициальный endpoint `translate.googleapis.com` без API-ключа. При `concurrency=15` (дефолт) 15 параллельных запросов уходят одновременно, получают 429, и все 15 делают `sleep(2**attempt * 2)` — синхронно, без jitter, т.е. через 2с снова 15 одновременных ретраев.

* Java-версия вообще без ретраев — один 429 → весь батч падает → фолбэк на Google sync → снова 429 → файл не записан.
* `aiohttp` timeout 15с + 3 ретрая × 4с wait = до ~60с на строку; `file_translation_timeout` = 60+0.35×N → файл на 2000 строк получит 760с, но GUI не показывает ETA.

**Рекомендация:** jitter (`wait * (0.5+random())`), глобальный rate-limiter (token bucket 5 rps), circuit breaker на 429, отдельный backoff для Java.

### 🟠 №6 — Семантика параллелизма

**Текущий код (`translation.py:365`):**

```python
batch_size = max(1, self.concurrency)  # 15
# ...
pending = { create_task(...) for i in range(first, last) }  # 15 задач
# каждая задача внутри делает `async with self._semaphore` где semaphore=15
```

Семафор не ограничивает — 15 задач уже запущены. Ограничение сработает только если `concurrency < batch_size`, чего нет. Фактически код хотел «не более N одновременных HTTP», а получил «N батчей по N запросов последовательно». Исправление: убрать батчинг или сделать `semaphore = concurrency`, а `batch_size = concurrency * 2` для pipeline.

Плюс `await asyncio.wait(pending, timeout=0.1)` — busy-poll каждые 100мс, просыпает event-loop даже когда нечего делать. Лучше `FIRST_COMPLETED` без таймаута + отдельный `wait` на `stop_event`.

### 🟠 №13 — God Object `App`

1305 строк в одном классе нарушают SRP:

* построение UI (`_build_ui` 600 строк) + бизнес-логика (`run_translation` 300 строк) + настройки + логика отчётов — всё в одном файле;
* прямые вызовы `tk.StringVar` внутри worker-потока (`log_line` делает `_post_ui` но `config['glossary']` читается из Tk-переменной на worker-потоке — гонка);
* `protocol("WM_DELETE_WINDOW")` + `_close_when_finished` — state machine без тестов.

**Рекомендация:** выделить `TranslationController`, `SettingsStore`, `UiLogger`; `App` только собирает виджеты.

---

## 3. Средний приоритет

### №1 Мёртвый код

```python
# localisation.py:115 — после return None всё ниже недостижимо
    return None
    start = match.end()   # ← dead
    if content[start:start+1] != '"':
```

Оставлен от предыдущей реализации (scan слева-направо). Удаление — 10 строк, риск ноль. Ruff с `FURB` не ловит т.к. файл в ignore? Проверить `ruff.toml`.

### №3 Path traversal

`collect_yml_files` рекурсивно идёт по `os.walk(root)` — если мод содержит symlink на `/etc`, последует за ним. `rename_dst` делает `os.path.relpath(src_path, src_root)` — при symlink relpath может содержать `../`. Фикс: `os.path.realpath` уже есть в `validate_directory_relationships`, но не в `collect_yml_files`.

`latin-1` fallback: любой бинарный файл с расширением `.yml` (например, картинка ошибочно) будет прочитан как текст, раздут кэш, отправлен в Google. Фикс: проверять что первые 4 КБ содержат печатные символы + `:`.

### №7 Два event loop

GUI: `self._async_loop = asyncio.new_event_loop(); Thread(target=loop.run_forever)`.
CLI: `asyncio.run(_translate(...))` — создаёт новый loop на main thread. Если GUI импортируется в CLI (например, `from hoi4translator.gui.app import App` для проверки версии), `get_running_loop()` вернёт GUI loop и `run_coroutine_threadsafe` уйдёт не туда.

Фикс: в `pipeline.process_file_sync` принимать не `loop` а `runner: Callable[[Coroutine], Future]` — инверсия зависимости.

### №14 Preview truncation

```python
MAX_PREVIEW_ROWS = 1000
for number, (orig, trans) in enumerate(zip(originals[:1000], ...))
```

Файл на 4700 ключей (реальный `stellaris`-мод) — 3700 строк переводятся без показа. Пользователь нажимает «Сохранить» думая что просмотрел всё. Нужно хотя бы красный баннер «Показаны первые 1000 из 4700 — остальные будут записаны без просмотра» (сейчас есть `count_text`, но он в шапке мелким шрифтом).

### №17 CI

* `fail-under=40` — текущее покрытие ~45% (по `coverage run`), порог в 40% не ловит деградацию. Поднять до 60% и добавить `branch=True`.
* `ruff 0.17.0` — в `ruff.toml` используются правила `RET`, `PIE` переименованные в новых версиях; локальный `pip install ruff` без версии тихо игнорирует часть линт-правил.
* Windows build запускается только на тегах — PR с поломкой `pyinstaller` не ловится. Добавить `workflow_dispatch` уже есть, но нужен `pull_request` с `build --help` smoke test.

---

## 4. Низкий приоритет / гигиена

| № | Что улучшить | Как |
|---|--------------|-----|
| 10 | Vanilla threshold | `MIN_USEFUL_LENGTH=3` отфильтровывает `"OK"` — а это валидный ванильный перевод. Сделать allowlist коротких ключей. |
| 11 | `§` handling | Добавить тест на `§§§` → `§`+`§§`; `§[unclosed` → invalid, но сейчас `find("]")==-1` скипает. |
| 12 | PostProcessor markers | Вынести префикс в константу, добавить assert что он не встречается в исходнике (сейчас `while marker_prefix in text` — уже есть, ок). |
| 15 | Отчёт имя | Добавить микросекунды или `uuid4[:6]` к штампу. |
| 16 | CLI dry-run | Убрать второй `load_glossary` — оставить первый. |
| 18 | Логи | `StreamHandler` → `NullHandler` в windowed режиме (`sys.executable.endswith(".exe")`). |
| 20 | fsync | На Windows `os.replace` атомарен в NTFS, `_fsync_directory` не нужен — ок, комментарий добавить. |
| 21 | UX | В `browse_src/dst` добавить валидацию что путь содержит `.yml`. |
| 22 | Deps | Запинить версии в `requirements.txt` через `pip-compile`; добавить `keyring` в `extras` для headless. |

---

## 5. Что сделано хорошо (сохранить)

* **Защита плейсхолдеров до/после** — `protect_placeholders` маскирует и маркеры, и глоссарий одним механизмом, `restore` толерантен к вариациям `<< PH 0 >>` от движков. Отлично.
* **Валидация до записи** — `validate_pairs` + `split_row_issues` различают «битый исходник» vs «битый перевод» — файл не блокируется из-за ошибки автора мода.
* **Атомарная запись** — `_atomic_write` с `mkstemp` в той же директории + `fsync` + `replace` — защита от отключения питания/краша посреди записи.
* **Кэш с версионированием** — `CACHE_FORMAT_VERSION` в хеше инвалидирует старые записи при изменении постобработки.
* **Глоссарий longest-first** — сортировка по длине решает «Soviet» vs «Soviet Union».
* **CLI exit codes 0/1/2** — правильно для CI.
* **Vanilla reuse** — экономит деньги DeepL и улучшает консистентность.
* **Тема в одном месте** — `amber.json` + `theme.py` — смена акцента одной строкой.
* **Тесты на реальных файлах** — `test_real_world_files.py` с 110 файлами Kaiserreich — редкая практика, очень ценна.

---

## 6. План исправлений (предлагаемый порядок)

**Фаза 1 — Безопасность данных (1-2 дня):**
1. Унифицировать парсер (`load_existing_translations` → `split_value_line`) + тест.
2. Удалить мёртвый код `localisation.py:115-135`.
3. Включить WAL + timeout в `storage.py`.
4. Убрать второй `load_glossary` в CLI.

**Фаза 2 — Надёжность сети (2-3 дня):**
5. Jitter + глобальный rate limiter для Google.
6. Исправить семафор/батчинг в `translate_batch`.
7. Добавить retry в Java-клиент.

**Фаза 3 — UX/Архитектура (1 неделя):**
8. Разбить `App` на `Controller` + `View`; убрать чтение `StringVar` из worker-потока.
9. Явный баннер в Preview при truncation.
10. Уникальное имя отчёта.

**Фаза 4 — Гигиена:**
11. Поднять `fail-under` до 60, запинить зависимости, поправить `ruff.toml`.

---

## 7. Инструментальная проверка

```
$ python -m unittest discover -s tests -v → 177 OK (1 skipped)
$ python -m compileall → OK
$ ruff check → не установлен (требует ruff==0.17.0)
$ grep -rn "TODO\|FIXME" → 0 (чисто)
```

**Ручная проверка парсера:**

```python
>>> from hoi4translator.localisation import split_value_line
>>> split_value_line(' loading_drill_master_desc: "§L\\"Shoot them again\\"§!"')
# OK — берёт последнюю кавычку с валидным suffix
>>> split_value_line(' KEY:0 "value" trailing junk')
# None — корректно отклоняет
```

---

## 8. Открытые вопросы к владельцу

1. Поддерживать ли Python 3.9? `zip(..., strict=True)` требует 3.10, сейчас используется `zip_equal` — оставить?
2. Нужна ли поддержка `languages.yml`? Сейчас пропускается (`f.lower() != 'languages.yml'`) — а это файл с метаданными языков, его иногда надо переводить.
3. Какой лимит параллелизма считать безопасным для Google endpoint? 5? 10? Сейчас 15 — уже ловит 429 на больших модах.
4. Хранить ли кэш в `%LOCALAPPDATA%` или рядом с EXE для portable-режима?

---

*Аудит подготовлен автоматически (Arena Agent). Готов внести правки из Фазы 1 прямо в ветку `arena/1486f252-hoi4-translator` по команде.*
