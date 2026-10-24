"""
Colour themes: two dark, a medium-tone, two light and a high-contrast one.

Each theme is a :class:`Palette` of base colours (background levels, text
levels, accent, semantic colours, node colours).  :func:`_colors` derives
every *logical* colour the GUI asks for (``get_color("panel_label_required")``)
from the palette, so all themes define the same keys and stay consistent;
:meth:`ThemeManager.apply_theme` maps them onto every DearPyGui widget,
ImPlot and node-editor colour.

Legibility is enforced, not hoped for: :func:`_legible` moves every text
colour towards white (dark themes) or black (light themes) just enough to
reach WCAG contrast 4.5 on every surface it is drawn on (3.0 for disabled
text and for links on the canvas), and moves the surfaces that carry text
(title bars, selections) the other way.

Widgets get their meaningful colours (``color=get_color(...)``) when they
are created; on a theme switch :meth:`ThemeManager.save_preference` gives
every widget still showing a colour of the old theme the same logical colour
of the new one, so windows that stay open do not keep unreadable colours.

Theme ids are stored in the settings (``"theme"``); ``"dark"`` and
``"light"`` are the ids of the original two themes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import dearpygui.dearpygui as dpg

from . import settings

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class Palette:
    dark: bool
    # surfaces
    bg: RGB              # window background
    panel: RGB           # child windows, panels, plot frames
    raised: RGB          # popups, menus
    frame: RGB           # input fields
    frame_hover: RGB
    frame_active: RGB
    border: RGB
    title: RGB           # window title bars
    title_active: RGB
    button: RGB
    button_hover: RGB
    button_active: RGB
    # text
    text: RGB
    text2: RGB           # secondary
    text3: RGB           # muted / hints
    text_disabled: RGB
    # accents and meanings
    accent: RGB          # selection, checkmarks, sliders
    accent_text: RGB     # accent used for text (headers, links)
    heading: RGB         # dialog titles, control-panel sections
    success: RGB
    warning: RGB
    error: RGB
    info: RGB
    data: RGB            # data objects
    ref: RGB             # references
    # node editor
    canvas: RGB
    grid: RGB
    node_data: RGB
    node_proc: RGB
    node_data_title: RGB
    node_proc_title: RGB
    node_data_outline: RGB
    node_proc_outline: RGB
    link: RGB
    link_feedback: RGB
    highlight: RGB       # "find" flash


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    """*a* moved a fraction *t* towards *b*."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _rgba(c, alpha: int = 255) -> tuple[int, int, int, int]:
    return (c[0], c[1], c[2], alpha)


# ── legibility ────────────────────────────────────────────────────────────────

TEXT_CONTRAST = 4.5         # WCAG AA, normal text
DISABLED_CONTRAST = 3.0     # disabled text, links and outlines on the canvas
_WHITE, _BLACK = (255, 255, 255), (0, 0, 0)
# palette fields drawn as text
_TEXT_FIELDS = ("text", "text2", "text3", "accent_text", "heading", "success", "warning",
                "error", "info", "data", "ref")
# palette fields drawn on the canvas as lines
_CANVAS_FIELDS = ("link", "link_feedback", "accent")
# palette fields that text is drawn on
_TEXT_CARRIERS = ("title", "title_active", "frame_active", "button_active",
                  "node_data_title", "node_proc_title")


def luminance(c) -> float:
    """WCAG relative luminance of an RGB(A) colour (0-255)."""
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    """WCAG contrast ratio of two colours (1 to 21)."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _towards(c: RGB, target: RGB, ok) -> RGB:
    """*c* moved towards *target* in small steps until ``ok(colour)``."""
    for i in range(41):
        moved = _mix(c, target, i / 40)
        if ok(moved):
            return moved
    return target


def _surfaces(p: Palette) -> tuple:
    """The surfaces ordinary text is drawn on."""
    return (p.bg, p.panel, p.raised, p.frame, p.frame_hover, p.button, p.button_hover,
            p.node_data, p.node_proc)


def _readable(c: RGB, p: Palette, backgrounds, minimum: float) -> RGB:
    """Text colour *c*, made just readable enough on all *backgrounds*."""
    ink = _WHITE if p.dark else _BLACK
    return _towards(c, ink, lambda x: all(contrast(x, b) >= minimum for b in backgrounds))


def _backdrop(c: RGB, p: Palette, *texts: RGB) -> RGB:
    """Surface *c*, made just dark (light) enough for *texts* on it."""
    paper = _BLACK if p.dark else _WHITE
    texts = texts or (p.text,)
    return _towards(c, paper, lambda x: all(contrast(t, x) >= TEXT_CONTRAST for t in texts))


def _legible(p: Palette) -> Palette:
    """*p* with every text readable on every surface it is drawn on."""
    surfaces = _surfaces(p)
    p = replace(p, **{f: _readable(getattr(p, f), p, surfaces, TEXT_CONTRAST)
                      for f in _TEXT_FIELDS},
                text_disabled=_readable(p.text_disabled, p, surfaces, DISABLED_CONTRAST))
    p = replace(p, **{f: _readable(getattr(p, f), p, (p.canvas,), DISABLED_CONTRAST)
                      for f in _CANVAS_FIELDS})
    return replace(p, **{f: _backdrop(getattr(p, f), p) for f in _TEXT_CARRIERS})


# logical colours used as backgrounds only (never as a widget's text colour)
BACKGROUND_KEYS = frozenset({
    "window_bg", "child_bg", "popup_bg", "frame_bg", "frame_hovered", "frame_active",
    "button_bg", "button_hovered", "button_active", "title_bg", "title_bg_active",
    "menu_bar_bg", "table_row_bg", "table_row_bg_alt", "table_header_bg", "header",
    "header_hovered", "header_active", "node_editor_bg", "plot_bg", "plot_grid",
    "node_data_bg", "node_proc_bg", "node_data_selected", "node_proc_selected",
    "node_data_title", "node_proc_title", "node_data_title_hover", "node_proc_title_hover",
    "node_data_title_selected", "node_proc_title_selected", "text_selected",
})


def _colors(p: Palette) -> dict:
    """Every logical colour of the GUI, derived from palette *p* (RGBA 0-255)."""
    p = _legible(p)
    # selections (list rows, combo items, tree headers) and node states carry text
    header = _backdrop(_mix(p.accent, p.panel, 0.62 if p.dark else 0.78), p)
    header_hover = _backdrop(_mix(p.accent, p.panel, 0.48 if p.dark else 0.66), p)
    header_active = _backdrop(_mix(p.accent, p.panel, 0.34 if p.dark else 0.54), p)
    on_nodes = (p.text, p.text2)
    c = {
        "header": header, "header_hovered": header_hover, "header_active": header_active,
        # ── surfaces ───────────────────────────────────────────────────────
        "window_bg": p.bg, "child_bg": p.panel, "popup_bg": p.raised,
        "frame_bg": p.frame, "frame_hovered": p.frame_hover, "frame_active": p.frame_active,
        "button_bg": p.button, "button_hovered": p.button_hover,
        "button_active": p.button_active,
        "title_bg": p.title, "title_bg_active": p.title_active, "menu_bar_bg": p.raised,
        "border": p.border,
        "text_default": p.text, "text_disabled": p.text_disabled,
        "table_row_bg": p.panel, "table_row_bg_alt": _mix(p.panel, p.frame, 0.45),
        "table_header_bg": p.frame,
        # ── node editor ────────────────────────────────────────────────────
        "node_editor_bg": p.canvas, "plot_grid": p.grid, "plot_bg": p.canvas,
        "link_default": p.link, "link_selected": p.accent,
        "node_data_bg": p.node_data, "node_proc_bg": p.node_proc,
        "node_data_selected": _backdrop(_mix(p.node_data, p.ref, 0.30), p, *on_nodes),
        "node_proc_selected": _backdrop(_mix(p.node_proc, p.success, 0.28), p, *on_nodes),
        "node_data_outline": p.node_data_outline, "node_proc_outline": p.node_proc_outline,
        "node_data_title": p.node_data_title, "node_proc_title": p.node_proc_title,
        "node_data_title_hover": _backdrop(_mix(p.node_data_title, p.accent, 0.20), p),
        "node_proc_title_hover": _backdrop(_mix(p.node_proc_title, p.accent, 0.20), p),
        "node_data_title_selected": _backdrop(_mix(p.node_data_title, p.ref, 0.40), p),
        "node_proc_title_selected": _backdrop(_mix(p.node_proc_title, p.success, 0.40), p),
        "node_pin": p.text2, "node_pin_hover": p.accent,
        "node_incomplete_outline": p.error, "node_highlight": p.highlight,
        "node_feedback_link_color": p.link_feedback,
        "node_ref_link_color": _rgba(_mix(p.link, p.canvas, 0.35), 110),
        # labels drawn on nodes
        "node_class_label": p.text2, "node_input_label": p.text, "node_output_label": p.text,
        "node_hint_text": p.text3, "node_ref_input_label": p.success,
        "node_data_obj_ref_output": p.data, "node_dynamic_output_label": p.info,
        # ── text with a meaning ────────────────────────────────────────────
        "text_secondary": p.text2, "text_instruction": p.text2, "status_text": p.text2,
        "text_muted": p.text3, "text_hint": p.text3,
        "text_success": p.success, "text_error": p.error,
        "accent": p.accent_text, "section_header": p.accent_text,
        "dialog_title": p.heading, "font_selected_text": p.success,
        "tooltip_text": p.text,
        # monitors
        "monitor_output_label": p.success, "monitor_status_waiting": p.warning,
        "monitor_status_receiving": p.info, "monitor_status_subscribed": p.success,
        "monitor_status_error": p.error,
        # help window
        "help_param_name": p.ref, "help_param_required": p.error, "help_param_type": p.data,
        "help_default": p.text2, "help_default_required": p.error,
        # property panel
        "param_default": p.text3, "param_modified": p.text, "param_hint": p.text3,
        "param_unit": p.success,
        "panel_section_header": p.accent_text, "panel_label_normal": p.text,
        "panel_label_required": p.error, "panel_label_data_obj": p.data,
        "panel_label_ref": p.ref, "panel_text_secondary": p.text2,
        "panel_text_muted": p.text3, "panel_text_hint": p.warning,
        "panel_conn_src_name": p.success, "panel_conn_dst_name": p.success,
        "panel_monitor_active": p.success,
        # simulation control
        "control_section_header": p.heading, "control_subsection_header": p.accent_text,
        "control_terminal_title": p.text3,
        # file dialogs
        "file_ext_data": p.success, "file_ext_numpy": p.data, "file_ext_other": p.text3,
    }
    out = {k: (v if len(v) == 4 else _rgba(v)) for k, v in c.items()}
    out["text_selected"] = _rgba(p.accent, 90)          # selected-text background
    return out


# ── the themes ────────────────────────────────────────────────────────────────

GRAPHITE = Palette(
    dark=True,
    bg=(24, 25, 28), panel=(30, 31, 35), raised=(38, 40, 45),
    frame=(44, 46, 52), frame_hover=(56, 59, 66), frame_active=(38, 70, 106),
    border=(64, 67, 75), title=(20, 21, 24), title_active=(32, 40, 54),
    button=(52, 55, 62), button_hover=(66, 70, 79), button_active=(44, 104, 166),
    text=(232, 234, 238), text2=(184, 188, 196), text3=(140, 145, 155),
    text_disabled=(108, 112, 120),
    accent=(74, 158, 255), accent_text=(112, 182, 255), heading=(240, 198, 116),
    success=(112, 206, 122), warning=(240, 182, 72), error=(255, 112, 106),
    info=(84, 206, 230), data=(132, 182, 255), ref=(240, 166, 112),
    canvas=(27, 28, 32), grid=(42, 44, 51),
    node_data=(46, 49, 56), node_proc=(32, 50, 76),
    node_data_title=(64, 68, 78), node_proc_title=(38, 70, 112),
    node_data_outline=(80, 84, 96), node_proc_outline=(60, 92, 134),
    link=(150, 166, 206), link_feedback=(255, 96, 90), highlight=(255, 206, 64),
)

MIDNIGHT = Palette(
    dark=True,
    bg=(13, 17, 28), panel=(18, 23, 37), raised=(25, 32, 50),
    frame=(30, 39, 60), frame_hover=(40, 52, 78), frame_active=(18, 82, 92),
    border=(48, 60, 88), title=(10, 13, 22), title_active=(16, 40, 54),
    button=(34, 45, 70), button_hover=(46, 60, 92), button_active=(16, 122, 122),
    text=(226, 232, 245), text2=(172, 184, 208), text3=(124, 138, 164),
    text_disabled=(94, 106, 130),
    accent=(38, 198, 186), accent_text=(76, 216, 202), heading=(255, 192, 122),
    success=(126, 222, 142), warning=(250, 192, 82), error=(255, 120, 130),
    info=(112, 172, 255), data=(130, 178, 255), ref=(255, 162, 122),
    canvas=(11, 15, 25), grid=(26, 34, 54),
    node_data=(30, 38, 58), node_proc=(18, 50, 60),
    node_data_title=(44, 56, 84), node_proc_title=(16, 84, 92),
    node_data_outline=(60, 74, 106), node_proc_outline=(30, 112, 114),
    link=(132, 162, 222), link_feedback=(255, 102, 112), highlight=(255, 214, 90),
)

DAYLIGHT = Palette(
    dark=False,
    bg=(243, 245, 248), panel=(250, 251, 253), raised=(255, 255, 255),
    frame=(229, 233, 239), frame_hover=(214, 221, 231), frame_active=(196, 217, 246),
    border=(190, 198, 210), title=(226, 231, 238), title_active=(204, 220, 243),
    button=(221, 227, 236), button_hover=(202, 213, 229), button_active=(166, 196, 240),
    text=(24, 28, 36), text2=(70, 78, 92), text3=(98, 106, 120),
    text_disabled=(150, 156, 168),
    accent=(26, 115, 232), accent_text=(14, 94, 198), heading=(138, 88, 0),
    success=(20, 122, 50), warning=(158, 90, 0), error=(190, 36, 36),
    info=(0, 116, 140), data=(18, 88, 184), ref=(170, 80, 8),
    canvas=(250, 251, 253), grid=(226, 230, 237),
    node_data=(236, 239, 244), node_proc=(221, 233, 250),
    node_data_title=(214, 220, 230), node_proc_title=(188, 210, 243),
    node_data_outline=(166, 174, 190), node_proc_outline=(136, 166, 212),
    link=(88, 102, 138), link_feedback=(200, 38, 38), highlight=(230, 146, 0),
)

PAPER = Palette(
    dark=False,
    bg=(246, 241, 231), panel=(251, 248, 241), raised=(255, 252, 246),
    frame=(235, 227, 212), frame_hover=(224, 213, 193), frame_active=(238, 212, 166),
    border=(206, 192, 168), title=(233, 224, 206), title_active=(231, 210, 172),
    button=(230, 220, 201), button_hover=(219, 205, 180), button_active=(212, 174, 110),
    text=(44, 36, 28), text2=(92, 78, 62), text3=(116, 100, 82),
    text_disabled=(164, 150, 130),
    accent=(176, 104, 26), accent_text=(140, 78, 8), heading=(132, 60, 20),
    success=(56, 112, 36), warning=(156, 82, 0), error=(176, 38, 34),
    info=(26, 104, 124), data=(36, 84, 156), ref=(160, 74, 14),
    canvas=(251, 248, 241), grid=(232, 223, 206),
    node_data=(240, 233, 220), node_proc=(222, 233, 226),
    node_data_title=(226, 215, 196), node_proc_title=(196, 219, 208),
    node_data_outline=(194, 178, 152), node_proc_outline=(148, 182, 166),
    link=(108, 94, 78), link_feedback=(186, 38, 34), highlight=(212, 118, 0),
)

# Medium tones: mid-grey surfaces, light text (a "dark" palette for the
# derivations: text is lighter than its background).
SLATE = Palette(
    dark=True,
    bg=(62, 68, 78), panel=(68, 74, 85), raised=(76, 83, 95),
    frame=(78, 85, 98), frame_hover=(86, 94, 108), frame_active=(58, 92, 130),
    border=(112, 121, 137), title=(56, 61, 70), title_active=(64, 84, 110),
    button=(82, 90, 103), button_hover=(90, 98, 112), button_active=(62, 104, 150),
    text=(250, 251, 253), text2=(228, 232, 238), text3=(208, 214, 222),
    text_disabled=(166, 172, 182),
    accent=(126, 194, 255), accent_text=(170, 216, 255), heading=(255, 218, 150),
    success=(160, 232, 160), warning=(255, 212, 120), error=(255, 172, 162),
    info=(150, 230, 242), data=(180, 212, 255), ref=(255, 196, 150),
    canvas=(66, 72, 82), grid=(78, 85, 97),
    node_data=(80, 87, 99), node_proc=(64, 80, 102),
    node_data_title=(96, 104, 118), node_proc_title=(66, 94, 130),
    node_data_outline=(132, 140, 156), node_proc_outline=(112, 142, 182),
    link=(206, 216, 238), link_feedback=(255, 138, 128), highlight=(255, 224, 96),
)

# High contrast: black surfaces, white text, saturated accents, white outlines.
CONTRAST = Palette(
    dark=True,
    bg=(0, 0, 0), panel=(0, 0, 0), raised=(12, 12, 12),
    frame=(22, 22, 22), frame_hover=(40, 40, 40), frame_active=(0, 56, 104),
    border=(220, 220, 220), title=(0, 0, 0), title_active=(0, 48, 96),
    button=(28, 28, 28), button_hover=(48, 48, 48), button_active=(0, 72, 140),
    text=(255, 255, 255), text2=(240, 240, 240), text3=(222, 222, 222),
    text_disabled=(150, 150, 150),
    accent=(255, 230, 0), accent_text=(0, 228, 255), heading=(255, 236, 64),
    success=(96, 255, 128), warning=(255, 196, 0), error=(255, 104, 104),
    info=(64, 232, 255), data=(130, 204, 255), ref=(255, 172, 72),
    canvas=(0, 0, 0), grid=(38, 38, 38),
    node_data=(16, 16, 16), node_proc=(0, 22, 44),
    node_data_title=(58, 58, 58), node_proc_title=(0, 62, 122),
    node_data_outline=(230, 230, 230), node_proc_outline=(120, 200, 255),
    link=(235, 235, 235), link_feedback=(255, 84, 84), highlight=(255, 255, 96),
)

# id -> (label, palette); ids are stored in the settings
THEMES: dict[str, tuple[str, Palette]] = {
    "dark": ("Dark - Graphite", GRAPHITE),
    "midnight": ("Dark - Midnight", MIDNIGHT),
    "slate": ("Medium - Slate", SLATE),
    "light": ("Light - Daylight", DAYLIGHT),
    "paper": ("Light - Paper", PAPER),
    "contrast": ("High Contrast", CONTRAST),
}
DEFAULT_THEME = "dark"


def theme_label(theme_id: str) -> str:
    return THEMES.get(theme_id, THEMES[DEFAULT_THEME])[0]


def theme_id(label_or_id: str) -> str:
    """The id of a theme given its id or its label (unknown -> default)."""
    if label_or_id in THEMES:
        return label_or_id
    return next((tid for tid, (label, _) in THEMES.items() if label == label_or_id),
                DEFAULT_THEME)


class Theme:
    """The logical colours of one theme."""

    def __init__(self, name: str, palette: Palette):
        self.name = name                       # theme id
        self.palette = _legible(palette)
        self.colors = _colors(palette)

    @property
    def dark(self) -> bool:
        return self.palette.dark

    def get(self, key: str):
        return self.colors[key]


class ThemeManager:
    """Singleton: the current theme, its colours and their DearPyGui binding."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.current_theme: Theme | None = None
        self._global_theme = None
        self.load_preference()

    def load_preference(self):
        tid = theme_id(str(settings.store().get("theme", DEFAULT_THEME)))
        self.current_theme = Theme(tid, THEMES[tid][1])

    def save_preference(self, theme_name: str):
        store = settings.store()
        store["theme"] = theme_id(theme_name)
        store.save()
        old = self.current_theme
        self.load_preference()
        self.apply_theme()
        if old is not None:
            self.recolor_items(old, self.current_theme)

    @staticmethod
    def recolor_items(old: Theme, new: Theme) -> None:
        """Give every widget showing a colour of *old* the same logical colour
        of *new* (widgets get their colours when they are created: without
        this, windows that stay open keep the old theme's colours)."""
        mapping = {}
        for key, value in old.colors.items():
            if key not in BACKGROUND_KEYS:
                mapping.setdefault(tuple(value), new.colors[key])
        for item in dpg.get_all_items():
            try:
                color = dpg.get_item_configuration(item).get("color")
            except Exception:       # items without a configuration
                continue
            if not color or color[0] < 0:          # no colour, or the default one
                continue
            target = mapping.get(tuple(round(v * 255) for v in color))
            if target is not None:
                dpg.configure_item(item, color=target)

    def get_color(self, key: str):
        assert self.current_theme is not None
        return self.current_theme.get(key)

    # ── binding ───────────────────────────────────────────────────────────────

    def apply_theme(self):
        """Bind the current theme to every widget, plot and node editor."""
        if not self.current_theme:
            return
        p = self.current_theme.palette
        g = self.current_theme.get
        dim = (0, 0, 0, 120) if p.dark else (40, 34, 28, 70)
        header, header_hover, header_active = (g("header"), g("header_hovered"),
                                               g("header_active"))

        core = {
            dpg.mvThemeCol_WindowBg: g("window_bg"),
            dpg.mvThemeCol_ChildBg: g("child_bg"),
            dpg.mvThemeCol_PopupBg: g("popup_bg"),
            dpg.mvThemeCol_MenuBarBg: g("menu_bar_bg"),
            dpg.mvThemeCol_Text: g("text_default"),
            dpg.mvThemeCol_TextDisabled: g("text_disabled"),
            dpg.mvThemeCol_TextSelectedBg: g("text_selected"),
            dpg.mvThemeCol_InputTextCursor: g("text_default"),
            dpg.mvThemeCol_Border: g("border"),
            dpg.mvThemeCol_BorderShadow: (0, 0, 0, 0),
            dpg.mvThemeCol_FrameBg: g("frame_bg"),
            dpg.mvThemeCol_FrameBgHovered: g("frame_hovered"),
            dpg.mvThemeCol_FrameBgActive: g("frame_active"),
            dpg.mvThemeCol_TitleBg: g("title_bg"),
            dpg.mvThemeCol_TitleBgActive: g("title_bg_active"),
            dpg.mvThemeCol_TitleBgCollapsed: g("title_bg"),
            dpg.mvThemeCol_Button: g("button_bg"),
            dpg.mvThemeCol_ButtonHovered: g("button_hovered"),
            dpg.mvThemeCol_ButtonActive: g("button_active"),
            dpg.mvThemeCol_Header: header,
            dpg.mvThemeCol_HeaderHovered: header_hover,
            dpg.mvThemeCol_HeaderActive: header_active,
            dpg.mvThemeCol_CheckMark: _rgba(p.accent),
            dpg.mvThemeCol_SliderGrab: _rgba(p.accent),
            dpg.mvThemeCol_SliderGrabActive: _rgba(_mix(p.accent, p.text, 0.25)),
            dpg.mvThemeCol_ScrollbarBg: _rgba(p.panel),
            dpg.mvThemeCol_ScrollbarGrab: _rgba(p.frame_hover),
            dpg.mvThemeCol_ScrollbarGrabHovered: _rgba(p.border),
            dpg.mvThemeCol_ScrollbarGrabActive: _rgba(p.accent),
            dpg.mvThemeCol_Separator: g("border"),
            dpg.mvThemeCol_SeparatorHovered: _rgba(p.accent),
            dpg.mvThemeCol_SeparatorActive: _rgba(p.accent),
            dpg.mvThemeCol_ResizeGrip: _rgba(p.border, 120),
            dpg.mvThemeCol_ResizeGripHovered: _rgba(p.accent, 170),
            dpg.mvThemeCol_ResizeGripActive: _rgba(p.accent, 230),
            dpg.mvThemeCol_Tab: g("frame_bg"),
            dpg.mvThemeCol_TabHovered: header_hover,
            dpg.mvThemeCol_TabSelected: header_active,
            dpg.mvThemeCol_TabSelectedOverline: _rgba(p.accent),
            dpg.mvThemeCol_TabDimmed: _rgba(p.panel),
            dpg.mvThemeCol_TabDimmedSelected: header,
            dpg.mvThemeCol_TableHeaderBg: g("table_header_bg"),
            dpg.mvThemeCol_TableBorderStrong: g("border"),
            dpg.mvThemeCol_TableBorderLight: _rgba(_mix(p.border, p.panel, 0.5)),
            dpg.mvThemeCol_TableRowBg: g("table_row_bg"),
            dpg.mvThemeCol_TableRowBgAlt: g("table_row_bg_alt"),
            dpg.mvThemeCol_PlotLines: _rgba(p.accent),
            dpg.mvThemeCol_PlotLinesHovered: _rgba(p.warning),
            dpg.mvThemeCol_PlotHistogram: _rgba(p.warning),
            dpg.mvThemeCol_PlotHistogramHovered: _rgba(p.accent),
            dpg.mvThemeCol_DragDropTarget: _rgba(p.warning),
            dpg.mvThemeCol_NavHighlight: _rgba(p.accent),
            dpg.mvThemeCol_NavWindowingHighlight: _rgba(p.accent),
            dpg.mvThemeCol_NavWindowingDimBg: dim,
            dpg.mvThemeCol_ModalWindowDimBg: dim,
        }
        plot = {
            dpg.mvPlotCol_FrameBg: g("child_bg"),
            dpg.mvPlotCol_PlotBg: g("plot_bg"),
            dpg.mvPlotCol_PlotBorder: g("border"),
            dpg.mvPlotCol_LegendBg: _rgba(p.raised, 230),
            dpg.mvPlotCol_LegendBorder: g("border"),
            dpg.mvPlotCol_LegendText: g("text_default"),
            dpg.mvPlotCol_TitleText: g("text_default"),
            dpg.mvPlotCol_InlayText: g("text_secondary"),
            dpg.mvPlotCol_AxisText: g("text_secondary"),
            dpg.mvPlotCol_AxisGrid: g("plot_grid"),
            dpg.mvPlotCol_AxisTick: g("border"),
            dpg.mvPlotCol_Selection: _rgba(p.warning),
            dpg.mvPlotCol_Crosshairs: g("text_secondary"),
        }
        nodes = {
            dpg.mvNodeCol_NodeBackground: g("node_proc_bg"),
            dpg.mvNodeCol_NodeBackgroundHovered: _rgba(_mix(p.node_proc, p.text, 0.06)),
            dpg.mvNodeCol_NodeBackgroundSelected: g("node_proc_selected"),
            dpg.mvNodeCol_NodeOutline: g("node_proc_outline"),
            dpg.mvNodeCol_TitleBar: g("node_proc_title"),
            dpg.mvNodeCol_TitleBarHovered: g("node_proc_title_hover"),
            dpg.mvNodeCol_TitleBarSelected: g("node_proc_title_selected"),
            dpg.mvNodeCol_Link: g("link_default"),
            dpg.mvNodeCol_LinkHovered: _rgba(p.accent),
            dpg.mvNodeCol_LinkSelected: _rgba(_mix(p.accent, p.text, 0.25)),
            dpg.mvNodeCol_Pin: g("node_pin"),
            dpg.mvNodeCol_PinHovered: g("node_pin_hover"),
            dpg.mvNodeCol_BoxSelector: _rgba(p.accent, 40),
            dpg.mvNodeCol_BoxSelectorOutline: _rgba(p.accent, 160),
            dpg.mvNodeCol_GridBackground: g("node_editor_bg"),
            dpg.mvNodeCol_GridLine: g("plot_grid"),
            # minimap: a small copy of the canvas, the visible part outlined
            dpg.mvNodesCol_MiniMapBackground: _rgba(p.panel, 225),
            dpg.mvNodesCol_MiniMapBackgroundHovered: _rgba(p.panel, 245),
            dpg.mvNodesCol_MiniMapOutline: g("border"),
            dpg.mvNodesCol_MiniMapOutlineHovered: _rgba(p.accent),
            dpg.mvNodesCol_MiniMapNodeBackground: g("node_proc_title"),
            dpg.mvNodesCol_MiniMapNodeBackgroundHovered: g("node_proc_title_hover"),
            dpg.mvNodesCol_MiniMapNodeBackgroundSelected: _rgba(p.accent),
            dpg.mvNodesCol_MiniMapNodeOutline: g("node_proc_outline"),
            dpg.mvNodesCol_MiniMapLink: g("link_default"),
            dpg.mvNodesCol_MiniMapLinkSelected: _rgba(p.accent),
            dpg.mvNodesCol_MiniMapCanvas: _rgba(p.canvas, 120),
            dpg.mvNodesCol_MiniMapCanvasOutline: g("text_secondary"),
        }

        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                for key, color in core.items():
                    dpg.add_theme_color(key, color)
                for key, color in plot.items():
                    dpg.add_theme_color(key, color, category=dpg.mvThemeCat_Plots)
                for key, color in nodes.items():
                    dpg.add_theme_color(key, color, category=dpg.mvThemeCat_Nodes)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 3)
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, 3)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 3)

            # Disabled widgets (configure_item(..., enabled=False)): DearPyGui
            # does not dim them by itself, so give them the disabled text
            # colour and a faded frame.
            disabled_text = g("text_disabled")
            faded = _rgba(p.frame, 110)
            for item_type in (dpg.mvInputText, dpg.mvInputInt, dpg.mvCombo,
                              dpg.mvButton, dpg.mvCheckbox):
                with dpg.theme_component(item_type, enabled_state=False):
                    dpg.add_theme_color(dpg.mvThemeCol_Text, disabled_text)
                    for col in (dpg.mvThemeCol_FrameBg, dpg.mvThemeCol_FrameBgHovered,
                                dpg.mvThemeCol_FrameBgActive, dpg.mvThemeCol_Button,
                                dpg.mvThemeCol_ButtonHovered, dpg.mvThemeCol_ButtonActive):
                        dpg.add_theme_color(col, faded)

        dpg.bind_theme(global_theme)
        # Free the previously bound global theme (one is created per switch).
        if self._global_theme is not None and dpg.does_item_exist(self._global_theme):
            dpg.delete_item(self._global_theme)
        self._global_theme = global_theme
