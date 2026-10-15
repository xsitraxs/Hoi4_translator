"""Colour palettes and the ttk style sheet.

Both themes are described by one :class:`Palette`, so retuning a colour or the
accent is a one-line change, and switching a theme is a single re-apply of the
style sheet. Every widget uses a *named* style instead of a hard-coded colour:
that is what lets a running window change theme without being rebuilt.
"""
from dataclasses import dataclass
from tkinter import ttk
from typing import Dict, Optional, Tuple

# Segoe UI is present on Windows and macOS; Tk falls back gracefully elsewhere.
FONT = "Segoe UI"
FONT_MONO = "Consolas"


@dataclass(frozen=True)
class Palette:
    """One complete colour scheme."""

    name: str
    label: str
    bg: str           # фон окна
    panel: str        # панели, шапка, статус-бар
    panel_alt: str    # hover/active, ползунок прогресса
    input_bg: str     # поля ввода и комбобоксы
    border: str
    fg: str
    fg_muted: str
    fg_subtle: str
    accent: str
    accent_active: str
    on_accent: str    # текст на акцентной кнопке
    success: str
    warning: str
    error: str
    info: str
    log_bg: str
    log_fg: str
    row_alt: str      # «зебра» в предпросмотре


DARK = Palette(
    name="dark",
    label="Тёмная",
    bg="#1a1a1a",
    panel="#272727",
    panel_alt="#3a3a3a",
    input_bg="#232323",
    border="#3d3d3d",
    fg="#f2f2f2",
    fg_muted="#bbbbbb",
    fg_subtle="#8a8a8a",
    accent="#f5a524",
    accent_active="#ffbe45",
    on_accent="#1a1a1a",
    success="#69db7c",
    warning="#ffa500",
    error="#ff6b6b",
    info="#74c0fc",
    log_bg="#101010",
    log_fg="#d8d8d8",
    row_alt="#202020",
)

LIGHT = Palette(
    name="light",
    label="Светлая",
    bg="#f4f5f7",
    panel="#ffffff",
    panel_alt="#e9ebee",
    input_bg="#ffffff",
    border="#d5d8dc",
    fg="#1f2124",
    fg_muted="#4a4f55",
    fg_subtle="#6b7280",
    accent="#c2410c",
    accent_active="#9a3412",
    on_accent="#ffffff",
    success="#2e7d32",
    warning="#a16207",
    error="#c62828",
    info="#1565c0",
    log_bg="#ffffff",
    log_fg="#2b2f34",
    row_alt="#f1f2f4",
)

PALETTES: Dict[str, Palette] = {palette.name: palette for palette in (DARK, LIGHT)}
DEFAULT_THEME = DARK.name

# Log tags: every message colour in one place.
LOG_TAGS: Tuple[str, ...] = ("error", "success", "info", "warning", "muted")


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


def _label_styles(style: ttk.Style, palette: Palette, prefix: str, background: str) -> None:
    """Define the label family for one background (window or panel)."""
    base = f"{prefix}TLabel" if prefix else "TLabel"
    style.configure(base, background=background, foreground=palette.fg, font=(FONT, 10))
    style.configure(f"{prefix}Muted.TLabel", background=background, foreground=palette.fg_muted)
    style.configure(f"{prefix}Subtle.TLabel", background=background, foreground=palette.fg_subtle)
    style.configure(f"{prefix}Accent.TLabel", background=background, foreground=palette.accent)
    style.configure(f"{prefix}Success.TLabel", background=background, foreground=palette.success)
    style.configure(f"{prefix}Warning.TLabel", background=background, foreground=palette.warning)
    style.configure(f"{prefix}Error.TLabel", background=background, foreground=palette.error)
    style.configure(f"{prefix}Info.TLabel", background=background, foreground=palette.info)


def apply_style(root, palette: Palette) -> None:
    """Apply `palette` to `root` and to every named style the GUI uses."""
    style = ttk.Style(root)
    style.theme_use("clam")
    p = palette

    root.configure(background=p.bg)
    # Defaults for anything we did not name explicitly.
    style.configure(
        ".",
        background=p.bg,
        foreground=p.fg,
        font=(FONT, 10),
        bordercolor=p.border,
        troughcolor=p.panel_alt,
    )

    # --- containers ------------------------------------------------------
    style.configure("TFrame", background=p.bg)
    style.configure("Panel.TFrame", background=p.panel)
    style.configure("Header.TFrame", background=p.panel)
    style.configure("TLabelframe", background=p.bg, borderwidth=1, relief="solid")
    style.configure(
        "TLabelframe.Label",
        background=p.bg,
        foreground=p.accent,
        font=(FONT, 10, "bold"),
    )
    style.configure("TSeparator", background=p.border)

    # --- text ------------------------------------------------------------
    _label_styles(style, p, "", p.bg)
    _label_styles(style, p, "Panel.", p.panel)
    _label_styles(style, p, "Header.", p.panel)
    style.configure("Title.TLabel", background=p.panel, foreground=p.accent,
                    font=(FONT, 17, "bold"))
    style.configure("Header.Title.TLabel", background=p.panel, foreground=p.accent,
                    font=(FONT, 17, "bold"))
    style.configure("Subtitle.TLabel", background=p.panel, foreground=p.fg_subtle,
                    font=(FONT, 9))
    style.configure("Header.Subtitle.TLabel", background=p.panel,
                    foreground=p.fg_subtle, font=(FONT, 9))

    # --- buttons ---------------------------------------------------------
    style.configure("TButton", background=p.panel, foreground=p.fg,
                    borderwidth=1, padding=(12, 6), font=(FONT, 10))
    style.map(
        "TButton",
        background=[("active", p.panel_alt), ("disabled", p.panel)],
        foreground=[("disabled", p.fg_subtle)],
    )
    style.configure("Tool.TButton", padding=(10, 4), font=(FONT, 9))
    style.configure("Header.Tool.TButton", background=p.panel_alt, padding=(10, 4),
                    font=(FONT, 9))
    style.configure("Accent.TButton", background=p.accent, foreground=p.on_accent,
                    borderwidth=0, padding=(14, 8), font=(FONT, 10, "bold"))
    style.map(
        "Accent.TButton",
        background=[("active", p.accent_active), ("disabled", p.panel)],
        foreground=[("disabled", p.fg_subtle)],
    )
    style.configure("Danger.TButton", background=p.panel, foreground=p.error,
                    borderwidth=1, padding=(14, 8), font=(FONT, 10, "bold"))
    style.map(
        "Danger.TButton",
        background=[("active", p.panel_alt), ("disabled", p.panel)],
        foreground=[("disabled", p.fg_subtle)],
    )

    # --- inputs ----------------------------------------------------------
    style.configure("TEntry", fieldbackground=p.input_bg, foreground=p.fg,
                    insertcolor=p.fg, borderwidth=1, padding=(7, 5))
    style.map("TEntry", fieldbackground=[("disabled", p.panel)])
    style.configure("TCombobox", fieldbackground=p.input_bg, background=p.panel,
                    foreground=p.fg, arrowcolor=p.fg_muted, borderwidth=1,
                    padding=(7, 5))
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", p.input_bg), ("disabled", p.panel)],
        background=[("active", p.panel_alt)],
        selectbackground=[("readonly", p.input_bg)],
        selectforeground=[("readonly", p.fg)],
        arrowcolor=[("disabled", p.fg_subtle)],
    )
    style.configure("TCheckbutton", background=p.bg, foreground=p.fg,
                    indicatorcolor=p.input_bg, padding=(4, 2))
    style.map(
        "TCheckbutton",
        background=[("active", p.bg)],
        foreground=[("disabled", p.fg_subtle)],
        indicatorcolor=[("selected", p.accent)],
    )
    style.configure("TScale", background=p.bg, troughcolor=p.panel_alt)
    style.configure("Accent.Horizontal.TProgressbar", background=p.accent,
                    troughcolor=p.panel_alt, borderwidth=0, thickness=10)
    style.configure("Vertical.TScrollbar", background=p.panel,
                    troughcolor=p.bg, arrowcolor=p.fg_muted, borderwidth=0)
    style.map("Vertical.TScrollbar", background=[("active", p.panel_alt)])
