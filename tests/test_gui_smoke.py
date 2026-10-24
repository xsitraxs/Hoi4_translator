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
            "start_btn", "stop_btn", "theme_btn", "status_var", "speed_var",
        ):
            self.assertTrue(hasattr(app, attribute), f"нет атрибута {attribute}")

    def test_widget_theme_is_loaded_before_the_widgets(self):
        tk_stub.CALLS.clear()
        self._app()
        self.assertEqual(ctk.state["theme"], theme.theme_file())

    def test_theme_switch_changes_appearance_mode_and_is_remembered(self):
        app = self._app()
        self.assertEqual(app._theme_name, theme.DEFAULT_THEME)
        self.assertEqual(ctk.get_appearance_mode(), theme.DEFAULT_THEME)

        app._toggle_theme()
        other = theme.opposite(theme.DEFAULT_THEME)
        self.assertEqual(app._theme_name, other)
        self.assertEqual(ctk.get_appearance_mode(), other)
        # The button advertises the theme you get by clicking it.
        self.assertIn(
            theme.resolve(theme.opposite(app._theme_name)).label,
            app.theme_btn.kwargs.get("text", ""),
        )
        self.assertEqual(settings_module.load_settings().get("theme"), other)

        app._toggle_theme()
        self.assertEqual(ctk.get_appearance_mode(), theme.DEFAULT_THEME)
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
        self.assertEqual(ctk.get_appearance_mode(), chosen)

    def test_log_tags_are_recoloured_after_a_switch(self):
        app = self._app()
        dark = theme.log_tag_colors(theme.resolve("dark"))
        light = theme.log_tag_colors(theme.resolve("light"))
        self.assertNotEqual(dark["error"], light["error"])

        app._toggle_theme()
        self.assertEqual(
            app.log_widget.kwargs.get("fg_color"), app._palette.log_bg
        )

    def test_progress_bars_take_fractions_not_maximums(self):
        app = self._app()
        self.assertEqual(app._fraction(0, 0), 0.0)
        self.assertEqual(app._fraction(3, 4), 0.75)
        self.assertEqual(app._fraction(9, 4), 1.0)  # перевыполнение не ломает шкалу
        app._update_line_progress(2, 4)
        self.assertEqual(app.progress_lines.kwargs["value"], 0.5)

    def test_widget_colours_follow_the_theme_instead_of_freezing(self):
        """A colour passed as one string is frozen at construction.

        That is how the header and the bottom buttons stayed dark after a
        switch to the light theme: the widget kept the old palette's colour.
        CustomTkinter resolves a ``(light, dark)`` tuple on every switch, so
        every colour a widget is built with must be such a pair.
        """
        tk_stub.CREATED.clear()
        app = self._app()
        self.assertTrue(app is not None and tk_stub.CREATED, "виджеты не собраны")

        colour_options = (
            "fg_color", "text_color", "hover_color", "border_color",
            "progress_color", "button_color", "button_hover_color",
            "checkmark_color", "placeholder_text_color", "dropdown_fg_color",
            "dropdown_text_color", "label_text_color",
        )
        frozen = []
        for widget, kwargs in tk_stub.CREATED:
            for option in colour_options:
                value = kwargs.get(option)
                if value is None or isinstance(value, tuple):
                    continue
                if value == "transparent":
                    continue
                # The log is a Tk text widget: it is repainted by
                # _refresh_log_colors because Tk has no appearance modes.
                if widget == "CTkTextbox":
                    continue
                frozen.append((widget, option, value))
        self.assertEqual(
            frozen, [],
            f"цвета заданы строкой и не переключатся с темой: {frozen}",
        )

    def test_every_palette_role_can_be_used_as_a_theme_pair(self):
        for role in theme.Palette.__dataclass_fields__:
            if role in {"name", "label"}:
                continue
            pair = theme.color(role)
            self.assertEqual(pair, (getattr(theme.LIGHT, role), getattr(theme.DARK, role)))
        with self.assertRaises(KeyError):
            theme.color("нет_такого_цвета")

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
            palette=app._palette,
        )
        self.addCleanup(window.destroy)

        self.assertEqual(window._palette, app._palette)
        self.assertEqual(len(window._entries), 2)
        self.assertEqual(window._entries[0].get(), "Привет")


if __name__ == "__main__":
    unittest.main()
