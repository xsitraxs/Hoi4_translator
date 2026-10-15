import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# The service logic can be tested on headless builders where Python was built
# without Tk. Production still imports the real tkinter module normally.
from tests import tk_stub

tk_stub.install()

import hoi4translator as translator_module  # noqa: E402  (после установки заглушки)
from hoi4translator import pipeline as pipeline_module  # noqa: E402
from hoi4translator import report as report_module  # noqa: E402
from hoi4translator import settings as settings_module  # noqa: E402
from hoi4translator import storage as storage_module  # noqa: E402
from hoi4translator import translation as translation_module  # noqa: E402


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
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
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
        translator._deepl_client = None
        translator._deepl_lock = threading.Lock()
        fake_deepl = types.SimpleNamespace(DeepLClient=FakeDeepLClient)
        with patch.object(translation_module, "deepl", fake_deepl, create=True):
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
        translator._deepl_client = None
        translator._deepl_lock = threading.Lock()
        fake_deepl = types.SimpleNamespace(DeepLClient=FakeDeepLClient)
        with patch.object(translation_module, "deepl", fake_deepl, create=True):
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

    def test_output_path_collisions_are_rejected_before_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory) / "source"
            destination_root = Path(directory) / "translated"
            generic = source_root / "localisation" / "events.yml"
            english = source_root / "localisation" / "events_l_english.yml"
            outputs = [
                (
                    str(generic),
                    translator_module.rename_dst(
                        str(generic), str(source_root), str(destination_root), "pl"
                    ),
                ),
                (
                    str(english),
                    translator_module.rename_dst(
                        str(english), str(source_root), str(destination_root), "pl"
                    ),
                ),
            ]

            self.assertEqual(outputs[0][1], outputs[1][1])
            with self.assertRaisesRegex(ValueError, "Коллизия выходных файлов"):
                translator_module.validate_output_path_collisions(outputs)

            translator_module.validate_output_path_collisions(
                [(str(generic), outputs[0][1]), (str(english), str(destination_root / "other.yml"))]
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

            with patch.object(
                settings_module, "SETTINGS_FILE", str(settings_path)
            ), patch.object(
                settings_module, "LEGACY_SETTINGS_FILE", str(legacy_path)
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
        with patch.object(settings_module, "keyring", FakeKeyring()):
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

        with patch.object(settings_module, "keyring", FakePlaintextKeyring()):
            self.assertFalse(translator_module.store_api_key("secret"))


class TranslationCacheTests(unittest.TestCase):
    def test_cache_version_separates_new_entries_from_legacy_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = translator_module.TranslationCache(
                str(Path(directory) / "translations.sqlite3")
            )
            try:
                cache.set("Hello", "Привет", "en", "ru", "Google")
                self.assertEqual(cache.get("Hello", "en", "ru", "Google"), "Привет")

                with patch.object(
                    storage_module,
                    "CACHE_FORMAT_VERSION",
                    storage_module.CACHE_FORMAT_VERSION - 1,
                ):
                    self.assertIsNone(cache.get("Hello", "en", "ru", "Google"))
            finally:
                cache.close()


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
        result = translator_module.PostProcessor.process(original)

        self.assertIn("[(OBJECT ? TRUE_CASE : FALSE_CASE)]", result)
        self.assertIn("$$5", result)
        self.assertIn("£GFX_icon|1", result)
        self.assertIn(r'\"quoted\"', result)

    def test_postprocessor_preserves_colour_codes_and_neighbouring_case(self):
        translated = "§W text, §2 army§!"
        result = translator_module.PostProcessor.process(translated)
        self.assertEqual(result, translated)

    def test_postprocessor_uses_target_language_typography(self):
        french = translator_module.PostProcessor.process(
            'Il a dit "bonjour" !', target_lang="fr"
        )
        english = translator_module.PostProcessor.process(
            'He said "hello" !', target_lang="en"
        )

        self.assertEqual(french, "Il a dit «\u202fbonjour\u202f»\u202f!")
        self.assertEqual(english, "He said “hello”!")

    def test_colour_markers_must_remain_balanced_and_in_order(self):
        original = "§Ygold§! and §Rred§!"
        reordered = "§Rred§! and §Ygold§!"
        issues = translator_module.PlaceholderValidator.validate(original, reordered)
        unclosed = translator_module.PlaceholderValidator.validate("§Ygold§!", "§Ygold")
        malformed = translator_module.PlaceholderValidator.validate("value", "value § ")

        self.assertTrue(any("порядок цветовых маркеров" in issue for issue in issues))
        self.assertTrue(any("не закрыт" in issue for issue in unclosed))
        self.assertTrue(any("некорректный знак §" in issue for issue in malformed))

    def test_postprocessor_does_not_hide_a_physical_newline_near_literal_escape(self):
        translated = "First line\n\\nSecond line"
        processed = translator_module.PostProcessor.process(translated)

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
    def _run_processor(
        self, source, destination, translator, target_lang="pl", skip=False, preview_cb=None
    ):
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        logs = []
        try:
            result = translator_module.process_file_sync(
                str(source), str(destination), translator,
                lambda *entry: logs.append(entry),
                skip_translated=skip, stop_event=threading.Event(), loop=loop,
                target_lang=target_lang, preview_cb=preview_cb,
            )
            return result, logs
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2)
            loop.close()

    @staticmethod
    def _translator(transform, source_lang="en"):
        class FakeTranslator:
            validation_report = translator_module.ValidationReport()

            def __init__(self):
                self.source_lang = source_lang

            async def translate_batch(
                self, texts, target_lang, keys=None, file_ctx="",
                line_progress_cb=None, stop_event=None
            ):
                return [transform(text) for text in texts]

        return FakeTranslator()

    def test_duplicate_localization_keys_reject_the_file_before_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n KEY:0 "Hello"\n KEY:0 "Hi"\n', encoding="utf-8-sig"
            )
            translated = []

            result, logs = self._run_processor(
                source,
                destination,
                self._translator(lambda value: translated.append(value) or value),
            )

            self.assertIsNone(result)
            self.assertEqual(translated, [])
            self.assertFalse(destination.exists())
            self.assertTrue(any("повторяющиеся localization keys" in entry[0] for entry in logs))

    def test_preview_cancellation_is_not_reported_as_a_successful_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n KEY:0 "Hello"\n', encoding="utf-8-sig"
            )

            result, logs = self._run_processor(
                source,
                destination,
                self._translator(lambda _: "Cześć"),
                preview_cb=lambda *_: False,
            )

            self.assertIs(result, translator_module.FileProcessStatus.PREVIEW_CANCELLED)
            self.assertFalse(destination.exists())
            self.assertTrue(any("Предпросмотр отменён" in entry[0] for entry in logs))

    def test_approved_preview_must_confirm_its_write(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                'l_english:\n KEY:0 "Hello"\n', encoding="utf-8-sig"
            )

            def approve_preview(originals, translations, indexes, metadata, lines, path):
                for line_idx, translated, (prefix, suffix, ending) in zip(
                    indexes, translations, metadata
                ):
                    lines[line_idx] = f'{prefix}"{translated}"{suffix}{ending}'
                translator_module.atomic_write_lines(path, lines)
                return True

            result, _ = self._run_processor(
                source,
                destination,
                self._translator(lambda _: "Cześć"),
                preview_cb=approve_preview,
            )

            self.assertEqual(result, 1)
            self.assertEqual(
                destination.read_text(encoding="utf-8-sig"),
                'l_polish:\n KEY:0 "Cześć"\n',
            )

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
                b"\xef\xbb\xbf" + b'l_english:\r\n KEY:0 "Hello"\r\n'
            )

            result, _ = self._run_processor(
                source, destination, self._translator(lambda _: "Cześć")
            )

            self.assertEqual(result, 1)
            self.assertEqual(
                destination.read_bytes(),
                b"\xef\xbb\xbf" + 'l_polish:\r\n KEY:0 "Cześć"\r\n'.encode(),
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
                validation_report = translator_module.ValidationReport()

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
        with patch.object(translation_module, "aiohttp", fake_aiohttp, create=True), \
                self.assertRaisesRegex(RuntimeError, "rate-limited"):
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
        with patch.object(translation_module, "aiohttp", fake_aiohttp, create=True):
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
        translator.reuse_fallback_cache = False
        translator.fallback_notifier = None
        translator.fallback_counts = {}
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
        translator.source_lang = "en"
        calls = []

        class FakeJavaService:
            def translate_batch_java(self, texts, target_lang, source_lang=None):
                calls.append((list(texts), target_lang, source_lang))
                return [f"java:{target_lang}:{text}" for text in texts]

        translator.java_service = FakeJavaService()
        result = await translator_module.AsyncTranslator._call_engine(
            translator, "JavaSim", "protected text", "pl", 3
        )

        self.assertEqual(result, "java:pl:protected text")
        # JavaSim must honour the selected source language instead of sl=auto.
        self.assertEqual(calls, [(["protected text"], "pl", "en")])
        self.assertEqual(
            translator_module.FALLBACK_CHAIN["JavaSim"], ["Google", "MyMemory"]
        )


class CacheFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_engine_cache_entries_are_not_reused_by_default(self):
        # Switching from Google to DeepL must actually re-translate: silently
        # serving the old Google text defeats the point of switching engines.
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.engine = "Google"
        translator.source_lang = "en"
        translator.reuse_fallback_cache = False
        translator.fallback_notifier = None
        translator.fallback_counts = {}
        translator._semaphore = asyncio.Semaphore(1)
        translator.validation_report = translator_module.ValidationReport()
        looked_up = []

        class FakeCache:
            def get(self, text, source, target, engine):
                looked_up.append(engine)
                return "cached fallback" if engine == "MyMemory" else None

            def set(self, *_args):
                pass

        translator.cache = FakeCache()

        async def google_answer(self, engine, protected, target, retries):
            return "свежий перевод"

        translator._call_engine = types.MethodType(google_answer, translator)

        result = await translator.translate_text_async("Hello", "ru")

        self.assertEqual(result, "свежий перевод")
        self.assertEqual(looked_up, ["Google"])

    async def test_fallback_engine_cache_entry_is_reused_when_opted_in(self):
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.engine = "Google"
        translator.source_lang = "en"
        translator.reuse_fallback_cache = True
        translator.fallback_notifier = None
        translator.fallback_counts = {}
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
        translator.reuse_fallback_cache = False
        translator.fallback_notifier = None
        translator.fallback_counts = {}
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
        translator.reuse_fallback_cache = False
        translator.fallback_notifier = None
        translator.fallback_counts = {}
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
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.concurrency = 3
        started = asyncio.Event()

        async def slow_translation(
            self, text, target_lang="ru", retries=3, _file_ctx="", _key_ctx=""
        ):
            started.set()
            await asyncio.sleep(30)
            return text

        translator.translate_text_async = types.MethodType(slow_translation, translator)
        stop_event = threading.Event()
        batch = asyncio.create_task(
            translator.translate_batch(
                ["one", "two", "three"], stop_event=stop_event
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        stop_event.set()

        with self.assertRaises(translator_module.TranslationCancelled):
            await asyncio.wait_for(batch, timeout=1)

    async def test_batch_returns_results_in_original_order(self):
        translator = object.__new__(translator_module.AsyncTranslator)
        translator.concurrency = 3

        async def staggered_translation(
            self, text, target_lang="ru", retries=3, _file_ctx="", _key_ctx=""
        ):
            await asyncio.sleep({"first": 0.03, "second": 0.01, "third": 0.02}[text])
            return text.upper()

        translator.translate_text_async = types.MethodType(staggered_translation, translator)
        result = await translator.translate_batch(["first", "second", "third"])
        self.assertEqual(result, ["FIRST", "SECOND", "THIRD"])

# ---------------------------------------------------------------------------
# Общие помощники для сценариев обработки файлов
# ---------------------------------------------------------------------------

def run_processor(source, destination, translator, target_lang="pl", skip=False,
                  preview_cb=None, settle=0.0, broken_line_mode=None, report=None):
    """Run process_file_sync on a private event loop and return (result, logs).

    `settle` gives cancelled tasks time to unwind before the loop is closed.
    """
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    logs = []
    try:
        result = translator_module.process_file_sync(
            str(source), str(destination), translator,
            lambda *entry: logs.append(entry),
            skip_translated=skip, stop_event=threading.Event(), loop=loop,
            target_lang=target_lang, preview_cb=preview_cb,
            broken_line_mode=broken_line_mode
            if broken_line_mode is not None
            else pipeline_module.BrokenLineMode.REJECT_FILE,
            report=report,
        )
        if settle:
            time.sleep(settle)
        return result, logs
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()


def cached_translator(cache, transform=None, source_lang="en"):
    """Fake translator that owns a real cache, like AsyncTranslator does."""
    transform = transform or (lambda text: "Cześć " + text)

    class FakeTranslator:
        def __init__(self):
            self.source_lang = source_lang
            self.cache = cache
            self.validation_report = translator_module.ValidationReport()
            self.batches = []

        async def translate_batch(
            self, texts, target_lang, keys=None, file_ctx="",
            line_progress_cb=None, stop_event=None
        ):
            self.batches.append(list(texts))
            return [transform(text) for text in texts]

    return FakeTranslator()


class StaleTranslationTests(unittest.TestCase):
    """«Пропускать переведённое» обязано сверять исходный текст (H3)."""

    def _prepare(self, directory, source_text):
        source = Path(directory) / "source" / "events_l_english.yml"
        destination = Path(directory) / "out" / "events_l_polish.yml"
        source.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f'l_english:\n KEY:0 "{source_text}"\n', encoding="utf-8-sig")
        return source, destination

    def test_edited_source_line_is_retranslated_instead_of_reusing_old_value(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = translator_module.TranslationCache(Path(directory) / "cache.db")
            source, destination = self._prepare(directory, "Hello")
            try:
                first = cached_translator(cache)
                run_processor(source, destination, first)

                # Unchanged source: nothing to translate, the file is reused.
                second = cached_translator(cache)
                result, _ = run_processor(source, destination, second, skip=True)
                self.assertEqual(result, 0)
                self.assertEqual(second.batches, [])

                # The mod author edited the English line: the old Polish text
                # describes the previous source and must not survive.
                source.write_text(
                    'l_english:\n KEY:0 "Hello, world"\n', encoding="utf-8-sig"
                )
                third = cached_translator(cache)
                result, logs = run_processor(source, destination, third, skip=True)

                self.assertEqual(result, 1)
                self.assertEqual(third.batches, [["Hello, world"]])
                self.assertIn("Cześć Hello, world", destination.read_text(encoding="utf-8-sig"))
                self.assertTrue(any("Исходник изменился" in entry[0] for entry in logs))
            finally:
                cache.close()

    def test_legacy_file_is_kept_but_warned_and_then_tracked(self):
        # A file written before fingerprints existed cannot be verified, so it
        # is kept (it may be a hand-made translation) but the run says so and
        # records hashes so later source edits are detected.
        with tempfile.TemporaryDirectory() as directory:
            cache = translator_module.TranslationCache(Path(directory) / "cache.db")
            source, destination = self._prepare(directory, "Hello")
            destination.write_text('l_polish:\n KEY:0 "Witaj"\n', encoding="utf-8-sig")
            try:
                translator = cached_translator(cache)
                result, logs = run_processor(source, destination, translator, skip=True)

                self.assertEqual(result, 0)
                self.assertEqual(translator.batches, [])
                self.assertIn("Witaj", destination.read_text(encoding="utf-8-sig"))
                self.assertTrue(
                    any("нет контрольных сумм" in entry[0] for entry in logs)
                )

                # Fingerprints are recorded now, so an edited source is caught.
                source.write_text(
                    'l_english:\n KEY:0 "Hello, world"\n', encoding="utf-8-sig"
                )
                second = cached_translator(cache)
                result, _ = run_processor(source, destination, second, skip=True)

                self.assertEqual(result, 1)
                self.assertEqual(second.batches, [["Hello, world"]])
            finally:
                cache.close()


class FileTimeoutTests(unittest.TestCase):
    def test_timeout_scales_with_the_number_of_lines(self):
        self.assertEqual(translator_module.file_translation_timeout(1), 120.0)
        self.assertGreater(translator_module.file_translation_timeout(2000), 300)
        self.assertGreater(translator_module.file_translation_timeout(20000),
                           translator_module.file_translation_timeout(2000))

    def test_slow_file_is_abandoned_and_the_batch_is_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text('l_english:\n KEY:0 "Hello"\n', encoding="utf-8-sig")

            class SlowTranslator:
                validation_report = translator_module.ValidationReport()

                def __init__(self):
                    self.seen_stop = None
                    self.cancelled = False

                async def translate_batch(
                    self, texts, target_lang, keys=None, file_ctx="",
                    line_progress_cb=None, stop_event=None
                ):
                    self.seen_stop = stop_event
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        self.cancelled = True
                        raise
                    return list(texts)

            translator = SlowTranslator()
            original_timeout = pipeline_module.file_translation_timeout
            pipeline_module.file_translation_timeout = lambda count: 0.2
            try:
                result, logs = run_processor(source, destination, translator, settle=0.1)
            finally:
                pipeline_module.file_translation_timeout = original_timeout

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            # The abandoned coroutine must be told to stop, otherwise it keeps
            # running on the shared loop while the next file is processed.
            self.assertIsNotNone(translator.seen_stop)
            self.assertTrue(translator.seen_stop.is_set())
            self.assertTrue(any("Превышено время перевода" in entry[0] for entry in logs))

    def test_combined_stop_event_mirrors_both_sources(self):
        outer = threading.Event()
        combined = pipeline_module._CombinedStopEvent(outer)
        self.assertFalse(combined.is_set())
        outer.set()
        self.assertTrue(combined.is_set())

        other = pipeline_module._CombinedStopEvent(threading.Event())
        other.cancel()
        self.assertTrue(other.is_set())


class SourceEncodingTests(unittest.TestCase):
    def test_cp1251_source_is_translated_and_rewritten_as_utf8_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_bytes(
                'l_english:\n KEY:0 "Привет"\n'.encode("cp1251")
            )

            translator = cached_translator(None, transform=lambda text: "Cześć " + text)
            result, logs = run_processor(source, destination, translator)

            self.assertEqual(result, 1)
            self.assertEqual(translator.batches, [["Привет"]])
            self.assertTrue(destination.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("Cześć Привет", destination.read_text(encoding="utf-8-sig"))
            self.assertTrue(any("cp1251" in entry[0] for entry in logs))

    def test_unknown_bytes_never_raise_and_report_the_fallback_encoding(self):
        # A single-byte fallback keeps a batch alive instead of aborting it
        # with a bare UnicodeDecodeError in the middle of a long run.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.yml"
            path.write_bytes('KEY:0 "Привет"\n'.encode("cp1251"))
            warnings = []
            lines, encoding = translator_module.read_localisation_lines(
                str(path), lambda *entry: warnings.append(entry)
            )

            self.assertEqual(lines, ['KEY:0 "Привет"\n'])
            self.assertEqual(encoding, "cp1251")
            self.assertTrue(warnings and "cp1251" in warnings[0][0])


class LongTextSplittingTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(translator_module.split_long_text("Hello"), ["Hello"])

    def test_long_text_is_split_without_losing_characters(self):
        text = ("Secure the northern border and hold the line. " * 40).strip()
        chunks = translator_module.split_long_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= translator_module.MAX_QUERY_CHARS for chunk in chunks))

    def test_split_never_cuts_through_a_protection_marker(self):
        marker = "<<PH7>>"
        text = "word " * 100 + marker + " tail"
        chunks = translator_module.split_long_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(marker not in chunk or chunk.endswith(marker) or
                            marker in chunk for chunk in chunks))
        for chunk in chunks:
            for index in range(1, len(marker)):
                self.assertFalse(
                    chunk.endswith(marker[:index]) and len(chunk) > index,
                    f"chunk ends inside a marker: {chunk[-12:]!r}",
                )

    def test_split_prefers_a_sentence_boundary(self):
        text = "First sentence here. " + "x" * 500
        chunks = translator_module.split_long_text(text)
        self.assertTrue(chunks[0].endswith("First sentence here. "))
        self.assertEqual("".join(chunks), text)


class ProviderLanguageCodeTests(unittest.TestCase):
    def test_brazilian_portuguese_is_consistent_across_providers(self):
        codes = translator_module.provider_language_code
        self.assertEqual(codes("DeepL", "pt", target=True), "PT-BR")
        self.assertEqual(
            codes(translator_module.GOOGLE_ENDPOINT_ENGINE, "pt", target=True), "pt-BR"
        )
        # deep-translator rejects "pt-BR", so its Google wrapper gets "pt".
        self.assertEqual(codes("Google", "pt", target=True), "pt")
        self.assertEqual(codes("MyMemory", "pt", target=True), "pt-BR")

    def test_chinese_codes_follow_each_provider(self):
        codes = translator_module.provider_language_code
        self.assertEqual(codes("DeepL", "zh-CN", target=True), "ZH-HANS")
        self.assertEqual(codes("DeepL", "zh-CN", target=False), "ZH")
        self.assertEqual(codes("MyMemory", "zh-CN", target=True), "zh-CN")

    def test_synchronous_google_fallback_uses_a_supported_variant(self):
        captured = {}

        class FakeTranslator:
            def __init__(self, source, target):
                captured.update(source=source, target=target)

            def translate(self, text):
                return "ok"

        translator = object.__new__(translator_module.AsyncTranslator)
        translator.source_lang = "en"
        translator.engine = "Google"
        with patch.object(translation_module, "GoogleTranslator", FakeTranslator):
            result = translator_module.AsyncTranslator._sync_fallback(
                translator, "Hello", "pt", 1, "Google"
            )

        self.assertEqual(result, "ok")
        self.assertEqual(captured, {"source": "en", "target": "pt"})

    def test_synchronous_mymemory_fallback_uses_locales(self):
        captured = {}

        class FakeTranslator:
            def __init__(self, source, target):
                captured.update(source=source, target=target)

            def translate(self, text):
                return "ok"

        translator = object.__new__(translator_module.AsyncTranslator)
        translator.source_lang = "en"
        translator.engine = "MyMemory"
        with patch.object(translation_module, "MyMemoryTranslator", FakeTranslator):
            translator_module.AsyncTranslator._sync_fallback(
                translator, "Hello", "pt", 1, "MyMemory"
            )

        self.assertEqual(captured, {"source": "en-US", "target": "pt-BR"})


class JavaSimContractTests(unittest.TestCase):
    def test_outdated_jar_falls_back_to_the_two_argument_call(self):
        calls = []

        class OutdatedJar:
            def translate(self, text, target):
                calls.append((text, target))
                return "java:" + text

        result = translator_module.JavaTranslatorService._translate_one(
            OutdatedJar(), "Hello", "pl", "en"
        )
        self.assertEqual(result, "java:Hello")
        self.assertEqual(calls, [("Hello", "pl")])

    def test_current_jar_receives_the_selected_source_language(self):
        calls = []

        class CurrentJar:
            def translate(self, text, target, source):
                calls.append((text, target, source))
                return "java:" + text

        translator_module.JavaTranslatorService._translate_one(
            CurrentJar(), "Hello", "pl", "en"
        )
        self.assertEqual(calls, [("Hello", "pl", "en")])


class ValidationReportLimitTests(unittest.TestCase):
    def test_broken_rows_are_capped_but_still_counted(self):
        report = translator_module.ValidationReport()
        for index in range(700):
            report.record("file.yml", f"KEY_{index}", ["потерян: '$X$'"], False)

        self.assertEqual(len(report.broken), report.MAX_TRACKED_BROKEN)
        self.assertEqual(report.broken_count, 700)
        self.assertEqual(report.untracked_broken, 200)
        self.assertIn("битых: 700", report.summary())
        self.assertEqual(
            [entry["key"] for entry in report.broken_for_file("file.yml")][:2],
            ["KEY_0", "KEY_1"],
        )
        self.assertEqual(report.broken_for_file("other.yml"), [])


class SharedWriterTests(unittest.TestCase):
    def test_preview_result_writes_through_the_shared_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True)
            source.write_text('l_english:\n KEY:0 "Hello"\n', encoding="utf-8-sig")

            def approve_with_edit(originals, translations, indexes, metadata, lines, path):
                # The callback only approves values now; writing belongs to
                # process_file_sync so both paths cannot drift apart.
                return ["Cześć!"]

            result, _ = run_processor(
                source, destination,
                cached_translator(None, transform=lambda text: "Cześć"),
                preview_cb=approve_with_edit,
            )

            self.assertEqual(result, 1)
            self.assertEqual(
                destination.read_text(encoding="utf-8-sig"),
                'l_polish:\n KEY:0 "Cześć!"\n',
            )

    def test_zip_equal_refuses_silent_truncation(self):
        pairs = list(translator_module.zip_equal([1, 2, 3], "abc"))
        self.assertEqual(pairs, [(1, "a"), (2, "b"), (3, "c")])
        with self.assertRaises(ValueError):
            list(translator_module.zip_equal([1, 2, 3], "ab"))

    def test_build_output_lines_keeps_untranslated_rows(self):
        lines = translator_module.build_output_lines(
            ['l_russian:\n', ' A:0 "старое"\n', ' B:0 "старое"\n'],
            [2],
            ["новое"],
            [(' B:0 ', '', '\n')],
        )
        self.assertEqual(lines, ['l_russian:\n', ' A:0 "старое"\n', ' B:0 "новое"\n'])


class JavaJarResolutionTests(unittest.TestCase):
    def test_explicit_jar_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "custom.jar"
            jar.write_bytes(b"")
            self.assertEqual(
                translator_module.resolve_java_jar(str(jar), directory), str(jar)
            )

    def test_bundled_jar_is_found_next_to_the_script(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "hoi4_translator.jar").write_bytes(b"")
            self.assertEqual(
                translator_module.resolve_java_jar("", directory),
                os.path.join(directory, "hoi4_translator.jar"),
            )

    def test_missing_sources_are_reported_instead_of_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(translator_module.resolve_java_jar("", directory))

    @unittest.skipUnless(shutil.which("javac"), "javac is not installed")
    def test_jar_is_built_from_the_committed_source(self):
        # Guards the Java/Python contract: the JAR must expose the class that
        # JavaTranslatorService loads, and it must stay in sync with java/.
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(__file__).resolve().parent.parent
            output = os.path.join(directory, "hoi4_translator.jar")
            result = translator_module.build_java_jar(str(source_root), output)
            self.assertEqual(result, output)
            self.assertTrue(os.path.getsize(output) > 0)


class BrokenLineRecoveryTests(unittest.TestCase):
    """Одна битая строка не должна Goodbye whole file (per-line recovery)."""

    def _prepare(self, directory, source_text='l_english:\n KEY:0 "Hello $STATE$"\n'
                 ' OTHER:0 "Fine"\n'):
        source = Path(directory) / "source" / "events_l_english.yml"
        destination = Path(directory) / "out" / "events_l_polish.yml"
        source.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(source_text, encoding="utf-8-sig")
        return source, destination

    @staticmethod
    def _translator(transform, cache=None):
        class FakeTranslator:
            source_lang = "en"
            validation_report = translator_module.ValidationReport()
            batches = []

            def __init__(self):
                self.cache = cache

            async def translate_batch(
                self, texts, target_lang, keys=None, file_ctx="",
                line_progress_cb=None, stop_event=None
            ):
                self.batches.append(list(texts))
                return [transform(text) for text in texts]

        return FakeTranslator()

    def test_broken_line_blocks_the_file_by_default(self):
        # The engine dropped the $STATE$ marker: writing this file would make
        # the game print a broken string, so the safe mode refuses it.
        with tempfile.TemporaryDirectory() as directory:
            source, destination = self._prepare(directory)
            translator = self._translator(lambda text: text.replace("$STATE$", ""))

            result, logs = run_processor(source, destination, translator)

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(
                any("Файл не записан" in entry[0] for entry in logs), logs
            )

    def test_recovery_replaces_the_broken_line_with_the_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = self._prepare(directory)
            translator = self._translator(lambda text: text.replace("$STATE$", ""))

            result, logs = run_processor(
                source, destination, translator,
                broken_line_mode=pipeline_module.BrokenLineMode.KEEP_ORIGINAL,
            )

            self.assertEqual(result, 2)
            written = destination.read_text(encoding="utf-8-sig")
            # The line that could not be trusted keeps its English text…
            self.assertIn('KEY:0 "Hello $STATE$"', written)
            # …while the healthy line next to it is still translated.
            self.assertIn('OTHER:0 "Fine"', written)
            self.assertTrue(
                any("заменена оригиналом" in entry[0] for entry in logs), logs
            )

    def test_recovered_line_is_translated_again_on_the_next_run(self):
        # Without this the English text written as a repair would be treated as
        # a finished translation forever.
        with tempfile.TemporaryDirectory() as directory:
            cache = translator_module.TranslationCache(Path(directory) / "cache.db")
            source, destination = self._prepare(directory)
            try:
                first = self._translator(
                    lambda text: text.replace("$STATE$", ""), cache
                )
                run_processor(
                    source, destination, first,
                    broken_line_mode=pipeline_module.BrokenLineMode.KEEP_ORIGINAL,
                )
                self.assertEqual(first.batches, [["Hello $STATE$", "Fine"]])

                # Second run, "Пропускать переведённое": only the repaired key
                # may be re-translated.
                second = self._translator(lambda text: "PL " + text, cache)
                result, _ = run_processor(
                    source, destination, second, skip=True,
                    broken_line_mode=pipeline_module.BrokenLineMode.KEEP_ORIGINAL,
                )
                self.assertEqual(result, 1)
                self.assertEqual(second.batches, [["Hello $STATE$"]])
                self.assertIn(
                    "PL Hello $STATE$", destination.read_text(encoding="utf-8-sig")
                )
            finally:
                cache.close()

    def test_healthy_lines_are_not_touched_by_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = self._prepare(directory)
            translator = self._translator(lambda text: "PL " + text)

            result, logs = run_processor(
                source, destination, translator,
                broken_line_mode=pipeline_module.BrokenLineMode.KEEP_ORIGINAL,
            )

            self.assertEqual(result, 2)
            self.assertFalse(
                any("заменена оригиналом" in entry[0] for entry in logs), logs
            )

    def test_preview_that_changes_the_row_count_is_rejected_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = self._prepare(directory)
            translator = self._translator(lambda text: "PL " + text)

            result, logs = run_processor(
                source, destination, translator, preview_cb=lambda *args: ["only one"],
            )

            self.assertIsNone(result)
            self.assertFalse(destination.exists())
            self.assertTrue(
                any("Количество строк изменилось" in entry[0] for entry in logs), logs
            )

    def test_mode_labels_round_trip(self):
        for mode, label in pipeline_module.BROKEN_LINE_MODES:
            self.assertEqual(
                pipeline_module.broken_line_mode_from_label(label), mode
            )
        self.assertEqual(
            pipeline_module.broken_line_mode_from_label("нет такого"),
            pipeline_module.BrokenLineMode.REJECT_FILE,
        )


class RunReportTests(unittest.TestCase):
    """The run report is a file, not a line lost in the GUI log."""

    def test_report_is_written_next_to_the_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True, exist_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('l_english:\n KEY:0 "Hello $STATE$"\n', encoding="utf-8-sig")

            translator = BrokenLineRecoveryTests._translator(
                lambda text: text.replace("$STATE$", "")
            )
            report = report_module.RunReport("Google", "en", "pl", "src", "dst")
            run_processor(
                source, destination, translator,
                broken_line_mode=pipeline_module.BrokenLineMode.KEEP_ORIGINAL,
                report=report,
            )
            report.set_fallbacks({"DeepL→Google": 3})
            written = report.write(str(Path(directory) / "out"))

            self.assertEqual(len(written), 2)
            json_path = next(path for path in written if path.endswith(".json"))
            csv_path = next(path for path in written if path.endswith(".csv"))
            payload = json.loads(Path(json_path).read_text(encoding="utf-8"))

            self.assertEqual(payload["engine"], "Google")
            self.assertEqual(payload["summary"]["files"], 1)
            self.assertEqual(payload["summary"]["written"], 1)
            self.assertEqual(payload["summary"]["recovered"], 1)
            self.assertEqual(payload["summary"]["fallbacks"], 3)
            self.assertEqual(payload["fallbacks"], {"DeepL→Google": 3})
            self.assertEqual(
                [row["key"] for row in payload["recovered_rows"]], ["KEY:0"]
            )
            self.assertTrue(any("$STATE$" in issue for issue in
                                payload["recovered_rows"][0]["issues"]))
            self.assertTrue(
                Path(json_path).name.startswith("hoi4_translator_report_")
            )

            csv_text = Path(csv_path).read_text(encoding="utf-8")
            self.assertIn("state;file;key;issues", csv_text)
            self.assertIn("recovered;events_l_english.yml;KEY:0;", csv_text)

    def test_rejected_file_appears_in_the_report_with_a_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "events_l_english.yml"
            destination = Path(directory) / "out" / "events_l_polish.yml"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('l_english:\n KEY:0 "Hello $STATE$"\n', encoding="utf-8-sig")

            translator = BrokenLineRecoveryTests._translator(
                lambda text: text.replace("$STATE$", "")
            )
            report = report_module.RunReport("Google", "en", "pl", "src", "dst")
            result, _ = run_processor(source, destination, translator, report=report)

            self.assertIsNone(result)
            summary = report.summary()
            self.assertEqual(summary["files"], 1)
            self.assertEqual(summary["written"], 0)
            self.assertEqual(summary["rejected"], 1)
            self.assertIn("перевод нарушает формат", report.files[0]["message"])

    def test_row_list_is_capped_but_still_counted(self):
        report = report_module.RunReport("Google", "en", "pl", "src", "dst")
        limit = report_module.MAX_TRACKED_ROWS
        for index in range(limit + 7):
            report.record_broken("events.yml", f"KEY{index}", ["плохой §"], False)
        payload = report.to_dict()
        self.assertEqual(len(payload["broken_rows"]), limit)
        self.assertEqual(payload["hidden_rows"], 7)

    def test_report_survives_an_unwritable_directory(self):
        report = report_module.RunReport("Google", "en", "pl", "src", "dst")
        report.record_file("a.yml", "b.yml", "written", translated=1)
        # The run must not fail just because the report could not be written,
        # and the report must not create folders of its own.
        self.assertEqual(report.write(""), [])
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                report.write(str(Path(directory) / "missing")), []
            )


class OptionalDependencyTests(unittest.TestCase):
    """A missing engine must be explained, not silently dropped from the list."""

    def test_deepl_capability_matches_the_optional_import(self):
        import importlib

        config = importlib.import_module("hoi4translator.config")
        try:
            importlib.import_module("deepl")
            available = True
        except ImportError:
            available = False

        self.assertEqual(config.DEEPL_OK, available)
        self.assertEqual(bool(config.DEEPL_IMPORT_ERROR), not available)
        if available:
            self.assertIs(config.deepl, importlib.import_module("deepl"))
        else:
            self.assertIsNone(config.deepl)


class EntryPointTests(unittest.TestCase):
    """The CLI switches let CI smoke-test the built EXE without a display."""

    def test_help_and_version_exit_before_tk_is_used(self):
        self.assertEqual(translator_module.main(["--help"]), 0)
        self.assertEqual(translator_module.main(["--version"]), 0)

    def test_unknown_argument_is_rejected(self):
        self.assertEqual(translator_module.main(["--nope"]), 2)

    def test_version_constant_is_declared(self):
        self.assertRegex(translator_module.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
