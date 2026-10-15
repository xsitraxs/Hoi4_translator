"""GUI guarantees that do not need a display.

The window cannot be rendered in CI, but its construction and the theme switch
can be executed against the tkinter stand-in: that catches a broken layout, a
missing widget or a style that was never defined — the failures that are easy
to introduce and impossible to notice without a screen.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import tk_stub

tk_stub.install()

from hoi4translator.gui import theme  # noqa: E402  (после установки заглушки)
from hoi4translator.gui.app import App  # noqa: E402
from hoi4translator import settings as settings_module  # noqa: E402


class PaletteTests(unittest.TestCase):
    def test_both_palettes_are_complete_and_self_consistent(self):
        for name, palette in theme.PALETTES.items():
            self.assertEqual(palette.name, name)
            for field_name in palette.__dataclass_fields__:
                value = getattr(palette, field_name)
                if field_name in {"name", "label"}:
                    continue
                self.assertIsInstance(value, str, f"{name}.{field_name}")
                self.assertTrue(value, f"{name}.{field_name} пустой")

    def test_unknown_theme_name_falls_back_to_the_default(self):
        self.assertEqual(theme.resolve("").name, theme.DEFAULT_THEME)
        self.assertEqual(theme.resolve("нет такой темы").name, theme.DEFAULT_THEME)
        self.assertEqual(theme.resolve("LIGHT").name, "light")

    def test_opposite_always_leaves_the_current_theme(self):
        for name in theme.PALETTES:
            self.assertNotEqual(theme.opposite(name), name)
            self.assertIn(theme.opposite(name), theme.PALETTES)

    def test_log_tags_cover_every_tag_the_gui_uses(self):
        for palette in theme.PALETTES.values():
            colours = theme.log_tag_colors(palette)
            self.assertEqual(set(colours), set(theme.LOG_TAGS))
            self.assertTrue(all(colours.values()))


class GuiBuildTests(unittest.TestCase):
    """Build the window and switch the theme, with settings in a temp file."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.settings_file = str(Path(self._directory.name) / "settings.json")
        patcher = patch.object(settings_module, "SETTINGS_FILE", self.settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._directory.cleanup)

    def _app(self):
        app = App()
        self.addCleanup(app.destroy)
        return app

    def test_window_builds_with_every_control(self):
        app = self._app()

        for attribute in (
            "src_var", "dst_var", "engine_cb", "target_lang_cb", "sem_lbl",
            "java_jar_var", "api_entry", "verify_btn", "api_status_lbl",
            "broken_line_var", "report_info_var", "progress", "progress_lines",
            "progress_file_lbl", "progress_lines_lbl", "log_widget",
            "start_btn", "stop_btn", "theme_btn", "status_var", "speed_var",
        ):
            self.assertTrue(hasattr(app, attribute), f"нет атрибута {attribute}")

    def test_theme_switch_changes_palette_and_is_remembered(self):
        app = self._app()
        self.assertEqual(app._theme_name, theme.DEFAULT_THEME)

        app._toggle_theme()
        other = theme.opposite(theme.DEFAULT_THEME)
        self.assertEqual(app._theme_name, other)
        self.assertEqual(app._palette.name, other)
        # The button advertises the theme you get by clicking it.
        self.assertIn(
            theme.resolve(theme.opposite(app._theme_name)).label,
            app.theme_btn.kwargs.get("text", ""),
        )
        self.assertEqual(settings_module.load_settings().get("theme"), other)

        app._toggle_theme()
        self.assertEqual(app._theme_name, theme.DEFAULT_THEME)
        self.assertEqual(
            settings_module.load_settings().get("theme"), theme.DEFAULT_THEME
        )

    def test_saved_theme_is_restored_on_the_next_start(self):
        app = self._app()
        app._toggle_theme()
        chosen = app._theme_name
        app.destroy()

        restored = self._app()
        self.assertEqual(restored._theme_name, chosen)
        self.assertEqual(restored._palette.name, chosen)

    def test_log_tags_are_recoloured_after_a_switch(self):
        app = self._app()
        dark = dict(theme.log_tag_colors(theme.resolve("dark")))
        light = dict(theme.log_tag_colors(theme.resolve("light")))
        self.assertNotEqual(dark["error"], light["error"])

        app._toggle_theme()
        self.assertEqual(
            app.log_widget.kwargs.get("background"), app._palette.log_bg
        )

    def test_report_helper_still_works_after_a_restyle(self):
        from hoi4translator.report import RunReport

        app = self._app()
        app._toggle_theme()
        report = RunReport("Google", "en", "ru", "src", self._directory.name)
        report.record_file("a.yml", "b.yml", "written", translated=2)
        self.assertTrue(app._write_run_report(report, self._directory.name))
        self.assertTrue(
            all(os.path.exists(path) for path in app._last_report_files)
        )


if __name__ == "__main__":
    unittest.main()
