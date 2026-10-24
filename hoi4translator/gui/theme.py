"""The palette, fonts and the CustomTkinter colour theme.

The amber theme file (``amber.json``) defines how the *widgets* look; this
module defines the colours that a widget cannot express on its own: log tags,
status messages, the zebra striping of the preview and the accent shades used
for the buttons that must stand out. ``amber.json`` still carries a light
variant of every widget colour, but the app never asks for it.

There is one theme, deliberately. A light variant existed and caused nothing
but trouble: the palette had to be threaded through every widget, and a switch
restyled ~80 CustomTkinter widgets inside one Tk callback, which froze the
window while it happened. Dark only means a widget's colour is fixed when it is
built, and no code path ever has to repaint anything.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import customtkinter as ctk

FONT_FAMILY = "Segoe UI"
FONT_MONO = "Consolas"


@dataclass(frozen=True)
class Palette:
    """Colours that are not part of the widget theme."""

    name: str          # режим CustomTkinter, всегда dark
    label: str
    bg: str
    panel: str
    panel_alt: str
    input_bg: str
    border: str
    fg: str
    fg_muted: str
    fg_subtle: str
    accent: str
    accent_hover: str
    on_accent: str     # текст на акцентной кнопке
    success: str
    warning: str
    error: str
    info: str
    log_bg: str
    log_fg: str
    row_alt: str


DARK = Palette(
    name="dark",
    label="Тёмная",
    bg="#16181B",
    panel="#1E2023",
    panel_alt="#26292D",
    input_bg="#1A1C1F",
    border="#2E3237",
    fg="#ECEEF1",
    fg_muted="#A9AFB8",
    fg_subtle="#7A818B",
    accent="#F5A524",
    accent_hover="#FFBE45",
    on_accent="#16181B",
    success="#5BD47C",
    warning="#F5A524",
    error="#FF6B6B",
    info="#6FC3FF",
    log_bg="#15171A",
    log_fg="#D8DBE0",
    row_alt="#202327",
)

PALETTE = DARK
PALETTES: Dict[str, Palette] = {DARK.name: DARK}
DEFAULT_THEME = DARK.name

# Log tags: every message colour in one place.
LOG_TAGS = ("error", "success", "info", "warning", "muted")


def resolve(name: Optional[str] = None) -> Palette:
    """Return the palette. The argument is accepted and ignored.

    Settings written by older versions still carry ``"theme": "light"``; the
    app now has one theme, so a saved name must not be able to change anything.
    """
    return PALETTE


def log_tag_colors(palette: Palette) -> Dict[str, str]:
    return {
        "error": palette.error,
        "success": palette.success,
        "info": palette.info,
        "warning": palette.warning,
        "muted": palette.fg_muted,
    }


def theme_file() -> str:
    """Path of the amber widget theme shipped with the package."""
    return str(Path(__file__).with_name("amber.json"))


def install_theme() -> None:
    """Load the amber widget theme once per process."""
    ctk.set_default_color_theme(theme_file())


def color(role: str) -> str:
    """Return the colour for a palette role.

    One theme means a plain string is correct: nothing ever re-resolves it.
    """
    if role not in Palette.__dataclass_fields__:
        raise KeyError(f"Нет такого цвета в палитре: {role}")
    return getattr(PALETTE, role)


def apply(root, palette: Palette = PALETTE) -> None:
    """Fix the appearance mode and paint the window background."""
    ctk.set_appearance_mode(palette.name)
    root.configure(fg_color=palette.bg)


def font(size: int = 13, weight: Optional[str] = None, mono: bool = False) -> "ctk.CTkFont":
    """A font that follows the theme's own family."""
    return ctk.CTkFont(
        family=FONT_MONO if mono else FONT_FAMILY, size=size, weight=weight
    )
