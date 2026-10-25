"""Lines from real mods that the parser used to reject.

Every case here was found by running ``--dry-run`` over 34 localisation files
taken from two large public mods (Kaiserreich, Millennium Dawn) plus vanilla
``traits_l_english.yml``: 89 715 keys, of which 16 lines in three files did not
parse. Both shapes below are legal and appear in the game's own files, so a
translator that rejects them silently drops whole files.
"""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from hoi4translator import cli, pipeline as pipeline_module
from hoi4translator.inspect import inspect_file
from hoi4translator.localisation import split_value_line
from hoi4translator.text import PostProcessor, protect_placeholders, restore_placeholders
from hoi4translator.validation import PlaceholderValidator, ValidationReport

# vanilla traits_l_english.yml: quotes inside the value, never escaped
TRAITS_LINE = ' loading_drill_master_desc: "§L"Shoot them again, they\'re not dead yet!"§!"'
# Millennium Dawn, MD_focus_USA_l_english.yml: a doubled section sign
LAW_LINE = (' USA_econ_event.22.d: "Conspiracy in violation of Section 1 '
            'of the Sherman Antitrust Act (15 U.S.C. §§ 1)."')
# Kaiserreich, nsb_ideas_l_english.yml: a raw quote right before the colour end
IDEAS_LINE = (
    ' fleet_in_being_academy_spirit_tt: "New Admirals: §G50% chance§! to begin '
    'with §Y"Gunnery Expert"§! trait."'
)


class RealWorldLineTests(unittest.TestCase):
    def test_raw_quotes_inside_a_value_stay_in_the_value(self):
        prefix, value, suffix = split_value_line(TRAITS_LINE)
        self.assertEqual(prefix, " loading_drill_master_desc: ")
        self.assertEqual(value, '§L"Shoot them again, they\'re not dead yet!"§!')
        self.assertEqual(suffix, "")

    def test_quotes_wrapped_in_colour_codes_do_not_end_the_value(self):
        _, value, _ = split_value_line(IDEAS_LINE)
        self.assertEqual(
            value,
            'New Admirals: §G50% chance§! to begin with §Y"Gunnery Expert"§! trait.',
        )

    def test_a_comment_with_quotes_is_not_part_of_the_value(self):
        parts = split_value_line(' KEY:0 "value"  # the "old" text')
        self.assertEqual(parts[1], "value")
        self.assertEqual(parts[2], '  # the "old" text')

    def test_escaped_quotes_still_work(self):
        self.assertEqual(split_value_line(r' KEY:7 "a \"b\" c"')[1], r'a \"b\" c')

    def test_an_unterminated_value_is_still_malformed(self):
        self.assertIsNone(split_value_line(' KEY:9 "never closed'))
        self.assertIsNone(split_value_line(' KEY:9 "value" trailing junk'))

    def test_a_doubled_section_sign_is_not_a_broken_colour_code(self):
        self.assertEqual(PlaceholderValidator.validate(LAW_LINE, LAW_LINE), [])

    def test_a_real_broken_marker_is_still_reported(self):
        self.assertTrue(PlaceholderValidator.validate("stray § in text", "stray § in text"))
        self.assertTrue(PlaceholderValidator.validate("§Ynever closed", "§Ynever closed"))

    def test_the_inspector_reads_all_three_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "real_l_english.yml"
            path.write_text(
                "l_english:\n" + TRAITS_LINE + "\n" + LAW_LINE + "\n" + IDEAS_LINE + "\n",
                encoding="utf-8-sig",
            )
            inspection = inspect_file(str(path))
        self.assertEqual(inspection.malformed, [])
        self.assertEqual(inspection.broken_markers, [])
        self.assertEqual(inspection.keys, 3)


class DynamicColourTests(unittest.TestCase):
    """`§[expression]` is a colour the game computes, not a broken marker."""

    def test_a_dynamic_colour_is_one_placeholder(self):
        value = "§[IRE_loc_sf_ideology]Sinn Féin§! are in government"
        self.assertEqual(
            [ph for ph in PlaceholderValidator.extract_placeholders(value) if ph.startswith("§")],
            ["§[IRE_loc_sf_ideology]", "§!"],
        )

    def test_a_closed_dynamic_colour_is_valid(self):
        value = "The §[IRE_loc_an_ideology]Parliamentarian§! wing"
        self.assertEqual(PlaceholderValidator.validate(value, value), [])

    def test_a_dynamic_colour_left_open_is_still_reported(self):
        value = "This will §[IRE_loc_UCF_eco_improve]improve"
        self.assertTrue(PlaceholderValidator.validate(value, value))

    def test_the_inspector_does_not_block_a_file_that_uses_dynamic_colours(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ire_l_english.yml"
            path.write_text(
                "l_english:\n A: \"§[IRE_loc_sf_ideology]Sinn Féin§! are in government\"\n",
                encoding="utf-8-sig",
            )
            inspection = inspect_file(str(path))
        self.assertFalse(inspection.has_blocking_problems)
        self.assertFalse(inspection.has_notes)


class _FakeTranslator:
    """Stands in for AsyncTranslator, including its protection of game tokens.

    Protection and typography live in AsyncTranslator, so a fake that just
    prefixes the text would hand the pipeline raw `§` markers and raw quotes and
    test a code path no engine ever produces. Mirrors translation.py: protect,
    "translate", restore, post-process.
    """

    def __init__(self):
        self.batches = []
        self.validation_report = ValidationReport()

    async def translate_batch(self, texts, target_lang, keys=None, file_ctx="",
                              line_progress_cb=None, stop_event=None):
        self.batches.append(list(texts))
        results = []
        for raw in texts:
            protected, tokens = protect_placeholders(raw)
            restored = restore_placeholders("ПЕРЕВОД: " + protected, tokens)
            results.append(PostProcessor.process(restored, target_lang))
        return results


def run_processor(source, destination, translator, logs=None, target_lang="ru"):
    """Write one file on a private event loop; returns (result, logs)."""
    logs = logs if logs is not None else []
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        result = pipeline_module.process_file_sync(
            str(source), str(destination), translator,
            lambda *entry: logs.append(entry),
            skip_translated=False, stop_event=threading.Event(), loop=loop,
            target_lang=target_lang,
            broken_line_mode=pipeline_module.BrokenLineMode.REJECT_FILE,
            report=None,
        )
        return result, logs
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


class RealWorldPipelineTests(unittest.TestCase):
    """A file full of raw quotes must be translated, not skipped as broken."""

    def test_the_file_is_written_and_the_markup_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "in" / "real_l_english.yml"
            destination = Path(directory) / "out" / "real_l_russian.yml"
            source.parent.mkdir(parents=True)
            source.write_text(
                "l_english:\n" + TRAITS_LINE + "\n" + LAW_LINE + "\n",
                encoding="utf-8-sig",
            )
            translator = _FakeTranslator()
            result, _ = run_processor(source, destination, translator)
            written = destination.read_text(encoding="utf-8-sig")

        # The writer returns the number of keys it wrote, or a failure status.
        self.assertEqual(result, 2)
        # Both values reached the engine: the file was not skipped as broken.
        self.assertEqual(sum(len(batch) for batch in translator.batches), 2)
        self.assertIn("ПЕРЕВОД: §L«Shoot them again, they're not dead yet!»§!", written)
        self.assertEqual(written.count("§L"), 1)
        self.assertEqual(written.count("§!"), 1)      # §L…§!
        self.assertIn("§§", written)
        for line in written.splitlines()[1:]:
            if line.strip():
                self.assertIsNotNone(split_value_line(line), line)


class SourceMarkupTests(unittest.TestCase):
    """The mod's own broken markup must not cost the file."""

    SOURCE = (
        "l_english:\n"
        ' UKR_uncooperative_active: "§R"\n'
        ' QIE_desc: "§L$QIE_body$ end"\n'
        ' OK: "Fine"\n'
    )

    def _run(self, translator, source_text=SOURCE):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "in" / "kr_l_english.yml"
        destination = Path(directory.name) / "out" / "kr_l_russian.yml"
        source.parent.mkdir(parents=True)
        source.write_text(source_text, encoding="utf-8-sig")
        result, logs = run_processor(source, destination, translator)
        written = destination.read_text(encoding="utf-8-sig") if destination.exists() else ""
        return result, written, logs

    def test_a_broken_source_marker_is_written_as_it_is(self):
        result, written, logs = self._run(_FakeTranslator())
        self.assertEqual(result, 3)
        self.assertIn('UKR_uncooperative_active: "ПЕРЕВОД: §R"', written)
        self.assertIn("§L$QIE_body$ end", written)
        self.assertTrue(any("проблема в исходнике" in entry[0] for entry in logs))

    def test_markup_the_translation_broke_still_blocks_the_file(self):
        class Careless(_FakeTranslator):
            async def translate_batch(self, texts, target_lang, **kwargs):
                return [text.replace("§!", "") for text in texts]

        result, written, logs = self._run(Careless(), 'l_english:\n A: "§Rred§!"\n')
        self.assertIsNot(result, 1)
        self.assertEqual(written, "")
        self.assertTrue(any("Файл не записан" in entry[0] for entry in logs))


class DryRunNotesTests(unittest.TestCase):
    """`--dry-run` exits 0 when the only findings are the mod's own markup."""

    def _mod(self, line):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "localisation" / "english"
        source.mkdir(parents=True)
        (source / "a_l_english.yml").write_text("l_english:\n" + line + "\n", encoding="utf-8-sig")
        return directory.name

    def test_source_markup_notes_do_not_fail_the_check(self):
        mod = self._mod(' A: "§R"')
        code = cli.main(["--src", mod, "--dst", tempfile.mkdtemp(), "--dry-run", "--quiet"])
        self.assertEqual(code, 0)

    def test_a_line_that_does_not_parse_still_fails_the_check(self):
        mod = self._mod(' A: "unterminated')
        code = cli.main(["--src", mod, "--dst", tempfile.mkdtemp(), "--dry-run", "--quiet"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
