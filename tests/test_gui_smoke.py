"""GUI guarantees that do not need a display.

The window cannot be rendered in CI, but its construction, the theme switch and
the preview can be executed against the tkinter/CustomTkinter stand-in. That
catches a broken layout, a missing widget or an option a widget does not have —
the failures that are easy to introduce in GUI code and impossible to notice
without a screen.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import tk_stub

tk_stub.install()

import customtkinter as ctk  # noqa: E402  (после установки заглушки)

from hoi4translator import settings as settings_module  # noqa: E402
from hoi4translator.gui import theme  # noqa: E402
from hoi4translator.gui.app import App  # noqa: E402
from hoi4translator.gui.preview import PreviewWindow  # noqa: E402


class PaletteTests(unittest.TestCase):
    def test_the_single_palette_is_complete(self):
        palette = theme.PALETTE
        self.assertEqual(theme.PALETTES, {palette.name: palette})
        for field_name in palette.__dataclass_fields__:
            value = getattr(palette, field_name)
            if field_name in {"name", "label"}:
                continue
            self.assertIsInstance(value, str, field_name)
            self.assertTrue(value, f"{field_name} пустой")

    def test_resolve_ignores_a_theme_saved_by_an_older_version(self):
        # Settings written before the light theme was removed still contain
        # "theme": "light"; it must not be able to change anything.
        for name in ("", "light", "dark", "нет такой темы", None):
            self.assertIs(theme.resolve(name), theme.PALETTE)

    def test_every_role_resolves_to_its_colour(self):
        for role in theme.Palette.__dataclass_fields__:
            if role in {"name", "label"}:
                continue
            self.assertEqual(theme.color(role), getattr(theme.PALETTE, role))
        with self.assertRaises(KeyError):
            theme.color("нет_такого_цвета")

    def test_log_tags_cover_every_tag_the_gui_uses(self):
        colours = theme.log_tag_colors(theme.PALETTE)
        self.assertEqual(set(colours), set(theme.LOG_TAGS))
        self.assertTrue(all(colours.values()))


class ThemeFileTests(unittest.TestCase):
    """amber.json is what the widgets actually read; a typo there is silent."""

    REQUIRED = (
        "CTk", "CTkToplevel", "CTkFrame", "CTkButton", "CTkLabel", "CTkEntry",
        "CTkCheckBox", "CTkProgressBar", "CTkSlider", "CTkComboBox",
        "CTkTextbox", "CTkScrollableFrame", "CTkScrollbar", "CTkFont",
        "DropdownMenu",
    )

    def test_theme_file_describes_every_widget(self):
        path = Path(theme.theme_file())
        self.assertTrue(path.is_file(), path)
        data = json.loads(path.read_text(encoding="utf-8"))
        for widget in self.REQUIRED:
            self.assertIn(widget, data, f"нет секции {widget}")

    def test_every_colour_is_a_light_dark_pair(self):
        data = json.loads(Path(theme.theme_file()).read_text(encoding="utf-8"))
        for widget, options in data.items():
            if widget == "CTkFont":
                continue
            for option, value in options.items():
                if not isinstance(value, list):
                    continue  # corner_radius и «transparent» — не цвета
                self.assertEqual(
                    len(value), 2, f"{widget}.{option}: ожидалась пара [light, dark]"
                )
                for colour in value:
                    self.assertTrue(
                        colour.startswith(("#", "gray")),
                        f"{widget}.{option}: подозрительный цвет {colour}",
                    )

    def test_accent_is_amber_in_both_modes(self):
        data = json.loads(Path(theme.theme_file()).read_text(encoding="utf-8"))
        accent = data["CTkProgressBar"]["progress_color"]
        self.assertTrue(all(colour.startswith("#") for colour in accent))
        # Янтарный: красный и зелёный заметно выше синего.
        for colour in accent:
            red = int(colour[1:3], 16)
            green = int(colour[3:5], 16)
            blue = int(colour[5:7], 16)
            self.assertGreater(red, blue)
            self.assertGreater(green, blue)


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
            "start_btn", "stop_btn", "status_var", "speed_var",
            "glossary_var", "vanilla_var", "browse_glossary", "browse_vanilla",
        ):
            self.assertTrue(hasattr(app, attribute), f"нет атрибута {attribute}")

    def test_widget_theme_is_loaded_before_the_widgets(self):
        tk_stub.CALLS.clear()
        self._app()
        self.assertEqual(ctk.state["theme"], theme.theme_file())

    def test_appearance_mode_is_fixed_to_the_single_theme(self):
        for saved in (None, "light", "dark"):
            if saved is not None:
                settings_module.save_settings({"theme": saved})
            app = self._app()
            self.assertEqual(ctk.get_appearance_mode(), theme.DEFAULT_THEME)
            self.assertIs(app._palette, theme.PALETTE)
            app.destroy()

    def test_the_theme_switch_is_gone(self):
        tk_stub.CREATED.clear()
        app = self._app()

        # No widget may advertise a theme, and no attribute may hold one.
        labels = [kwargs.get("text", "") for _, kwargs in tk_stub.CREATED]
        self.assertFalse(
            [text for text in labels if "Светлая" in text or "Тёмная" in text],
            "в окне осталась кнопка переключения темы",
        )
        self.assertNotIn("theme_btn", vars(app))
        # Methods are class attributes, so hasattr here is meaningful.
        for method in ("_toggle_theme", "_update_theme_button", "_paint_background",
                       "_save_theme_only", "_set_window_alpha"):
            self.assertFalse(hasattr(App, method), f"остался метод {method}")

    def test_saved_settings_do_not_carry_a_theme(self):
        app = self._app()
        app._save_current_settings()
        self.assertNotIn("theme", settings_module.load_settings())

    def test_log_colours_match_the_palette(self):
        app = self._app()
        self.assertEqual(app.log_widget.kwargs.get("fg_color"), theme.PALETTE.log_bg)
        self.assertEqual(app.log_widget.kwargs.get("text_color"), theme.PALETTE.log_fg)

    def test_progress_bars_take_fractions_not_maximums(self):
        app = self._app()
        self.assertEqual(app._fraction(0, 0), 0.0)
        self.assertEqual(app._fraction(3, 4), 0.75)
        self.assertEqual(app._fraction(9, 4), 1.0)  # перевыполнение не ломает шкалу
        app._update_line_progress(2, 4)
        self.assertEqual(app.progress_lines.kwargs["value"], 0.5)

    def test_report_helper_still_works_after_a_restyle(self):
        from hoi4translator.report import RunReport

        app = self._app()
        report = RunReport("Google", "en", "ru", "src", self._directory.name)
        report.record_file("a.yml", "b.yml", "written", translated=2)
        self.assertTrue(app._write_run_report(report, self._directory.name))
        self.assertTrue(
            all(os.path.exists(path) for path in app._last_report_files)
        )


class PreviewWindowTests(unittest.TestCase):
    def test_preview_builds_with_the_parent_theme(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_file = str(Path(directory.name) / "settings.json")
        patcher = patch.object(settings_module, "SETTINGS_FILE", settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)

        app = App()
        self.addCleanup(app.destroy)
        window = PreviewWindow(
            app,
            "events_l_english.yml",
            ["Hello", "World $STATE$"],
            ["Привет", "Мир $STATE$"],
            on_accept=lambda _edited: None,
            on_cancel=lambda: None,
        )
        self.addCleanup(window.destroy)

        self.assertEqual(window._palette, theme.PALETTE)
        self.assertEqual(len(window._entries), 2)
        self.assertEqual(window._entries[0].get(), "Привет")


if __name__ == "__main__":
    unittest.main()
