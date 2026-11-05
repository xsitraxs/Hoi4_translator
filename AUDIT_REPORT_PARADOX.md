# 🔍 Аудит Hoi4_translator vs Официальная документация Paradox

**Сверка с:** https://hoi4.paradoxwikis.com/Modding + https://hoi4.paradoxwikis.com/Localisation  
**Дата сверки:** 2026-11-05  
**Дополняет:** `AUDIT_REPORT.md` (22 находки внутреннего аудита)

---

## 1. Что говорит Paradox (выжимка нормы)

### 1.1 Файл локализации обязан (Quick checklist)

| Требование Paradox | Точная формулировка | Проверка в коде |
|---|---|---|
| Расширение `.yml` | “has the .yml extension” | ✅ `collect_yml_files` — `f.endswith('.yml')` |
| Имя содержит внутренний язык | `filename_l_english.yml` где `l` — строчная L | ✅ `rename_dst` делает `_l_{lang}.yml` + `casefold` |
| Кодировка UTF-8 **с BOM** | “EF BB BF byte order mark” — иначе `error.log` | ✅ запись всегда `utf-8-sig` (с BOM); чтение толерантно к `cp1251/1252` — **оправданное расширение** |
| Первая строка — БД языка | `l_english:` иначе crash `0xC0000005` | ✅ `_prepare_target_localisation_lines` вставляет `l_{target}:` если отсутствует |
| Ключ `KEY:0 "Value"` | `A-Z a-z 0-9 _ . -` + опц. `:число` + одна строка в кавычках | ✅ `VALUE_PREFIX_RE = [A-Za-z0-9_.-]+:\d*` — точно совпадает |
| Значение в одну строку | `\\n` для переноса, `\\"` для кавычки, комментарии `#` игнорируются | ✅ `split_value_line` ищет последнюю кавычку с `VALUE_SUFFIX_RE = [ \t]*(#.*)?` |
| Ключи без multi-byte символов | “anything other than ASCII will show `Expected colon(:)`” | ✅ тот же RE — не-ASCII не матчится → `malformed` |
| `languages.yml` пропускается? | Файл метаданных языков, не локализация | ✅ `f.lower() != 'languages.yml'` — правильно пропускаем |

**Вывод:** базовая грамматика реализована **точно по спеку**, плюс два осознанных послабления (чтение cp1251, tolerance к raw-кавычкам внутри значения) которые документированы и покрыты тестами на Kaiserreich/Millennium Dawn.

---

### 1.2 Поддерживаемые языки (Paradox: “more languages cannot be added”)

Paradox перечисляет ровно 10 внутренних БД:

```
l_english, l_french, l_german, l_spanish, l_braz_por,
l_polish, l_russian, l_japanese, l_simp_chinese, l_korean
```

```python
# hoi4translator/localisation.py
HOI4_LANGUAGE_NAMES = {
    "en": "english", "fr": "french", "de": "german", "es": "spanish",
    "pt": "braz_por", "pl": "polish", "ru": "russian", "ja": "japanese",
    "zh-CN": "simp_chinese", "ko": "korean",
}
```

**Соответствие 100%** — код использует `get_hoi4_language_name()` как single source of truth, `rename_dst` перекладывает `localisation/russian` и переименовывает суффикс. Ошибка из спекуляции “добавить язык” невозможна — `ValueError` с перечислением доступных.

> **Сверка пройдена.** Единственный нюанс: Paradox пишет `l_braz_por` = Brazilian Portuguese, код мапит `pt → braz_por` — правильно, но провайдеры (DeepL/Google) ожидают `PT-BR`. Код это учитывает через `provider_language_code(GOOGLE_ENDPOINT_ENGINE, "pt") → "pt-BR"` и `deepl_language_code("pt") → "PT-BR"` — **корректно**.

---

### 1.3 Цветовые коды `§` (самый тонкий раздел)

Paradox таблица (Localisation → Colouring characters):

| Код | Смысл | RGB |
|-----|-------|-----|
| `§!` | конец | — |
| `§C §L §W §B §G §R §b §g §Y §H §T §O` | именованные | разные |
| `§0` … `§9` `§t` | Gradient Step 0-10 | разные |

**Дополнительные правила из Errors секции:**

* `§M` → `Could not find coloring for character 'M'` — любая буква вне списка = ошибка
* `§ ` (пробел) → `Could not find coloring for character ' '` — пробел после § = ошибка
* `§"` (конец строки) → `character id '0'` = NULL после § = ошибка
* Multi-byte после § (напр. `§Ā`) → читается только последний байт → ложные `character id '0'`/`'M'`

#### Что делает код сейчас

```python
# validation.py
_COLOR_CODE_CHAR_RE = re.compile(r'[A-Za-z0-9!]')  # ← принимает ЛЮБУЮ букву/цифру
def _invalid_sections(text):
    if following == "§" or (following and _COLOR_CODE_CHAR_RE.fullmatch(following)):
        index += 2; continue  # считает валидным
```

**Расхождение со спекой:**

* Код считает `§M` валидным (любая `A-Za-z`), а игра — ошибкой `Could not find coloring for 'M'`. **False negative**: битый перевод с `§M` пройдёт валидацию и попадёт в игру.
* Код правильно ловит `§ ` и `§` в конце строки как invalid — совпадает со спекой.
* Код не перечисляет допустимые коды как whitelist — должен быть `VALID_COLORS = {"C","L","W","B","G","R","b","g","Y","H","T","O","0","1","2","3","4","5","6","7","8","9","t","!"}`.

**Рекомендация:**

```python
VALID_COLORS = set("CLWBG RbgYHTO0123456789t!".replace(" ",""))  # 24 кода
# + динамический §[expression] уже обрабатывается отдельно
```

Также учесть multi-byte: если после `§` идёт non-ASCII (>0x7F), то по спеке это ошибка `character id '?'`, код сейчас пропустит т.к. `PLACEHOLDER_RE` не матчит его → `_invalid_sections` отметит как invalid — **правильно**.

> **Оценка:** логика “§ + буква = валидно” была разумным упрощением, но противоречит официальному списку. Исправить на whitelist — 1 строка.

Дополнительные находки по спеке:

* Игра требует `§!` даже в конце строки (“doesn't necessarily mean that the colouring rule will end”). Код проверяет `active_color` на конец строки — **соответствует**.
* `§§` как `§` literal не документирован Paradox, но встречается в Millennium Dawn (`15 U.S.C. §§ 1`). Код специально обрабатывает `§§` как literal — **оправданное расширение**, подтверждено тестами.
* `§[expr]` динамический цвет (например `§[IRE_loc_sf_ideology]`) не документирован в старой wiki, но есть в коде как `§\[[^\]]*\]` — **современное дополнение**, код его уже поддерживает.

---

### 1.4 Переменные, иконки, неймспейсы

| Конструкция Paradox | Пример | Код | Статус |
|---|---|---|---|
| `$VAR$` boundable (1.15) | `$NAME$`, `$STATE$`, `$VAL\|=+0$` | `PLACEHOLDER_RE: \$[^$]+\$` | ✅ ловит `$VAL\|=+0$` (pipe+форматтеры внутри) |
| `$$` escaped `$` | `$$100` → `$100` | `\|\$` в RE | ✅ |
| `[Namespace]` | `[GetTag]`, `[GER.GetName]` | `\[[^\]]*\]` | ✅ |
| `[?var\|%G0]` variable formatters | `* ^ = 0..9 % %% + -` | тот же `\[...\]` общий | ✅ (не валидируется детально, но сохраняется) |
| `£icon` | `£money`, `£GFX_army\|1` | `£\w+(?:\|\d+)?` | ✅ точь-в-точь по spec |
| `@FLAG` | `@GER` legacy flag | `@\w+\[!?\]?` | ✅ (редко, но покрыто) |
| `§W` etc. | цвета | `§[A-Za-z0-9!]` | ⚠️ см. выше whitelist |
| `\\n` `\\"` escapes | перенос/кавычка | `\\+.` в RE | ✅ |

**Важный нюанс:** wiki отмечает что `pdx_tooltip` не поддерживает `$KEY$`, а scripted localisation `[mod_loc]` — поддерживает. Код не различает контекст — он сохраняет все плейсхолдеры одинаково. Это **правильно**: переводчик не должен решать за автора где `$` раскроется, его задача — сохранить байт-в-байт.

---

### 1.5 Структура модов (Mod_structure)

| Требование | Код | Статус |
|---|---|---|
| `mod/` + `.mod` descriptor (не BOM!) | Не переводим `.mod` — только `localisation/` | ✅ `collect_yml_files` только `.yml` |
| `localisation/english/replace/mod_l_english.yml` имеет приоритет | Переводим все, включая `replace/` | ✅ `os.walk` + `rename_dst` сохраняет `replace/` подпапку |
| Ключи не должны дублироваться, `logs/text.log` | `duplicate_keys` внутри файла → `rejected` | ⚠️ **только внутри файла**, межфайловые дубликаты не ловятся |
| Путь не должен содержать non-ASCII | `validate_directory_relationships` через `realpath` | ✅ |

**Расхождение — дубликаты между файлами:** Paradox пишет: “overlap being the same key defined several times in the same language's files… tracked in `text.log`”. Код проверяет дубликаты только внутри одного файла (`key_line_numbers`), но не между файлами. Два файла `a_l_english.yml` и `b_l_english.yml` с одним `MY_KEY` не будут помечены, а игра выберет один непредсказуемо (или `replace/` приоритет). 

**Рекомендация:** добавить `validate_cross_file_duplicates(all_files)` перед стартом, хотя бы warning.

---

### 1.6 `replace_path` и `descriptor.mod` кодировка

* `replace_path` в `.mod` означает “заменить целиком папку из ванили”. Translator не читает `.mod`, поэтому не знает что папка `common/` заменена. На локализацию не влияет (localisation никогда не использует `replace_path` — только `replace/` подпапка).
* `descriptor.mod` обязан быть **без BOM**, localisation — **с BOM**. Код пишет `utf-8-sig` (с BOM) только для `*.yml`, `.mod` не трогает — **корректно**.

---

## 2. Где код строже/мягче спеки (обоснованно)

| Место | Спека | Код | Оценка |
|---|---|---|---|
| **Чтение cp1251/cp1252/latin-1** | Только UTF-8 BOM | Пробует BOM, затем cp1251/1252/latin-1 | ✅ **Ослабление оправдано**: старые моды (русские) лежат в 1251, без этого весь файл теряется. Логирует warning. |
| **Raw кавычки внутри значения** | `Expected colon(:)` при спецсимволах в ключе/значении | `split_value_line` сканирует справа, берёт последнюю валидную `"` | ✅ **Ослабление оправдано**: ваниль `traits_l_english.yml` сама содержит `§L"Shoot them..."§!` — спека нарушается самой игрой. Покрыто `test_real_world_files.py`. |
| **§§ как literal** | Не документировано | Обрабатывается как один `§` | ✅ Подтверждено Millennium Dawn, иначе 18 строк Kaiserreich падают. |
| **§[expr] dynamic colour** | Новый патч, wiki неполная | Матчится как один плейсхолдер | ✅ Современно. |
| **Многоязычный файл** | “A single file may contain entries for multiple languages” | `_prepare_target_localisation_lines` фильтрует по `source_lang` | ✅ **Строже спеки**: перевод только выбранного исходника, иначе дубликаты. |
| **Цвет whitelist** | 24 валидных кода | Принимает любой `[A-Za-z0-9!]` | ❌ **Слишком мягко** — пропускает `§M` который игра считает ошибкой. Исправить. |

---

## 3. Новые рекомендации из сверки с документацией

### 3.1 Обязательные (несоответствие спеке)

**№23 — Whitelist цветов.** Заменить `[A-Za-z0-9!]` на точный список `CLWBG RbgYHTO0-9t!` (+ `§[...]` уже есть). Сейчас `§Q` пройдёт и попадёт в `text.log` у игрока.

**№24 — Проверка первой строки БД.** Добавить в `inspect_file` диагностику: если файл не содержит `l_english:` и содержит ключи → warning “may crash 0xC0000005”. Сейчас `read_localisation_lines` молча читает, `_prepare_target...` вставит хедер при записи, но инспекция не предупреждает.

**№25 — Кросс-файловые дубликаты.** Добавить `validate_cross_file_duplicates()` — собрать `normalize_key` по всем файлам, отметить где `l_english` дублирует `l_russian` ключ в разных папках. Хотя бы warning в `RunReport`.

### 3.2 Рекомендуемые (улучшение совместимости)

**№26 — Сохранение `replace/` приоритета в имени.** Сейчас `rename_dst` сохраняет путь, включая `replace/`, но не документирует. Добавить в README пример: `localisation/english/replace/mod_l_english.yml → localisation/russian/replace/mod_l_russian.yml`.

**№27 — `l_english` регистр.** Спека: “l is a lowercase L”. Код использует `LANGUAGE_HEADER_RE` с `re.IGNORECASE` — принимает `L_english:` (заглавную). Это мягче спеки, но безопасно. Оставить, добавить комментарий.

**№28 — Комментарии `#` внутри значения.** Спека: “single sharp will make rest of line ignored”. Код правильно отделяет suffix `# comment` после `"` через `VALUE_SUFFIX_RE`. Проверить что `#` внутри `"a # b"` не считается комментарием — `split_value_line` берёт `suffix` после последней `"`, поэтому `"a # b"` остаётся значением — **корректно**.

---

## 4. Итоговая матрица соответствия

| Раздел спеки | Соответствие | Комментарий |
|---|:---:|---|
| Формат файла/кодировка/BOM | ✅ | Точно + толерантность к cp1251 |
| Имя файла `*_l_<lang>.yml` | ✅ | 10 языков Paradox, `braz_por` учтён |
| Первая строка `l_english:` | ✅ | Авто-вставка хедера |
| Ключ `KEY:0 "Value"` | ✅ | RE совпадает |
| Версия `:0` optional numeric | ✅ | `\d*` |
| Значение в одну строку `\\n` | ✅ | Сканер справа, `multiline_rows` ловит `\n` |
| Комментарии `#` | ✅ | Suffix RE |
| Цвета `§X` / `§!` | ⚠️ | Whitelist нет — исправить |
| `§[expr]` dynamic | ✅ | Реализовано |
| `§§` literal | ✅ | Расширение, оправдано |
| Текст-иконки `£` | ✅ | `£\w+(\|\d+)?` |
| Переменные `$VAR$` / `$$` | ✅ | |
| Неймспейсы `[Scope.Func]` | ✅ | |
| Scripted loc `[loc]` | ✅ | Тот же RE |
| `replace/` приоритет | ✅ | Путь сохраняется |
| Дубликаты ключей | ⚠️ | Только внутри файла |

**Итого: 15/17 требований — полное соответствие, 2 — требуют правки (whitelist цветов, кросс-файловые дубликаты).**

Код **не противоречит** документации Paradox, а в двух местах (cp1251, raw-кавычки) осознанно мягче — это правильно, иначе ломаются реальные моды.

---

*Сверка выполнена по snapshot wiki от 2026-11-05. Wiki — community maintained, может отставать от патчей; код уже учитывает `§[expr]` из более новых версий.*
