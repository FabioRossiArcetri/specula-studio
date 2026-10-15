"""
Minimal stand-in for ``dearpygui.dearpygui`` used to drive UI-coupled code
paths in tests without a GPU / display.

Every ``add_*`` call and every context-manager item returns a fresh integer
id.  Item positions are remembered so code that reads them back sees what it
wrote.  Frame callbacks run immediately.  Anything not explicitly modelled is
a no-op returning ``None`` (or a fresh id for ``mv*`` constants).
"""

from __future__ import annotations

import contextlib
import itertools
import types

_ids = itertools.count(10_000)


class _FakeDPG(types.ModuleType):
    def __init__(self):
        super().__init__("dearpygui.dearpygui")
        self._pos: dict = {}
        self._values: dict = {}
        self._aliases: set = set()

    # ── item creation ─────────────────────────────────────────────────────────
    def _new_id(self, kwargs) -> int:
        tag = kwargs.get("tag") or kwargs.get("id")
        if tag is not None:
            self._aliases.add(tag)
            return tag
        return next(_ids)

    def _ctx(self, *args, **kwargs):
        item = self._new_id(kwargs)

        @contextlib.contextmanager
        def cm():
            yield item

        return cm()

    def __getattr__(self, name):
        if name.startswith("mv"):
            value = next(_ids)
            setattr(self, name, value)
            return value
        if name.startswith("add_"):
            return lambda *a, **k: self._new_id(k)
        if name in {
            "node", "node_attribute", "group", "window", "child_window",
            "theme", "theme_component", "tooltip", "menu", "menu_bar",
            "font_registry", "handler_registry", "item_handler_registry",
            "file_dialog", "node_editor", "plot", "texture_registry",
            "table", "table_row", "collapsing_header", "tab_bar", "tab",
            "popup", "value_registry", "stage", "plot_axis",
        }:
            return self._ctx
        return lambda *a, **k: None

    # ── queried state ─────────────────────────────────────────────────────────
    def does_item_exist(self, item):
        return True

    def set_item_pos(self, item, pos):
        self._pos[item] = list(pos)

    def get_item_pos(self, item):
        return list(self._pos.get(item, [100, 100]))

    def get_item_rect_size(self, item):
        return [200, 120]

    def set_value(self, item, value):
        self._values[item] = value

    def get_value(self, item):
        return self._values.get(item, "")

    def get_frame_count(self):
        return 0

    def set_frame_callback(self, frame, callback, *a, **k):
        callback()

    def get_selected_nodes(self, editor):
        return []

    def get_selected_links(self, editor):
        return []

    def is_item_hovered(self, item):
        return False

    def is_item_visible(self, item):
        return False

    def get_item_children(self, item, slot=None):
        return [] if slot is not None else {}

    def get_viewport_width(self):
        return 1600

    def get_viewport_height(self):
        return 900

    def is_dearpygui_running(self):
        return False

    def get_item_width(self, item):
        return 400

    def get_item_height(self, item):
        return 300

    def is_item_shown(self, item):
        return False

    def is_any_item_active(self):
        return False

    def is_key_down(self, key):
        return False

    def get_item_alias(self, item):
        return item if isinstance(item, str) else ""

    def get_item_type(self, item):
        return "mvAppItemType::mvText"


def install():
    """Insert the fake module into ``sys.modules`` and return it."""
    import sys

    fake = _FakeDPG()
    pkg = types.ModuleType("dearpygui")
    pkg.dearpygui = fake
    sys.modules["dearpygui"] = pkg
    sys.modules["dearpygui.dearpygui"] = fake
    return fake
