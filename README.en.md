# 🌍 HoI4 Localization Tool

[![Latest release](https://img.shields.io/github/v/release/xsitraxs/Hoi4_translator?label=release)](https://github.com/xsitraxs/Hoi4_translator/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/xsitraxs/Hoi4_translator/total)](https://github.com/xsitraxs/Hoi4_translator/releases)
[![Tests](https://github.com/xsitraxs/Hoi4_translator/actions/workflows/build.yml/badge.svg)](https://github.com/xsitraxs/Hoi4_translator/actions/workflows/build.yml)
[![License](https://img.shields.io/github/license/xsitraxs/Hoi4_translator)](LICENSE)

**Batch-translate Hearts of Iron IV mod localization, without breaking Paradox formatting.**

Point the tool at a mod's `localisation` folder, pick a target language and an engine: it translates the values, keeps the keys untouched, and refuses to write a file it cannot verify. `$VALUE$`, `§R…§!`, `£money`, `[GetTag]`, `\n` and `"` escapes survive the round trip.

**Читать по-русски:** [README.md](README.md) — the Russian README is the full one (every option, cache behaviour, reports, troubleshooting).

---

## ⬇️ Download

| What | Who |
| --- | --- |
| **[Hoi4_Translator.exe](https://github.com/xsitraxs/Hoi4_translator/releases/latest)** | Windows, no Python required — just run it |
| `hoi4_translator.jar` | JavaSim engine, put it next to the EXE (needs Java 11+) |
| From source | Python 3.9+, `pip install -r requirements.txt`, then `python hoi4_translator.py` |

![HoI4 Translator main window, dark theme](docs/images/main-window-dark.png)

<!--
Still to capture in docs/images/, then uncomment:

![Light theme](docs/images/main-window-light.png)
![Translation preview](docs/images/preview.png)
![Demo](docs/images/demo.gif)
-->

## 🔒 Is the EXE safe to run?

Antivirus and SmartScreen warnings on PyInstaller builds are normal, but you should still verify:

* **Where it came from.** The EXE is built by GitHub Actions from this repository; each release notes which CI run produced it.
* **Checksum.** Every release lists a SHA256 — check it locally:
  ```powershell
  certutil -hashfile Hoi4_Translator.exe SHA256
  ```
  or `sha256sum` / `shasum -a 256` on Linux/macOS.
* **SmartScreen.** The binary is not code-signed (that costs money), so Windows will warn you: "More info" → "Run anyway".
* **VirusTotal.** The release notes link a scan report for that exact build.
* **No trust in binaries at all.** MIT source is right here — run it with Python 3.9+ instead.

## ✨ What it does

* **Engines:** DeepL (official client, API key stored in the OS credential store), Google Translate, MyMemory, and an optional Java engine (JavaSim).
* **Fallback chain:** `DeepL → Google → MyMemory`. Every substitution is logged and counted in the summary, so a paid engine never degrades to a free one silently.
* **Placeholder protection:** variables, formatters, colour codes, gradients, icons and escape sequences are masked before translating and restored afterwards.
* **Line-level validation:** if a translation loses a marker, the file is not written by default (`Do not write the file`) — alternatively the broken line can be replaced with the English original and reported.
* **Cache:** SQLite. A key is `text + language pair + engine`, so switching engines does not serve stale results. "Skip already translated" also stores a checksum of the source line, so a mod author's edit invalidates the old translation.
* **Preview:** the first 1000 lines of each file can be reviewed and edited before saving.
* **Run report:** `hoi4_translator_report_<date>.json` and `.csv` next to the output folder — files, written/rejected/cancelled counts, fallbacks, broken and recovered lines.
* **Output format:** UTF-8 **with BOM**, `l_<language>:` header, `_l_<language>.yml` suffix, relative folder structure preserved.
* **GUI:** CustomTkinter, dark and light themes, live log, twin progress bars, cancel that actually cancels requests.

## 🛠 Requirements

1. **Python 3.9+** (CI runs 3.9, 3.11 and 3.13) — only for the source install.
2. **Java JDK 11+** — only for the optional JavaSim engine.
3. `python -m pip install -r requirements.txt`

### Running from source

```bash
git clone https://github.com/xsitraxs/Hoi4_translator.git
cd Hoi4_translator
python -m pip install -r requirements.txt
python hoi4_translator.py        # --help / --version also work
```

## 📁 Choosing folders

Use the **same root level** for source and destination. If the source is a mod root, the output must be another mod root. If the source is the `localisation` folder, pick an output `localisation` root. The tool keeps the relative structure and swaps the language folder/suffix for the target language.

## ⚠️ Known limitations

* The Google path uses the unofficial `translate.googleapis.com/translate_a/single` endpoint; its response format can change at any time (429 retries are implemented).
* MyMemory has hard daily limits — it is the last resort in the chain.
* The preview window covers the first 1000 lines; the rest is saved as translated.
* The GUI is currently Russian-only.

## 👩‍💻 Development

```bash
python -m pip install -r requirements.txt ruff coverage
python -m ruff check hoi4_translator.py hoi4translator/ tests/
python -m unittest discover -s tests -v
```

Tests describe behaviour (format parser, marker validation, cache, cancellation, file writing). `tests/tk_stub.py` replaces tkinter and CustomTkinter with recording stubs, so GUI wiring is covered without a display — visual appearance still needs a human eye.

## 📄 License

MIT — see [LICENSE](LICENSE).
