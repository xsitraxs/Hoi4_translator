# 🌍 HoI4 Localization Tool

[![Latest release](https://img.shields.io/github/v/release/xsitraxs/Hoi4_translator?label=release)](https://github.com/xsitraxs/Hoi4_translator/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/xsitraxs/Hoi4_translator/total)](https://github.com/xsitraxs/Hoi4_translator/releases)
[![Tests](https://github.com/xsitraxs/Hoi4_translator/actions/workflows/build.yml/badge.svg)](https://github.com/xsitraxs/Hoi4_translator/actions/workflows/build.yml)
[![License](https://github.com/xsitraxs/Hoi4_translator/blob/main/LICENSE)](LICENSE)
[![Rust](https://img.shields.io/badge/rust-1.75%2B-orange)](https://www.rust-lang.org)

**Translate Hearts of Iron IV mods in batches, not by hand.** The tool walks a mod's `.yml`
localisation files, translates only the values (keys stay untouched) and never breaks Paradox
markup: `$VALUE$`, `§R…§!`, `£money`, `[GetTag]`, `\n`. Every file is validated before it is
written, so a broken translation never reaches the game.

**Read this in:** [Русский](README.md)

> **Version 2.0 is a Rust rewrite.** The core, the engines and the CLI were ported 1:1 from
> Python (file format, validation, cache, reports). Java is gone: the Google engine is built in.
> The desktop window is the next step — see [Roadmap](#-roadmap); today the tool runs as
> `hoi4-translator` in a terminal.

---

### ⬇️ Download / build

| What | For whom |
| --- | --- |
| `hoi4-translator.exe` | Windows, nothing to install — just run it (built by CI) |
| `hoi4-translator-gui.exe` | the same with a window in the browser: `--open` (built by CI next to the console one) |
| From source | Rust 1.75+, `cargo build --release`, binaries in `target/release/hoi4-translator` and `hoi4-translator-gui` |

```bash
git clone https://github.com/xsitraxs/Hoi4_translator.git
cd Hoi4_translator
cargo build --release
./target/release/hoi4-translator --help
./target/release/hoi4-translator-gui --help
```

### What the output looks like

Keys are untouched and markup stays in place — only the text changes:

```yml
# before (en)
l_english:
 GER_rearm_the_rhine:0 "Rearm $STATE$ with £army_experience|1 and [GetPlayerTag] backing."
 GER_day_of_infamy:0 "Our diplomats call it \"an incident\".\nThe press calls it war."

# after (ru)
l_russian:
 GER_rearm_the_rhine:0 "Перевооружите $STATE$ с помощью £army_experience|1 и поддержки [GetPlayerTag]."
 GER_day_of_infamy:0 "Наши дипломаты называют это \"инцидентом\".\nПресса называет это войной."
```

If an engine damages a single marker, the line is reported and — by default — the file is not
written at all.

## 🔒 Is the EXE safe to run?

* **Where it comes from.** The binary is built by GitHub Actions from open source; the CI run is
  linked in the release notes.
* **Checksum.** Every release ships `SHA256SUMS.txt`: `certutil -hashfile hoi4-translator.exe SHA256` (and the same for `hoi4-translator-gui.exe`)
  on Windows, `sha256sum -c SHA256SUMS.txt` elsewhere.
* **SmartScreen.** The binary is not code-signed (that costs money), so Windows shows
  "Windows protected your PC": *More info* → *Run anyway*.
* **No trust at all.** The code is MIT: `cargo build --release` gives you the same program.

## ✨ What it does

* **Three engines with a fallback chain:** Google (unofficial HTTP endpoint, no API key), DeepL
  (official API, key via `--api-key` or `DEEPL_API_KEY`), MyMemory. An outage *or an answer that
  would break the file* sends **only that line** to the next engine:
  `DeepL → Google → MyMemory`, `Google → MyMemory`, `MyMemory → Google`.
* **Term glossary:** a `term → translation` file (TSV/CSV/JSON). Terms are replaced by markers
  *before* the request and written back translated, so an engine cannot translate, reorder or
  decline them. See [`docs/glossary-example.tsv`](docs/glossary-example.tsv).
* **Vanilla reuse:** point it at the game's `localisation` folder and lines the mod copied from
  the base game take the official translation instead of a second machine translation.
* **Offline inspection:** `--dry-run` parses everything and reports malformed lines, duplicate keys
  and `§` problems without a single network request.
* **SQLite cache:** identical lines are never paid for twice; `--skip-translated` stores a
  fingerprint of every source line, so edited English text is translated again.
* **Run report:** JSON + CSV next to the output folder — what was written, rejected, repaired, and
  how often the engine had to fall back.
* **Atomic writes:** files are replaced whole (temp file + rename), so the game can never read half
  a line.
* **No Java, no Python:** a single self-contained binary with SQLite compiled in.

## 🖥 The window (GUI)

The window is the same translation with buttons and a log, and it needs no native libraries:
the program starts a server on this machine and opens a page in the browser.

```bash
hoi4-translator-gui --open          # prints one URL with a token and opens it
hoi4-translator-gui --port 9000     # another port; a busy port falls back to a free one
hoi4-translator-gui --host 0.0.0.0  # expose the window to the network (default: this PC only)
```

* the same settings as the flags: folders, language, engine, DeepL key, glossary, vanilla, threads,
  broken-line mode, source encoding and the mod build;
* "Check only" is `--dry-run`: parsing without internet and without writing;
* per-file and per-line progress, a log with `info`/`warning`/`error` levels, a file list with statuses;
* "Stop" ends the run after the current file — the report and the cache are still written, like Ctrl+C;
* the "Stats" and "Clear" buttons read and empty the same cache as `--cache-stats`/`--clear-cache`;
* settings are remembered in the browser (`localStorage`), the cache and the log are the same as the CLI's.

The source-language list is the game's own (`HOI4_LANGUAGE_NAMES`), not an arbitrary set of engine
codes: only what the game can read is worth translating.

**Access token.** The address carries a random token; any request without it gets `401`. Treat the
address as the password to the window: the page moves the token into `sessionStorage` and clears the
address bar, and the server sends `Referrer-Policy: no-referrer`, so following a link cannot leak it.
The server listens on `127.0.0.1` unless `--host` says otherwise, and "Close server" in the page
footer (or Ctrl+C) ends it.

## 🛠 Requirements

1. **Rust 1.75+** to build from source. The released binary needs nothing.
2. No Python, no Java, no installed game.

## 🖥 Usage

```bash
hoi4-translator \
  --src "MOD/localisation/english" \
  --dst "OUT/localisation/russian" \
  --target ru --engine Google
```

Check files without translating anything (no game, no internet):

```bash
hoi4-translator --src examples/demo-mod --dst /tmp/out --target ru --dry-run
```

| Flag | Meaning |
| --- | --- |
| `--engine Google\|DeepL\|MyMemory` | translation engine (Google by default) |
| `--api-key KEY` / `DEEPL_API_KEY` | DeepL key; without it `--engine DeepL` exits with code 2 |
| `--glossary FILE` | term glossary (TSV/CSV/JSON) |
| `--vanilla DIR` | the game's `localisation` folder, to reuse official translations |
| `--skip-translated` | do not translate lines already translated in the output folder |
| `--broken-lines reject\|keep-original` | what to do with a line that breaks the format |
| `--source-encoding ENC` | source encoding when detection is wrong: `auto`, `utf-8`, `cp1251`, `cp1252`, `latin-1` |
| `--concurrency N` | parallel requests per file (15 by default, at most 64) |
| `--no-report` | skip the JSON/CSV report |
| `--file-timeout SECONDS` | per-file translation budget (0 — no limit) |
| `--quiet` | final summary only |
| `--cache-stats` | print what the translation cache holds and exit |
| `--clear-cache` | empty the translation cache and exit |
| `--dry-run` | parse and report only, nothing translated or written |
| `--make-mod NAME` | build the finished mod: folder, `descriptor.mod`, `.mod` (see [`docs/MAKE-A-TRANSLATED-MOD.md`](docs/MAKE-A-TRANSLATED-MOD.md)) |
| `--mod-folder NAME` | folder and `.mod` file name (ASCII, no spaces) |
| `--mod-target PATH` | target mod descriptor: supplies `dependencies` and `supported_version` |
| `--mod-version X.Y.Z` | version of the translation mod (default 1.0.0) |
| `--mod-supported-version X.Y.*` | `supported_version` written into the descriptor |
| `--install` | copy the finished mod into the game's mods folder |
| `--install-dir DIR` | put the mod somewhere other than the default mods folder |

Exit codes: `0` — everything written; `1` — at least one file was not written (or `--dry-run`
found blocking problems, or `--make-mod` had to skip the build); `2` — bad arguments or folders
(including `--concurrency` above 64, an empty `--make-mod` name, `--install-dir` without
`--install`), nothing was started.

## 📁 Choosing folders

Pick separate source and output folders: nesting is rejected, so a run can never read its own
output. Keep the same root level as the source (mod root → mod root, `localisation` →
`localisation`). The relative structure is preserved and the language folder and suffix are
replaced with the target language.

## 🧠 Cache and "skip already translated"

* The cache is SQLite (`translation_cache.db`) in the application data directory. Entries are keyed
  by text + language pair + engine.
* **By default each engine reads only its own cache** — otherwise switching Google → DeepL would
  keep serving the old Google text forever.
* The cache carries a format version (`2`); Python 1.4.x entries are compatible, so updating does
  not force a full re-translation.
* `--skip-translated` also stores the fingerprint of each source line. Edited English text is
  re-translated; files without fingerprints keep their translation with a warning that it cannot be
  proven up to date.

Data directory: `%LOCALAPPDATA%\Hoi4Translator` (Windows),
`~/Library/Application Support/Hoi4Translator` (macOS),
`$XDG_DATA_HOME/Hoi4Translator` or `~/.local/share/Hoi4Translator` (Linux). The log
(`hoi4_translator.log`) lives there too; the text of translated lines is never logged.

## ⚠️ Known limitations

* **No desktop window yet:** 2.0 is a CLI; the GUI is the next milestone, and all translation logic
  it needs is already a library.
* Google is used through the unofficial `translate.googleapis.com` endpoint, whose format may
  change at any time (429 retries are implemented).
* MyMemory is the last fallback and has strict daily quotas.
* Legacy flags: `--cli` is accepted and ignored, `--engine JavaSim` behaves like `Google` (with a
  warning), `--java-jar` is ignored.
* BOM-less `cp1252` files are decoded as `cp1251` — same behaviour as the Python build; re-save the
  file as UTF-8 if it matters.

## 👩‍💻 Development

```bash
cargo fmt --all
cargo clippy --all-targets
cargo test
cargo build --release
```

Tests describe behaviour (format parser, markup validation, glossary, cache, file writing, exit
codes), not implementation details, and never touch the network: any value they translate is either
pinned by a glossary or pure markup.

CI (`.github/workflows/build.yml`) runs `clippy` and `cargo test` on every pull request
(module unit tests plus `tests/cli_integration.rs`), then runs the real binary over the demo
mod with `--dry-run`, which exercises the parser, the CLI and the exit codes without the
network. `cargo fmt --check` is blocking, but a pull request from this repository is fixed
rather than failed: version 2.0 was written where no Rust toolchain was available, so the
"Format the branch" step runs `cargo fmt --all`, adds `Cargo.lock` when it is missing, and
commits the result to the branch. Forks and other runs only check the formatting.

The Windows binary is built for tags and manual runs, smoke-tested with `--help`, and
published with `SHA256SUMS.txt`. `Cargo.lock` is checked in, so every build resolves the
same dependency versions.

## 🔁 Coming from Python 1.4

`python hoi4_translator.py --cli --src … --dst …` becomes `hoi4-translator --src … --dst …`; the
flags are the same. JavaSim/JAR are not needed, the cache and report formats are unchanged, and the
the GUI is not Tkinter any more: a local server plus a page in the browser, with settings kept in
`localStorage` and no OS keyring (the DeepL key comes from the form or `DEEPL_API_KEY`). Details:
[`docs/PORTING-NOTES.md`](docs/PORTING-NOTES.md). The 2.0 audit — what was checked,
what was found and fixed, and what is deliberately left open: [`docs/AUDIT.md`](docs/AUDIT.md).

Turning a finished translation into a launcher-ready mod (folder layout,
`descriptor.mod`, `.mod`, installation, common mistakes):
[`docs/MAKE-A-TRANSLATED-MOD.md`](docs/MAKE-A-TRANSLATED-MOD.md).

## 📝 Roadmap

- [x] Rust core: parser, validation, glossary, vanilla reuse, cache, reports.
- [x] CLI with the same flags and exit codes as the Python build.
- [x] Google/DeepL/MyMemory without Python, Java or external JARs.
- [x] Building the translation mod from the CLI: `--make-mod` (descriptor + `.mod` +
      `--install` into the mods folder), see [`docs/MAKE-A-TRANSLATED-MOD.md`](docs/MAKE-A-TRANSLATED-MOD.md).
- [x] The window on the same Rust layer: a local server plus a browser page, with a live log and progress.
- [ ] A native shell (Tauri) around the same window, for installers and a shortcut.
- [ ] Page-by-page preview of the translation before it is written.
- [ ] User replacement rules based on regular expressions.
- [ ] DeepL-style glossary (server-side dictionary).
- [ ] More engines (Yandex, Microsoft, local LLMs through an OpenAI-compatible API).

## 📄 License

MIT — see [LICENSE](LICENSE).
