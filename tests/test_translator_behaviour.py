import asyncio
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# The service logic can be tested on headless builders where Python was built
# without Tk. Production still imports the real tkinter module normally.
try:
    import tkinter  # noqa: F401
except ModuleNotFoundError:
    tk_stub = types.ModuleType("tkinter")
    tk_stub.Tk = type("Tk", (), {})
    tk_stub.Toplevel = type("Toplevel", (), {})
    tk_stub.StringVar = type("StringVar", (), {})
    tk_stub.BooleanVar = type("BooleanVar", (), {})
    tk_stub.IntVar = type("IntVar", (), {})
    for name in ("ttk", "filedialog", "scrolledtext", "messagebox"):
        setattr(tk_stub, name, types.ModuleType(f"tkinter.{name}"))
    sys.modules["tkinter"] = tk_stub

import hoi4_translator as translator_module


class TargetLanguageTests(unittest.TestCase):
    def test_target_languages_are_exactly_the_builtin_hoi4_databases(self):
        expected = {
            "en": "english", "fr": "french", "de": "german", "es": "spanish",
            "pt": "braz_por", "pl": "polish", "ru": "russian", "ja": "japanese",
            "zh-CN": "simp_chinese", "ko": "korean",
        }
        self.assertEqual(translator_module.HOI4_LANGUAGE_NAMES, expected)
        self.assertEqual(
            {code for code, _ in translator_module.HOI4_TARGET_LANGS}, set(expected)
        )
        self.assertEqual(translator_module.get_hoi4_language_name("PT"), "braz_por")
        with self.assertRaises(ValueError):
            translator_module.get_hoi4_language_name("it")

    def test_brazilian_portuguese_uses_the_internal_database_name_everywhere(self):
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory) / "source"
            source = source_root / "localisation" / "english" / "events_l_english.yml"
            result = translator_module.rename_dst(
                str(source), str(source_root), str(Path(directory) / "out"), "pt"
            )

            self.assertEqual(
                Path(result),
                Path(directory) / "out" / "localisation" / "braz_por" / "events_l_braz_por.yml",
            )

    def test_generic_yml_name_receives_required_language_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory) / "source"
            source = source_root / "localisation" / "custom" / "messages.yml"
            result = translator_module.rename_dst(
                str(source), str(source_root), str(Path(directory) / "out"), "en"
            )

            self.assertEqual(
                Path(result),
                Path(directory) / "out" / "localisation" / "custom" / "messages_l_english.yml",
            )

    def test_unsupported_target_is_rejected_instead_of_making_an_invalid_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                translator_module.rename_dst(
                    str(Path(directory) / "events_l_english.yml"),
                    directory, str(Path(directory) / "out"), "uk"
                )

    def test_deepl_receives_brazilian_portuguese_target_code(self):
        captured = {}

        class FakeResult:
            text = "translated:Hello"

        class FakeDeepLClient:
            def __init__(self, api_key):
                captured["api_key"] = api_key

            def translate_text(self, text, source_lang, target_lang):
                captured.update(text=text, source_lang=source_lang, target_lang=target_lang)
                return FakeResult()

        translator = object.__new__(translator_module.AsyncTranslator)
        translator.api_key = "test-key"
        translator.source_lang = "en"
        fake_deepl = types.SimpleNamespace(DeepLClient=FakeDeepLClient)
        with patch.object(translator_module, "deepl", fake_deepl, create=True):
            result = translator_module.AsyncTranslator._deepl_sync(
                translator, "Hello", "pt", retries=1
            )

        self.assertEqual(result, "translated:Hello")
        self.assertEqual(captured["api_key"], "test-key")
        self.assertEqual(captured["source_lang"], "EN")
        self.assertEqual(captured["target_lang"], "PT-BR")

    def test_deepl_uses_provider_supported_simplified_chinese_codes(self):
        captured = {}

        class FakeResult:
            text = "你好"

        class FakeDeepLClient:
            def __init__(self, api_key):
                pass

            def translate_text(self, text, source_lang, target_lang):
                captured.update(source_lang=source_lang, target_lang=target_lang)
                return FakeResult()

        translator = object.__new__(translator_module.AsyncTranslator)
        translator.api_key = "test-key"
        translator.source_lang = "zh-CN"
        fake_deepl = types.SimpleNamespace(DeepLClient=FakeDeepLClient)
        with patch.object(translator_module, "deepl", fake_deepl, create=True):
            result = translator_module.AsyncTranslator._deepl_sync(
                translator, "你好", "zh-CN", retries=1
            )

        self.assertEqual(result, "你好")
        self.assertEqual(captured["source_lang"], "ZH")
        self.assertEqual(captured["target_lang"], "ZH-HANS")

    def test_target_language_is_applied_to_filename_and_language_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory) / "source"
            destination_root = Path(directory) / "translated"
            source = source_root / "localisation" / "english" / "events_l_english.yml"
            source.parent.mkdir(parents=True)
            source.touch()

            result = translator_module.rename_dst(
                str(source), str(source_root), str(destination_root), "pl"
            )

            self.assertEqual(
                Path(result),
                destination_root / "localisation" / "polish" / "events_l_polish.yml",
            )

    def test_chinese_uses_the_hoi4_language_name(self):
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory) / "source"
            source = source_root / "english" / "events_l_english.yml"
            result = translator_module.rename_dst(
                str(source), str(source_root), str(Path(directory) / "out"), "zh-CN"
            )

            self.assertEqual(
                Path(result),
                Path(directory) / "out" / "simp_chinese" / "events_l_simp_chinese.yml",
            )

    def test_existing_non_cyrillic_translations_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "localisation_l_german.yml"
            path.write_text(
                'l_german:\n KEY_ONE:0 "Guten Tag"\n KEY_TWO:0 "你好"\n EMPTY:0 ""\n BAD:0 "junk" trailing\n',
                encoding="utf-8-sig",
            )

            existing = translator_module.load_existing_translations(str(path))

            self.assertEqual(existing, {"KEY_ONE:0": "Guten Tag", "KEY_TWO:0": "你好"})

    def test_source_and_destination_cannot_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mod"
            destination = source / "translated"
            source.mkdir()

            with self.assertRaises(ValueError):
                translator_module.validate_directory_relationships(str(source), str(destination))

            translator_module.validate_directory_relationships(
                str(source), str(Path(directory) / "output")
            )

    def test_atomic_write_leaves_previous_file_intact_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "localisation.yml"
            destination.write_text("old contents\\n", encoding="utf-8")

            def failing_lines():
                yield "new first line\\n"
                raise OSError("simulated write failure")

            with self.assertRaises(OSError):
                translator_module.atomic_write_lines(str(destination), failing_lines())

            self.assertEqual(destination.read_text(encoding="utf-8-sig"), "old contents\\n")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_preview_rejects_missing_game_placeholders(self):
        original = "Secure $STATE$ with [GetTag] §Yunits§!."
        valid = "Защитите $STATE$ вместе с [GetTag] §Yвойсками§!."
        invalid = "Защитите государство войсками."

        self.assertEqual(
            translator_module.validate_preview_translations([original], [valid]), []
        )
        issues = translator_module.validate_preview_translations([original], [invalid])
        self.assertTrue(issues)
        self.assertIn("потерян", issues[0])

    def test_settings_migrate_legacy_key_without_writing_it_to_json(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "data" / "settings.json"
            legacy_path = Path(directory) / "home" / ".hoi4_translator_settings.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                json.dumps({"src": "source", "api_key": "legacy-secret"}),
                encoding="utf-8",
            )

            with patch.object(translator_module, "SETTINGS_FILE", str(settings_path)), patch.object(
                translator_module, "LEGACY_SETTINGS_FILE", str(legacy_path)
            ):
                settings = translator_module.load_settings()
                self.assertEqual(settings["_legacy_api_key"], "legacy-secret")
                self.assertNotIn("api_key", json.loads(settings_path.read_text(encoding="utf-8")))

                translator_module.save_settings({"src": "source", "api_key": "do-not-save"})
                translator_module.scrub_plaintext_api_keys()

                self.assertNotIn("api_key", json.loads(settings_path.read_text(encoding="utf-8")))
                self.assertNotIn("api_key", json.loads(legacy_path.read_text(encoding="utf-8")))

    def test_only_os_backed_keyring_providers_are_used(self):
        memory = {}

        class FakeKeyring:
            def get_keyring(self):
                return NativeBackend()

            def get_password(self, service, username):
                return memory.get((service, username))

            def set_password(self, service, username, password):
                memory[(service, username)] = password

            def delete_password(self, service, username):
                memory.pop((service, username), None)

        NativeBackend = type(
            "WindowsKeyring", (), {"__module__": "keyring.backends.Windows", "priority": 1}
        )
        with patch.object(translator_module, "keyring", FakeKeyring()):
            self.assertTrue(translator_module.store_api_key("secret"))
            self.assertEqual(translator_module.load_api_key(), "secret")
            self.assertTrue(translator_module.store_api_key(""))
            self.assertEqual(translator_module.load_api_key(), "")

        PlaintextBackend = type(
            "PlaintextKeyring", (), {"__module__": "keyrings.alt.file", "priority": 1}
        )
        class FakePlaintextKeyring(FakeKeyring):
            def get_keyring(self):
                return PlaintextBackend()

        with patch.object(translator_module, "keyring", FakePlaintextKeyring()):
            self.assertFalse(translator_module.store_api_key("secret"))


class LocalizationSyntaxTests(unittest.TestCase):
    def test_game_tokens_are_protected_and_restored_byte_for_byte(self):
        original = r'Cost $$5: [$STATE$] §2army§! £GFX_army|1 \"quoted\" \n @TAG'
        protected, tokens = translator_module.protect_placeholders(original)

        self.assertNotIn("§2", protected)
        self.assertNotIn("£GFX_army|1", protected)
        self.assertNotIn("$$", protected)
        self.assertNotIn(r'\"', protected)
        self.assertEqual(translator_module.restore_placeholders(protected, tokens), original)

        trailing_backslash = r"ends in a literal backslash \\"
        protected_slash, slash_tokens = translator_module.protect_placeholders(trailing_backslash)
        self.assertNotIn(r"\\", protected_slash)
        self.assertEqual(
            translator_module.restore_placeholders(protected_slash, slash_tokens),
            trailing_backslash,
        )
        self.assertEqual(
            translator_module.PlaceholderValidator.validate(original, original.replace("§2", "§3")),

            ["потерян: '§2'", "лишний: '§3'"],
        )

    def test_postprocessor_does_not_rewrite_tokens_or_contextual_syntax(self):
        original = r'[(OBJECT ? TRUE_CASE : FALSE_CASE)] costs $$5 £GFX_icon|1 and \"quoted\"'
        result = translator_module.PostProcessor.process(original, original)

        self.assertIn("[(OBJECT ? TRUE_CASE : FALSE_CASE)]", result)
        self.assertIn("$$5", result)
        self.assertIn("£GFX_icon|1", result)
        self.assertIn(r'\"quoted\"', result)

    def test_postprocessor_handles_white_and_numeric_colour_codes(self):
        result = translator_module.PostProcessor.process("§W text, §2 army§!", "")
        self.assertEqual(result, "§WText, §2Army§!")

    def test_postprocessor_does_not_hide_a_physical_newline_near_literal_escape(self):
        translated = "First line\n\\nSecond line"
        processed = translator_module.PostProcessor.process(translated, "")

        self.assertIn("\n", processed)
        issues = translator_module.validate_preview_translations(
            ["First line"], [processed]
        )
        self.assertIn("одну физическую строку", issues[0])

    def test_localization_value_parser_handles_optional_version_escapes_and_comments(self):
        line = r'KEY-NAME: "He said: \"hello\"" # a comment with "quotes"'
        match = translator_module.VALUE_RE.match(line)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "KEY-NAME: ")
        self.assertEqual(match.group(2), r'He said: \"hello\"')
        self.assertEqual(match.group(3), ' # a comment with "quotes"')
        self.assertIsNone(translator_module.VALUE_RE.match('bad key:0 "not a valid key"'))
        self.assertIsNone(translator_module.VALUE_RE.match('KEY:abc "not a numeric version"'))

    def test_preview_rejects_physical_newlines_and_unescaped_quotes(self):
        newline_issues = translator_module.validate_preview_translations(
            ["One line"], ["First line\nSecond line"]
        )
        quote_issues = translator_module.validate_preview_translations(
            ["One line"], ['Text with "unescaped" quotes']
        )

        self.assertIn("одну физическую строку", newline_issues[0])
        self.assertIn("экранировать", quote_issues[0])
        escaped_quote = r'Quote: \"translated\"'
        self.assertEqual(
            translator_module.validate_preview_translations([escaped_quote], [escaped_quote]),
            [],
        )


class FileProcessingTests(unittest.TestCase):
    def _run_processor(self, source, destination, translator, target_lang="pl", skip=False):
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        logs = []
        try:
            result = translator_module.process_file_sync(
                str(source), str(destination), translator,
                lambda *entry: logs.append(entry),
                skip_translated=skip, stop_event=threading.Event(), loop=loop,
                target_lang=target_lang,
            )
            return result, logs
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2)
            loop.close()

    @staticmethod
    def _translator(transform, source_lang="en"):
        class FakeTranslator:
            validation_report = types.SimpleNamespace(broken=[])

            def __init__(self):
                self.source_lang = source_lang

            async def translate_batch(
                self, texts, target_lang, keys=None, file_ctx="",
                line_progress_cb=None, stop_event=None
            ):
                return [transform(text) for text in texts]

        return FakeTranslator()

    def test_missing_header_is_added_and_bom_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "localisation" / "messages.yml"
            destination = Path(directory) / "out" / "messages_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text('KEY:0 "Hello"\n', encoding="utf-8-sig")

            result, _ = self._run_processor(
                source, destination, self._translator(lambda _: "Cześć")
            )

            self.assertEqual(result, 1)
            self.assertTrue(destination.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(
                destination.read_text(encoding="utf-8-sig"),
                'l_polish:\nKEY:0 "Cześć"\n',
            )

    def test_windows_line_endings_are_preserved_in_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_bytes(
                b"\xef\xbb\xbf" + 'l_english:\r\n KEY:0 "Hello"\r\n'.encode("utf-8")
            )

            result, _ = self._run_processor(
                source, destination, self._translator(lambda _: "Cześć")
            )

            self.assertEqual(result, 1)
            self.assertEqual(
                destination.read_bytes(),
                b"\xef\xbb\xbf" + 'l_polish:\r\n KEY:0 "Cześć"\r\n'.encode("utf-8"),
            )

    def test_multiple_language_blocks_keep_only_selected_source_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n KEY:0 "Hello"\nl_german:\n KEY:0 "Hallo"\n',
                encoding="utf-8-sig",
            )

            result, logs = self._run_processor(
                source, destination, self._translator(lambda text: "Cześć " + text)
            )

            self.assertEqual(result, 1)
            self.assertEqual(
                destination.read_text(encoding="utf-8-sig"),
                'l_polish:\n KEY:0 "Cześć Hello"\n',
            )
            self.assertTrue(any("несколько языковых баз" in entry[0] for entry in logs))

    def test_multiple_language_blocks_are_skipped_if_source_language_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n KEY:0 "Hello"\nl_german:\n KEY:0 "Hallo"\n',
                encoding="utf-8-sig",
            )

            result, logs = self._run_processor(
                source, destination, self._translator(lambda text: text, source_lang="ja")
            )

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(any(entry[1] == "error" for entry in logs))

    def test_processor_rejects_destination_without_internal_language_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events.yml"
            source.parent.mkdir(parents=True)
            source.write_text('l_english:\n KEY:0 "Hello"\n', encoding="utf-8-sig")

            result, logs = self._run_processor(
                source, destination, self._translator(lambda _: "Cześć")
            )

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(any("должно заканчиваться" in entry[0] for entry in logs))

    def test_physical_newline_from_translator_does_not_write_invalid_localization(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text('l_english:\n KEY:0 "Hello"\n', encoding="utf-8-sig")

            result, logs = self._run_processor(
                source, destination, self._translator(lambda _: "Two\nlines")
            )

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(any("переносы строк" in entry[0] for entry in logs))

    def test_invalid_localization_key_or_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n BAD KEY:0 "Hello"\n', encoding="utf-8-sig"
            )

            result, logs = self._run_processor(
                source, destination, self._translator(lambda text: text)
            )

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(any("формату локализации" in entry[0] for entry in logs))

    def test_single_language_header_mismatch_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_german.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_german:\n KEY:0 "Guten Tag"\n', encoding="utf-8-sig"
            )

            result, logs = self._run_processor(
                source, destination, self._translator(lambda text: "Cześć " + text)
            )

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(any(entry[1] == "error" for entry in logs))

    def test_trailing_non_comment_junk_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n KEY:0 "Hello" unexpected\n', encoding="utf-8-sig"
            )

            result, logs = self._run_processor(
                source, destination, self._translator(lambda text: "Cześć")
            )

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(any("формату локализации" in entry[0] for entry in logs))

    def test_invalid_existing_translation_is_retranslated_not_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n KEY:0 "Protect $STATE$"\n', encoding="utf-8-sig"
            )
            destination.write_text(
                'l_polish:\n KEY:0 "Chroń państwo"\n', encoding="utf-8-sig"
            )

            result, logs = self._run_processor(
                source, destination,
                self._translator(lambda _: "Chroń $STATE$"), skip=True,
            )

            self.assertEqual(result, 1)
            self.assertEqual(
                destination.read_text(encoding="utf-8-sig"),
                'l_polish:\n KEY:0 "Chroń $STATE$"\n',
            )
            self.assertTrue(any("отклонён" in entry[0] for entry in logs))

    def test_file_header_and_atomic_output_use_selected_language(self):
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory) / "source"
            destination_root = Path(directory) / "translated"
            source = source_root / "localisation" / "english" / "events_l_english.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                '''l_english:
 KEY:0 "Secure $STATE$"
''', encoding="utf-8-sig"
            )
            destination = Path(
                translator_module.rename_dst(str(source), str(source_root), str(destination_root), "pl")
            )

            class FakeTranslator:
                validation_report = types.SimpleNamespace(broken=[])

                async def translate_batch(
                    self, texts, target_lang, keys=None, file_ctx="",
                    line_progress_cb=None, stop_event=None
                ):
                    return ["Chroń $STATE$" for _ in texts]

            loop = asyncio.new_event_loop()
            loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
            loop_thread.start()
            try:
                result = translator_module.process_file_sync(
                    str(source), str(destination), FakeTranslator(), lambda *_: None,
                    skip_translated=False, stop_event=threading.Event(), loop=loop,
                    target_lang="pl",
                )
            finally:
                loop.call_soon_threadsafe(loop.stop)
                loop_thread.join(timeout=2)
                loop.close()

            self.assertEqual(result, 1)
            self.assertEqual(
                destination.read_text(encoding="utf-8-sig"),
                '''l_polish:
 KEY:0 "Chroń $STATE$"
''',
            )


class BrazilianPortugueseProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_429_raises_after_final_retry_instead_of_returning_source_text(self):
        class FakeResponse:
            status = 429

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        translator = object.__new__(translator_module.AsyncTranslator)
        translator.source_lang = "en"
        translator.session = FakeSession()
        fake_aiohttp = types.SimpleNamespace(ClientTimeout=lambda **kwargs: kwargs)
        with patch.object(translator_module, "aiohttp", fake_aiohttp, create=True):
            with self.assertRaisesRegex(RuntimeError, "rate-limited"):
                await translator_module.AsyncTranslator._google_async(
                    translator, "Hello", "ru", retries=1
                )

    async def test_google_api_gets_the_brazilian_portuguese_variant(self):
        captured = {}

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self, content_type=None):
                return [[("Olá",)]]

        class FakeSession:
            def get(self, url, params, timeout):
                captured.update(params)
                return FakeResponse()

        translator = object.__new__(translator_module.AsyncTranslator)
        translator.source_lang = "en"
        translator.session = FakeSession()
        fake_aiohttp = types.SimpleNamespace(ClientTimeout=lambda **kwargs: kwargs)
        with patch.object(translator_module, "aiohttp", fake_aiohttp, create=True):
            result = await translator_module.AsyncTranslator._google_async(
                translator, "Hello", "pt", retries=1
            )

        self.assertEqual(result, "Olá")
        self.assertEqual(captured["tl"], "pt-BR")


class MissingPlaceholderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_game_token_is_reported_not_moved_to_sentence_end(self):
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.engine = "Stub"
        translator.source_lang = "en"
        translator._semaphore = asyncio.Semaphore(1)
        translator.validation_report = translator_module.ValidationReport()

        class FakeCache:
            def get(self, *_args):
                return None

            def set(self, *_args):
                pass

        translator.cache = FakeCache()

        async def drop_placeholder(self, engine, protected, target, retries):
            return protected.replace("<<PH0>>", "")

        translator._call_engine = types.MethodType(drop_placeholder, translator)
        result = await translator_module.AsyncTranslator.translate_text_async(
            translator, "Protect $STATE$ now", "ru"
        )

        self.assertNotIn("$STATE$", result)
        self.assertEqual(len(translator.validation_report.broken), 1)
        self.assertIn("потерян", ", ".join(translator.validation_report.broken[0]["issues"]))


class JavaSimRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_java_sim_engine_dispatches_to_java_service(self):
        translator = object.__new__(translator_module.AsyncTranslator)

        class FakeJavaService:
            def translate_batch_java(self, texts, target_lang):
                return [f"java:{target_lang}:{text}" for text in texts]

        translator.java_service = FakeJavaService()
        result = await translator_module.AsyncTranslator._call_engine(
            translator, "JavaSim", "protected text", "pl", 3
        )

        self.assertEqual(result, "java:pl:protected text")
        self.assertEqual(
            translator_module.FALLBACK_CHAIN["JavaSim"], ["Google", "MyMemory"]
        )


class CacheFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_engine_cache_entry_is_reused(self):
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.engine = "Google"
        translator.source_lang = "en"
        looked_up = []

        class FakeCache:
            def get(self, text, source, target, engine):
                looked_up.append(engine)
                return "cached fallback" if engine == "MyMemory" else None

        translator.cache = FakeCache()
        result = await translator.translate_text_async("Hello", "ru")

        self.assertEqual(result, "cached fallback")
        self.assertEqual(looked_up, ["Google", "MyMemory"])

    async def test_invalid_cached_translation_is_ignored_and_replaced(self):
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.engine = "Google"
        translator.source_lang = "en"
        translator._semaphore = asyncio.Semaphore(1)
        translator.validation_report = translator_module.ValidationReport()

        class FakeCache:
            def __init__(self):
                self.saved = []

            def get(self, text, source, target, engine):
                return "без маркера" if engine == "Google" else None

            def set(self, *args):
                self.saved.append(args)

        translator.cache = FakeCache()

        async def translate_valid(self, engine, protected, target, retries):
            return "Защитите <<PH0>> сейчас"

        translator._call_engine = types.MethodType(translate_valid, translator)
        result = await translator_module.AsyncTranslator.translate_text_async(
            translator, "Protect $STATE$ now", "ru"
        )

        self.assertEqual(result, "Защитите $STATE$ сейчас")
        self.assertEqual(len(translator.cache.saved), 1)
        self.assertEqual(translator.cache.saved[0][1], result)

    async def test_all_provider_failures_raise_instead_of_saving_source_as_translation(self):
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.engine = "Google"
        translator.source_lang = "en"
        translator._semaphore = asyncio.Semaphore(1)
        translator.validation_report = translator_module.ValidationReport()
        attempted = []

        class FakeCache:
            def get(self, *_args):
                return None

        translator.cache = FakeCache()

        async def unavailable(self, engine, protected, target, retries):
            attempted.append(engine)
            raise RuntimeError("provider unavailable")

        translator._call_engine = types.MethodType(unavailable, translator)
        with self.assertRaises(translator_module.TranslationFailed):
            await translator_module.AsyncTranslator.translate_text_async(
                translator, "Hello world", "ru"
            )

        self.assertEqual(attempted, ["Google", "MyMemory"])


class TranslationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_the_active_batch_without_waiting_for_network_timeout(self):
        old_limit = translator_module.SEMAPHORE_LIMIT
        translator_module.SEMAPHORE_LIMIT = 3
        translator = object.__new__(translator_module.AsyncTranslator)
        started = asyncio.Event()

        async def slow_translation(
            self, text, target_lang="ru", retries=3, _file_ctx="", _key_ctx=""
        ):
            started.set()
            await asyncio.sleep(30)
            return text

        translator.translate_text_async = types.MethodType(slow_translation, translator)
        stop_event = threading.Event()
        try:
            batch = asyncio.create_task(
                translator.translate_batch(
                    ["one", "two", "three"], stop_event=stop_event
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            stop_event.set()

            with self.assertRaises(translator_module.TranslationCancelled):
                await asyncio.wait_for(batch, timeout=1)
        finally:
            translator_module.SEMAPHORE_LIMIT = old_limit

    async def test_batch_returns_results_in_original_order(self):
        old_limit = translator_module.SEMAPHORE_LIMIT
        translator_module.SEMAPHORE_LIMIT = 3
        translator = object.__new__(translator_module.AsyncTranslator)

        async def staggered_translation(
            self, text, target_lang="ru", retries=3, _file_ctx="", _key_ctx=""
        ):
            await asyncio.sleep({"first": 0.03, "second": 0.01, "third": 0.02}[text])
            return text.upper()

        translator.translate_text_async = types.MethodType(staggered_translation, translator)
        try:
            result = await translator.translate_batch(["first", "second", "third"])
            self.assertEqual(result, ["FIRST", "SECOND", "THIRD"])
        finally:
            translator_module.SEMAPHORE_LIMIT = old_limit


if __name__ == "__main__":
    unittest.main()
