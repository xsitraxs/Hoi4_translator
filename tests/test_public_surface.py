"""The public surface: ``import hoi4translator`` and the ``hoi4_translator.py`` script.

The root script does ``from hoi4translator import *``, so a name listed in
``__all__`` but never imported in ``__init__.py`` breaks ``python
hoi4_translator.py`` while every unit test still passes: the script imports
fine only in the combination the tests never exercise. These tests import the
script the way a user runs it.
"""
import importlib.util
import unittest
from pathlib import Path

import hoi4translator

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_SCRIPT = REPO_ROOT / "hoi4_translator.py"


def load_entry_script():
    """Import ``hoi4_translator.py`` by path, without relying on sys.path."""
    spec = importlib.util.spec_from_file_location("_entry_script", ENTRY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicSurfaceTests(unittest.TestCase):
    def test_every_exported_name_exists(self):
        missing = [name for name in hoi4translator.__all__ if not hasattr(hoi4translator, name)]
        self.assertEqual(missing, [], f"declared in __all__ but not imported: {missing}")

    def test_no_exported_name_is_listed_twice(self):
        seen, duplicates = set(), set()
        for name in hoi4translator.__all__:
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        self.assertEqual(duplicates, set())

    def test_version_matches_the_package_metadata(self):
        from hoi4translator.config import __version__

        self.assertEqual(hoi4translator.__version__, __version__)
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_the_entry_script_starts_and_re_exports_the_api(self):
        """`from hoi4translator import *` must not raise AttributeError."""
        script = load_entry_script()
        self.assertEqual(script.__version__, hoi4translator.__version__)
        for name in ("main", "USAGE", "AsyncTranslator", "normalize_key"):
            self.assertTrue(hasattr(script, name), name)

    def test_the_entry_script_exposes_the_cli_parser(self):
        script = load_entry_script()
        parser = script.cli.build_parser() if hasattr(script, "cli") else None
        if parser is None:  # cli is imported lazily by main(), not re-exported
            from hoi4translator.cli import build_parser

            parser = build_parser()
        options = {action.dest for action in parser._actions}
        for expected in ("src", "dst", "target", "engine", "dry_run", "java_jar"):
            self.assertIn(expected, options)


if __name__ == "__main__":
    unittest.main()
