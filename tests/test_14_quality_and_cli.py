"""Glossary, vanilla reuse and the headless CLI.

These cover the three features that make 1.4.0: a term list that engines cannot
override, reuse of the game's own translations, and a run without a window.
"""
import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hoi4translator import cli, text as text_module
from hoi4translator.glossary import Glossary, GlossaryError
from hoi4translator.vanilla import VanillaIndex


def write(path, content, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)
    return str(path)


class GlossaryFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def _file(self, name, content):
        return write(Path(self.directory.name) / name, content)

    def test_tab_separated_terms_are_read(self):
        path = self._file("terms.tsv", "Division\tДивизия\nSoviet Union\tСоветский Союз\n")
        glossary = Glossary.load(path)
        self.assertEqual(len(glossary), 2)
        self.assertEqual(glossary.lookup("division"), "Дивизия")

    def test_semicolon_and_equals_delimiters_are_accepted(self):
        for name, content in (
            ("semi.csv", "Division;Дивизия\n"),
            ("equals.txt", "Division=Дивизия\n"),
        ):
            glossary = Glossary.load(self._file(name, content))
            self.assertEqual(len(glossary), 1, name)

    def test_json_glossary_is_accepted(self):
        path = self._file("terms.json", '{"Division": "Дивизия"}')
        self.assertEqual(Glossary.load(path).lookup("Division"), "Дивизия")

    def test_comments_and_blank_lines_are_ignored(self):
        path = self._file("c.tsv", "# комментарий\n\nDivision\tДивизия\n")
        self.assertEqual(len(Glossary.load(path)), 1)

    def test_a_line_without_a_delimiter_is_an_error_not_a_silent_skip(self):
        path = self._file("bad.tsv", "Division\tДивизия\nмусор без разделителя\n")
        with self.assertRaises(GlossaryError):
            Glossary.load(path)

    def test_a_glossary_without_pairs_is_rejected(self):
        path = self._file("empty.tsv", "# только комментарии\n")
        with self.assertRaises(GlossaryError):
            Glossary.load(path)

    def test_missing_file_is_reported(self):
        with self.assertRaises(GlossaryError):
            Glossary.load(str(Path(self.directory.name) / "нет.tsv"))


class GlossaryMaskingTests(unittest.TestCase):
    def setUp(self):
        self.glossary = Glossary([("Division", "Дивизия"), ("Soviet Union", "Советский Союз")])

    def test_term_is_replaced_by_its_pinned_translation(self):
        protected, tokens = text_module.protect_placeholders(
            "Soviet Union sends a Division", self.glossary
        )
        # Both terms leave the text: nothing is left for an engine to translate.
        self.assertNotIn("Division", protected)
        self.assertEqual(
            text_module.restore_placeholders(protected, tokens),
            "Советский Союз sends a Дивизия",
        )

    def test_longest_term_wins(self):
        glossary = Glossary([("Soviet", "Советский"), ("Soviet Union", "СССР")])
        protected, tokens = text_module.protect_placeholders("Soviet Union", glossary)
        self.assertEqual(text_module.restore_placeholders(protected, tokens), "СССР")

    def test_matching_is_case_insensitive_and_respects_word_boundaries(self):
        protected, tokens = text_module.protect_placeholders(
            "division DIVISION subdivisions", self.glossary
        )
        restored = text_module.restore_placeholders(protected, tokens)
        self.assertEqual(restored, "Дивизия Дивизия subdivisions")

    def test_markers_and_terms_coexist(self):
        protected, tokens = text_module.protect_placeholders(
            "The $STATE$ Division of §YGermany§!", self.glossary
        )
        restored = text_module.restore_placeholders(protected, tokens)
        self.assertEqual(restored, "The $STATE$ Дивизия of §YGermany§!")

    def test_without_a_glossary_behaviour_is_unchanged(self):
        protected, tokens = text_module.protect_placeholders("A Division")
        self.assertEqual(text_module.restore_placeholders(protected, tokens), "A Division")


class TranslatorGlossaryTests(unittest.IsolatedAsyncioTestCase):
    def _translator(self, glossary, cache=None):
        from hoi4translator.translation import AsyncTranslator

        translator = AsyncTranslator.__new__(AsyncTranslator)
        translator.engine = "Google"
        translator.source_lang = "en"
        translator.glossary = glossary
        translator.fallback_counts = {}
        translator.reuse_fallback_cache = False
        translator._semaphore = asyncio.Semaphore(1)
        translator.validation_report = _NullReport()
        translator.cache = cache or _NullCache()
        translator.fallback_notifier = None
        return translator

    async def test_engine_never_sees_a_glossary_term(self):
        seen = []

        async def fake_engine(engine, protected, target, retries):
            seen.append(protected)
            return protected.replace("stays", "остаётся")

        translator = self._translator(Glossary([("Division", "Дивизия")]))
        translator._call_engine = fake_engine
        with patch("hoi4translator.translation.PostProcessor.process", lambda value, lang: value):
            result = await translator.translate_text_async("A Division stays", "ru")

        self.assertEqual(result, "A Дивизия остаётся")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("Division", seen[0])

    async def test_a_value_that_is_only_a_term_needs_no_engine(self):
        called = []

        async def fake_engine(*args, **kwargs):
            called.append(args)
            return "должно быть пропущено"

        translator = self._translator(Glossary([("Division", "Дивизия")]))
        translator._call_engine = fake_engine
        result = await translator.translate_text_async("Division", "ru")

        self.assertEqual(result, "Дивизия")
        self.assertEqual(called, [])


class VanillaIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "localisation"
        write(
            self.root / "english" / "units_l_english.yml",
            'l_english:\n UNIT_1:0 "Infantry Division"\n UNIT_2:0 "Armored Division"\n'
            ' UNIT_3:0 "Only In English"\n',
        )
        write(
            self.root / "russian" / "units_l_russian.yml",
            'l_russian:\n UNIT_1:0 "Пехотная дивизия"\n UNIT_2:0 "Танковая дивизия"\n',
        )

    def test_official_translation_is_found_by_value(self):
        index = VanillaIndex.build(str(self.root), "en", "ru")
        self.assertEqual(index.lookup("Infantry Division"), "Пехотная дивизия")
        self.assertIsNone(index.lookup("Something else"))

    def test_keys_missing_in_the_target_language_are_not_indexed(self):
        index = VanillaIndex.build(str(self.root), "en", "ru")
        self.assertIsNone(index.lookup("Only In English"))

    def test_a_missing_pari_of_languages_yields_an_empty_index(self):
        index = VanillaIndex.build(str(self.root), "en", "de")
        self.assertEqual(len(index), 0)
        self.assertIsNone(index.lookup("Infantry Division"))

    def test_conflicting_translations_keep_the_first_and_count_the_rest(self):
        write(
            self.root / "english" / "zmore_l_english.yml",
            'l_english:\n UNIT_9:0 "Infantry Division"\n',
        )
        write(
            self.root / "russian" / "zmore_l_russian.yml",
            'l_russian:\n UNIT_9:0 "Другая дивизия"\n',
        )
        index = VanillaIndex.build(str(self.root), "en", "ru")
        self.assertEqual(index.lookup("Infantry Division"), "Пехотная дивизия")
        self.assertEqual(index.conflicts, 1)


class CliEndToEndTests(unittest.TestCase):
    """The whole CLI path with the engine stubbed: files, report, exit code."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        # src is the mod root and dst is another mod root, as documented: the
        # relative structure is preserved and the language folder is replaced.
        self.src = base / "mod"
        self.dst = base / "out"
        write(self.src / "localisation" / "english" / "mod_l_english.yml",
              'l_english:\n KEY_1:0 "Hello $STATE$"\n KEY_2:0 "Goodbye"\n')

    def _run(self, *extra):
        class FakeTranslator:
            def __init__(self, *args, **kwargs):
                self.source_lang = "en"
                self.cache = None
                self.validation_report = _NullTranslatorReport()
                self.batches = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def translate_batch(self, texts, target, keys=None, file_ctx="",
                                      line_progress_cb=None, stop_event=None):
                self.batches.append(list(texts))
                return [f"RU {value}" for value in texts]

        with patch.object(cli, "_translator_factory", return_value=FakeTranslator):
            return cli.main([
                "--src", str(self.src), "--dst", str(self.dst),
                "--target", "ru", *extra,
            ])

    def test_files_and_report_are_written(self):
        self.assertEqual(self._run(), 0)
        output = self.dst / "localisation" / "russian" / "mod_l_russian.yml"
        self.assertTrue(output.exists(), "файл перевода не создан")
        content = output.read_text(encoding="utf-8-sig")
        self.assertTrue(content.startswith("l_russian:"))
        self.assertIn('"RU Hello $STATE$"', content)

        reports = list(self.dst.glob("hoi4_translator_report_*.json"))
        self.assertEqual(len(reports), 1, "отчёт JSON не создан")

    def test_no_report_flag_skips_the_report(self):
        self.assertEqual(self._run("--no-report"), 0)
        self.assertEqual(list(self.dst.glob("hoi4_translator_report_*")), [])

    def test_quiet_keeps_the_summary_line(self):
        self.assertEqual(self._run("--quiet"), 0)


class AuditRegressionTests(unittest.TestCase):
    """Defects found by the pre-release audit; each one shipped-worthy."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_vanilla_matches_keys_that_differ_only_by_revision_number(self):
        # The game's own files are full of KEY:0 / KEY:3 for the same key.
        root = Path(self.directory.name)
        (root / "english").mkdir()
        (root / "russian").mkdir()
        (root / "english" / "units_l_english.yml").write_text(
            'l_english:\n UNIT_1:0 "Infantry Division"\n', encoding="utf-8-sig"
        )
        (root / "russian" / "units_l_russian.yml").write_text(
            'l_russian:\n UNIT_1:3 "Пехотная дивизия"\n', encoding="utf-8-sig"
        )
        index = VanillaIndex.build(str(root), "en", "ru")
        self.assertEqual(index.lookup("Infantry Division"), "Пехотная дивизия")

    def test_report_summary_counts_reused_lines(self):
        from hoi4translator.report import RunReport

        report = RunReport("Google", "en", "ru", "src", "dst")
        report.record_file("a.yml", "b.yml", "written", translated=3, reused=7)
        self.assertEqual(report.summary()["reused"], 7)

    def test_cli_rejects_deepl_without_a_key_instead_of_falling_back(self):
        base = Path(self.directory.name)
        write(base / "mod" / "a_l_english.yml", 'l_english:\n A:0 "one"\n')
        with patch.dict(os.environ, {"DEEPL_API_KEY": ""}, clear=False):
            code = cli.main([
                "--src", str(base / "mod"), "--dst", str(base / "out"),
                "--target", "ru", "--engine", "DeepL",
            ])
        self.assertEqual(code, 2)
        self.assertFalse((base / "out").exists())

    def test_cli_accepts_a_jar_path_for_javasim(self):
        parser = cli.build_parser()
        args = parser.parse_args([
            "--src", "a", "--dst", "b", "--engine", "JavaSim", "--java-jar", "x.jar",
        ])
        self.assertEqual(args.java_jar, "x.jar")

    def test_dry_run_does_not_ignore_a_broken_glossary(self):
        base = Path(self.directory.name)
        write(base / "mod" / "a_l_english.yml", 'l_english:\n A:0 "one"\n')
        bad = write(base / "bad.tsv", "мусор без разделителя\n")
        code = cli.main([
            "--src", str(base / "mod"), "--dst", str(base / "out"),
            "--target", "ru", "--dry-run", "--glossary", bad,
        ])
        self.assertEqual(code, 2)

    def test_glossary_term_with_regex_characters_is_matched_literally(self):
        glossary = Glossary([("C++", "Си плюс плюс"), ("US$", "Доллар")])
        protected, tokens = text_module.protect_placeholders(
            "C++ and US$ spend", glossary
        )
        self.assertEqual(
            text_module.restore_placeholders(protected, tokens),
            "Си плюс плюс and Доллар spend",
        )


class VanillaPipelineTests(unittest.TestCase):
    """The official translation must reach the file without an engine call."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.src = write(
            base / "mod" / "english" / "units_l_english.yml",
            'l_english:\n UNIT_1:0 "Infantry Division"\n UNIT_2:0 "Mod Only Line"\n',
        )
        self.dst = str(base / "out" / "russian" / "units_l_russian.yml")
        Path(self.dst).parent.mkdir(parents=True, exist_ok=True)

        self.vanilla_root = base / "vanilla"
        write(self.vanilla_root / "english" / "units_l_english.yml",
              'l_english:\n UNIT_1:0 "Infantry Division"\n')
        write(self.vanilla_root / "russian" / "units_l_russian.yml",
              'l_russian:\n UNIT_1:0 "Пехотная дивизия"\n')

    def test_official_line_is_reused_and_the_engine_sees_only_the_rest(self):
        from hoi4translator.pipeline import process_file_sync

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        index = VanillaIndex.build(str(self.vanilla_root), "en", "ru")
        translator = _RecordingTranslator()

        try:
            result = process_file_sync(
                self.src, self.dst, translator, lambda *a, **k: None,
                False, threading.Event(), loop,
                target_lang="ru", vanilla=index,
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

        self.assertIsInstance(result, int)
        # Only the line missing from vanilla went to the engine.
        self.assertEqual(translator.batches, [["Mod Only Line"]])
        content = Path(self.dst).read_text(encoding="utf-8-sig")
        self.assertIn('"Пехотная дивизия"', content)
        self.assertIn('"ПЕРЕВОД: Mod Only Line"', content)


class _NullTranslatorReport:
    def broken_for_file(self, _path):
        return []

    def record(self, *args, **kwargs):
        pass


class _RecordingTranslator:
    source_lang = "en"

    def __init__(self):
        self.batches = []
        self.validation_report = _NullTranslatorReport()
        self.cache = None

    async def translate_batch(self, texts, target_lang, keys=None, file_ctx="",
                              line_progress_cb=None, stop_event=None):
        self.batches.append(list(texts))
        return [f"ПЕРЕВОД: {value}" for value in texts]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.src = base / "mod" / "localisation" / "english"
        self.dst = base / "out" / "localisation" / "russian"
        write(self.src / "mod_l_english.yml", 'l_english:\n KEY_1:0 "Hello $STATE$"\n')

    def test_missing_source_folder_is_a_usage_error(self):
        self.assertEqual(cli.main(["--src", "/нет/такой", "--dst", str(self.dst)]), 2)

    def test_unknown_target_language_is_rejected(self):
        code = cli.main(["--src", str(self.src), "--dst", str(self.dst), "--target", "xx"])
        self.assertEqual(code, 2)

    def test_broken_glossary_is_reported_before_any_translation(self):
        bad = write(Path(self.directory.name) / "bad.tsv", "строка без разделителя\n")
        code = cli.main([
            "--src", str(self.src), "--dst", str(self.dst), "--glossary", bad,
        ])
        self.assertEqual(code, 2)

    def test_folder_without_yml_files_is_a_usage_error(self):
        empty = Path(self.directory.name) / "empty"
        empty.mkdir()
        self.assertEqual(cli.main(["--src", str(empty), "--dst", str(self.dst)]), 2)

    def test_version_is_printed_without_translating(self):
        with self.assertRaises(SystemExit):
            cli.main(["--version"])


class _NullReport:
    def record(self, *args, **kwargs):
        pass

    def broken_for_file(self, _path):
        return []


class _NullCache:
    def get(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()


class InspectTests(unittest.TestCase):
    """--dry-run exists so a mod can be checked without the game or the network."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def _file(self, name, content):
        return write(Path(self.directory.name) / name, content)

    def test_a_clean_file_has_no_problems(self):
        from hoi4translator.inspect import inspect_file

        path = self._file("ok_l_english.yml", 'l_english:\n KEY:0 "Hello $STATE$"\n')
        inspection = inspect_file(path)
        self.assertEqual(inspection.keys, 1)
        self.assertFalse(inspection.has_problems)

    def test_malformed_lines_are_counted_with_numbers(self):
        from hoi4translator.inspect import inspect_file

        path = self._file("bad_l_english.yml", 'l_english:\n KEY:0 "ok"\n broken line: "no key"\n')
        inspection = inspect_file(path)
        self.assertEqual(inspection.malformed, [3])
        self.assertTrue(inspection.has_problems)

    def test_duplicate_keys_are_reported(self):
        from hoi4translator.inspect import inspect_file

        path = self._file("dup_l_english.yml", 'l_english:\n A:0 "one"\n A:0 "two"\n')
        inspection = inspect_file(path)
        self.assertEqual(inspection.duplicates, {"A": [2, 3]})

    def test_unclosed_colour_marker_is_reported(self):
        from hoi4translator.inspect import inspect_file

        path = self._file("color_l_english.yml", 'l_english:\n A:0 "§Yunclosed"\n')
        inspection = inspect_file(path)
        self.assertTrue(inspection.broken_markers)
        self.assertTrue(inspection.has_problems)

    def test_already_translated_lines_are_counted(self):
        from hoi4translator.inspect import inspect_file

        source = self._file("a_l_english.yml", 'l_english:\n A:0 "one"\n B:0 "two"\n')
        target = self._file("a_l_russian.yml", 'l_russian:\n A:0 "раз"\n')
        self.assertEqual(inspect_file(source, target).translated, 1)

    def test_unreadable_file_is_reported_not_raised(self):
        from hoi4translator.inspect import inspect_file

        inspection = inspect_file(str(Path(self.directory.name) / "нет.yml"))
        self.assertTrue(inspection.error)

    def test_dry_run_returns_zero_for_a_clean_mod(self):
        base = Path(self.directory.name)
        write(base / "mod" / "a_l_english.yml", 'l_english:\n A:0 "one"\n')
        code = cli.main([
            "--src", str(base / "mod"), "--dst", str(base / "out"),
            "--target", "ru", "--dry-run",
        ])
        self.assertEqual(code, 0)

    def test_dry_run_returns_one_for_a_broken_mod(self):
        base = Path(self.directory.name)
        write(base / "mod" / "a_l_english.yml", 'l_english:\n broken line: "x"\n')
        code = cli.main([
            "--src", str(base / "mod"), "--dst", str(base / "out"),
            "--target", "ru", "--dry-run",
        ])
        self.assertEqual(code, 1)

    def test_dry_run_writes_nothing(self):
        base = Path(self.directory.name)
        write(base / "mod" / "a_l_english.yml", 'l_english:\n A:0 "one"\n')
        cli.main([
            "--src", str(base / "mod"), "--dst", str(base / "out"),
            "--target", "ru", "--dry-run",
        ])
        self.assertFalse((base / "out").exists())
