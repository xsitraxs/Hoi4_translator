"""Palettes, fonts and the CustomTkinter colour theme.

The amber theme file (``amber.json``) defines how the *widgets* look; this
module defines the colours that a widget cannot express on its own: log tags,
status messages, the zebra striping of the preview and the accent shades used
for the buttons that must stand out. Two palettes — dark and light — are
selected with ``customtkinter.set_appearance_mode``, so switching a theme is a
single call and every widget follows.
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

    name: str          # совпадает с режимом CustomTkinter: dark | light
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

LIGHT = Palette(
    name="light",
    label="Светлая",
    bg="#F4F5F7",
    panel="#FFFFFF",
    panel_alt="#EDEFF2",
    input_bg="#FFFFFF",
    border="#D5D8DC",
    fg="#1F2124",
    fg_muted="#565C66",
    fg_subtle="#7A818B",
    accent="#D97706",
    accent_hover="#B45309",
    on_accent="#FFFFFF",
    success="#2E7D32",
    warning="#B45309",
    error="#C62828",
    info="#1565C0",
    log_bg="#FFFFFF",
    log_fg="#2B2F34",
    row_alt="#F1F2F4",
)

PALETTES: Dict[str, Palette] = {palette.name: palette for palette in (DARK, LIGHT)}
DEFAULT_THEME = DARK.name

# Log tags: every message colour in one place.
LOG_TAGS = ("error", "success", "info", "warning", "muted")


def resolve(name: Optional[str]) -> Palette:
    """Return the palette for `name`, falling back to the dark one."""
    if name:
        return PALETTES.get(str(name).casefold(), DARK)
    return DARK


def opposite(name: str) -> str:
    return "light" if name == "dark" else "dark"


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


def apply(root, palette: Palette) -> None:
    """Switch the whole application to `palette`."""
    ctk.set_appearance_mode(palette.name)
    root.configure(fg_color=palette.bg)


def font(size: int = 13, weight: Optional[str] = None, mono: bool = False) -> "ctk.CTkFont":
    """A font that follows the theme's own family."""
    return ctk.CTkFont(
        family=FONT_MONO if mono else FONT_FAMILY, size=size, weight=weight
    )
