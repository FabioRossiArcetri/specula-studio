"""The four colour themes: complete, consistent and readable (WCAG contrast)."""

import pathlib
import re

import pytest

from specula_studio import theme_manager as tmod

SRC = pathlib.Path(tmod.__file__).parent
THEMES = {tid: tmod.Theme(tid, palette) for tid, (_, palette) in tmod.THEMES.items()}


def _luminance(c) -> float:
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _keys_used_in_code() -> set:
    keys = set()
    for f in SRC.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        keys |= set(re.findall(r"get_color\(\s*['\"]([a-z0-9_]+)['\"]", text))
        if f.name == "dpg_utils.py":                                        # node themes
            keys |= set(re.findall(r"\w+_key\s*=\s*['\"]([a-z0-9_]+)['\"]", text))
        # status -> colour key tables of the monitor windows
        keys |= set(re.findall(r"['\"]((?:monitor_status|text)_[a-z_]+)['\"]\s*[,}]", text))
    return keys


def test_two_dark_and_two_light_themes():
    assert sorted(t.dark for t in THEMES.values()) == [False, False, True, True]
    assert len({label for label, _ in tmod.THEMES.values()}) == 4


def test_every_theme_defines_every_colour_the_gui_uses():
    keys = set(THEMES["dark"].colors)
    for theme in THEMES.values():
        assert set(theme.colors) == keys, theme.name
    missing = _keys_used_in_code() - keys
    assert not missing, f"colour keys used but not defined: {sorted(missing)}"


def test_theme_ids_and_labels():
    assert tmod.theme_id("dark") == "dark" and tmod.theme_id("light") == "light"  # old ids
    assert tmod.theme_id("Light - Paper") == "paper"
    assert tmod.theme_id("no such theme") == tmod.DEFAULT_THEME
    assert tmod.theme_label("midnight") == "Dark - Midnight"


# (foreground key, background keys, minimum contrast)
_READABILITY = [
    ("text_default", ("window_bg", "child_bg", "popup_bg", "frame_bg"), 7.0),
    ("text_default", ("button_bg", "button_hovered", "title_bg", "table_header_bg"), 4.5),
    ("text_secondary", ("window_bg", "child_bg"), 4.5),
    ("text_hint", ("window_bg", "child_bg"), 3.0),
    ("text_disabled", ("window_bg",), 1.8),        # visibly dimmer, still visible
    ("node_input_label", ("node_data_bg", "node_proc_bg"), 7.0),
    ("text_default", ("node_data_title", "node_proc_title", "node_data_selected",
                      "node_proc_selected"), 4.5),
    ("node_class_label", ("node_data_bg", "node_proc_bg"), 4.5),
    ("node_hint_text", ("node_data_bg", "node_proc_bg"), 3.0),
]
_MEANINGFUL = ["text_success", "text_error", "accent", "section_header", "dialog_title",
               "monitor_status_waiting", "monitor_status_receiving", "monitor_status_error",
               "monitor_output_label", "panel_label_required", "panel_label_data_obj",
               "panel_label_ref", "panel_text_hint", "help_param_name", "help_param_type",
               "control_section_header", "control_subsection_header", "param_unit"]
_ON_NODES = ["node_ref_input_label", "node_data_obj_ref_output", "node_dynamic_output_label"]


@pytest.mark.parametrize("tid", sorted(THEMES))
def test_text_is_readable(tid):
    t = THEMES[tid]
    problems = []
    rules = list(_READABILITY)
    rules += [(k, ("window_bg", "child_bg"), 4.5) for k in _MEANINGFUL]
    rules += [(k, ("node_data_bg", "node_proc_bg"), 3.0) for k in _ON_NODES]
    for fg, bgs, minimum in rules:
        for bg in bgs:
            ratio = contrast(t.get(fg), t.get(bg))
            if ratio < minimum:
                problems.append(f"{fg} on {bg}: {ratio:.2f} < {minimum}")
    assert not problems, f"{tid}: " + "; ".join(problems)


@pytest.mark.parametrize("tid", sorted(THEMES))
def test_kinds_of_nodes_and_states_are_distinguishable(tid):
    t = THEMES[tid]
    assert t.get("node_data_bg") != t.get("node_proc_bg")
    assert contrast(t.get("node_incomplete_outline"), t.get("node_editor_bg")) >= 3.0
    assert contrast(t.get("node_highlight"), t.get("node_editor_bg")) >= 1.8
    assert contrast(t.get("link_default"), t.get("node_editor_bg")) >= 3.0
    assert len({t.get(k)[:3] for k in ("text_success", "text_error",
                                       "monitor_status_waiting")}) == 3
