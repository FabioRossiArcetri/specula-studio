"""The colour themes: complete, consistent and readable (WCAG contrast)."""

import pathlib
import re

import pytest

from specula_studio import theme_manager as tmod

SRC = pathlib.Path(tmod.__file__).parent
THEMES = {tid: tmod.Theme(tid, palette) for tid, (_, palette) in tmod.THEMES.items()}


contrast = tmod.contrast


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


def test_dark_medium_light_and_high_contrast_themes():
    assert {"dark", "midnight", "slate", "light", "paper", "contrast"} <= set(THEMES)
    assert not THEMES["light"].dark and not THEMES["paper"].dark
    assert THEMES["dark"].dark and THEMES["contrast"].dark
    assert len({label for label, _ in tmod.THEMES.values()}) == len(tmod.THEMES)


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


# every surface text is drawn on, and the text drawn on each
_SURFACES = ("window_bg", "child_bg", "popup_bg", "frame_bg", "frame_hovered", "button_bg",
             "button_hovered", "table_header_bg", "node_data_bg", "node_proc_bg")
_TEXTS = ["text_default", "text_secondary", "text_muted", "text_hint", "text_success",
          "text_error", "accent", "section_header", "dialog_title", "monitor_status_waiting",
          "monitor_status_receiving", "monitor_status_error", "monitor_output_label",
          "panel_label_required", "panel_label_data_obj", "panel_label_ref", "panel_text_hint",
          "help_param_name", "help_param_type", "control_section_header",
          "control_subsection_header", "param_unit", "param_default", "node_class_label",
          "node_hint_text", "node_ref_input_label", "node_data_obj_ref_output",
          "node_dynamic_output_label", "font_selected_text"]
_CARRIERS = ("title_bg", "title_bg_active", "frame_active", "button_active", "header",
             "header_hovered", "header_active", "node_data_title", "node_proc_title",
             "node_data_title_hover", "node_proc_title_hover", "node_data_title_selected",
             "node_proc_title_selected", "node_data_selected", "node_proc_selected")


@pytest.mark.parametrize("tid", sorted(THEMES))
def test_text_is_readable(tid):
    t = THEMES[tid]
    strong = 7.0 if tid == "contrast" else tmod.TEXT_CONTRAST
    rules = [(fg, _SURFACES, tmod.TEXT_CONTRAST) for fg in _TEXTS]
    rules += [("text_default", _SURFACES, strong), ("text_hint", _SURFACES, strong)]
    rules += [("text_default", _CARRIERS, tmod.TEXT_CONTRAST),
              ("text_disabled", _SURFACES, tmod.DISABLED_CONTRAST)]
    problems = [f"{fg} on {bg}: {contrast(t.get(fg), t.get(bg)):.2f} < {minimum}"
                for fg, bgs, minimum in rules for bg in bgs
                if contrast(t.get(fg), t.get(bg)) < minimum]
    assert not problems, f"{tid}: " + "; ".join(problems)


def test_theme_switch_maps_every_colour_one_to_one():
    """recolor_items maps a widget's colour by value: in every theme,
    foreground keys sharing a value must share it in every other theme too."""
    for old in THEMES.values():
        for new in THEMES.values():
            seen = {}
            for key, value in old.colors.items():
                if key in tmod.BACKGROUND_KEYS:
                    continue
                target = seen.setdefault(tuple(value), (key, new.colors[key]))
                assert target[1] == new.colors[key],                     f"{old.name}->{new.name}: {target[0]} and {key} share a colour"


def test_open_windows_follow_a_theme_switch():
    import dearpygui.dearpygui as dpg
    old, new = THEMES["dark"], THEMES["light"]
    dpg.create_context()
    try:
        with dpg.window():
            title = dpg.add_text("Preferences", color=old.get("dialog_title"))
            plain = dpg.add_text("plain")
        tmod.ThemeManager.recolor_items(old, new)
        color = [round(v * 255) for v in dpg.get_item_configuration(title)["color"]]
        assert tuple(color) == tuple(new.get("dialog_title"))
        assert dpg.get_item_configuration(plain)["color"][0] < 0       # untouched
    finally:
        dpg.destroy_context()


@pytest.mark.parametrize("tid", sorted(THEMES))
def test_kinds_of_nodes_and_states_are_distinguishable(tid):
    t = THEMES[tid]
    assert t.get("node_data_bg") != t.get("node_proc_bg")
    assert contrast(t.get("node_incomplete_outline"), t.get("node_editor_bg")) >= 3.0
    assert contrast(t.get("node_highlight"), t.get("node_editor_bg")) >= 1.8
    assert contrast(t.get("link_default"), t.get("node_editor_bg")) >= 3.0
    for pin in ("node_pin", "node_ref_pin"):                 # orange / yellow pins
        for body in ("node_data_bg", "node_proc_bg"):
            assert contrast(t.get(pin), t.get(body)) >= 3.0, (pin, body)
    assert t.get("node_pin") != t.get("node_ref_pin")
    assert len({t.get(k)[:3] for k in ("text_success", "text_error",
                                       "monitor_status_waiting")}) == 3
