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
                'l_german:\n KEY_ONE:0 "Guten Tag"\n KEY_TWO:0 "你好"\n EMPTY:0 ""\n',
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


class FileProcessingTests(unittest.TestCase):
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
