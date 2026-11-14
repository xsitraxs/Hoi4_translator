# Переход с Python-версии на Rust

Версия 2.0 — это перенос того же инструмента на Rust. Поведение сохранено намеренно: те же
правила разбора `.yml`, те же проверки перед записью, тот же формат кэша и отчётов. Ниже —
что изменилось для пользователя и где что теперь лежит.

## Командная строка

| Python 1.4.x | Rust 2.0 |
| --- | --- |
| `python hoi4_translator.py --cli --src A --dst B` | `hoi4-translator --src A --dst B` |
| `--target`, `--source-lang`, `--engine`, `--api-key` | без изменений |
| `--glossary`, `--vanilla`, `--concurrency` | без изменений |
| `--skip-translated`, `--broken-lines`, `--no-report`, `--quiet`, `--dry-run` | без изменений |
| `--java-jar` | принимается и игнорируется (Java больше не нужна) |
| `--engine JavaSim` | работает как `Google`, в stderr выводится предупреждение |
| `--cli` | принимается и игнорируется: CLI — единственный режим в 2.0 |

Коды возврата те же: `0` — всё записано, `1` — были ошибки файлов, `2` — ошибка аргументов.

## Что удалено

* **GUI на tkinter/CustomTkinter** — окно портируется отдельным этапом. Вся логика для него уже
  вынесена в библиотечный слой (`src/lib.rs`), поэтому GUI станет тонкой обёрткой.
* **Движок JavaSim и Java-класс** (`java/`, `hoi4_translator.jar`) — встроенный HTTP-движок Google
  делает то же самое без JVM.
* **`settings.json` и системное хранилище ключей** — ключ DeepL передаётся через `--api-key` или
  `DEEPL_API_KEY`. Хранилище вернётся вместе с окном.
* **Python-зависимости** (`requirements.txt`, `ruff.toml`, PyInstaller-сборка).

## Соответствие модулей

| Python | Rust |
| --- | --- |
| `hoi4translator/config.py` | `src/config.rs` |
| `hoi4translator/localisation.py` | `src/localisation.rs` |
| `hoi4translator/text.py` | `src/text.rs` |
| `hoi4translator/validation.py` | `src/validation.rs` |
| `hoi4translator/glossary.py` | `src/glossary.rs` |
| `hoi4translator/vanilla.py` | `src/vanilla.rs` |
| `hoi4translator/storage.py` | `src/cache.rs` |
| `hoi4translator/providers/` | `src/providers.rs` |
| `hoi4translator/translation.py` | `src/translation.rs` |
| `hoi4translator/java_service.py` | — (движок Google встроен в `translation.rs`) |
| `hoi4translator/pipeline.py` | `src/pipeline.rs` |
| `hoi4translator/inspect.py` | `src/inspect.rs` |
| `hoi4translator/report.py` | `src/report.rs` |
| `hoi4translator/cli.py` | `src/cli.rs` |
| `hoi4translator/gui/` | — (в работе) |

## Что осталось на диске

* **Кэш** — те же таблицы SQLite (`cache`, `file_sources`) и та же версия формата ключа (`2`),
  поэтому записи Python 1.4.x используются повторно; перевод заново не нужен.
* **Отчёты** — те же имена (`hoi4_translator_report_<дата>.json`/`.csv`) и те же поля.
* **Лог** — `hoi4_translator.log` в том же каталоге данных. Rust-версия дописывает файл и не
  ротирует его: при разрастании удалите файл вручную.

## Технические отличия реализации

* **Потоки вместо asyncio.** Файл переводится пулом из `--concurrency` потоков, каждый запрос
  блокирующий (ureq) с таймаутами `8 с` на соединение и `20 с` на ответ. Бюджет на файл считается
  так же: `60 с + 0.35 с/строка`, минимум 120 с.
* **`Regex` из Rust не умеет lookaround.** Поиск терминов глоссария с границами слов реализован
  проходом по символам (длинные термины проверяются первыми), результат тот же: `Division` внутри
  `divisions` не заменяется.
* **Атомарная запись.** На Windows `rename` не заменяет существующий файл, поэтому при ошибке
  файл-назначение удаляется и `rename` повторяется — окно «нет файла» остаётся в пределах одного
  системного вызова.
* **Кодировки.** Как и раньше, сначала пробуется UTF-8 (с BOM и без), затем `cp1251`. Файлы без BOM
  в `cp1252` по-прежнему прочитаются как `cp1251` — это известное ограничение обеих версий.

## Чем порт проверен

Песочница, в которой писался код 2.0, не имела Rust-тулчейна (crates.io и
static.rust-lang.org недоступны), поэтому компиляция и тесты выполнялись в GitHub
Actions:

* **Linux:** `cargo clippy --all-targets`, `cargo test --all` (юнит-тесты модулей +
  `tests/cli_integration.rs`) и прогон собранного бинарника по демо-моду в `--dry-run`.
  Шаг `cargo fmt --all --check` блокирующий; сам код отформатирован job-ом
  «Format the branch» в том же прогоне.
* **Windows (MSVC):** `cargo build --release` (включая сборку встроенного SQLite),
  `hoi4-translator.exe --help` и `--dry-run` по демо-моду. Сборка Windows-бинарника
  запускается на тегах и вручную; один раз она была прогнана на pull request, чтобы
  подтвердить, что порт собирается MSVC.

Что этим **не** проверено: живой перевод через Google/DeepL/MyMemory (тесты намеренно
без сети), поведение на больших модах и совместимость кэша с файлами, созданными
Python-версией 1.4 на реальном моде. Это стоит прогнать на своём моде перед релизом.
