"""View -> Log window.

Mixin of :class:`specula_studio.app.SpeculaEditor`; it relies on the editor's
attributes (``nm``, ``fh``, ``preferences``, ``theme_manager``, ...).
"""

import logging

import dearpygui.dearpygui as dpg

from ..logs import LEVELS, ring_buffer, set_level


log = logging.getLogger(__name__)


class LogWindowMixin:
    def _show_log_window(self):
        tag = "log_window"
        if dpg.does_item_exist(tag):
            dpg.show_item(tag)
            dpg.focus_item(tag)
            return
        current = logging.getLevelName(logging.getLogger("specula_studio").level)
        with dpg.window(label="Log", tag=tag, width=900, height=420):
            with dpg.group(horizontal=True):
                dpg.add_text("Show:")
                dpg.add_combo(LEVELS, default_value="INFO", width=110,
                              tag="log_window_filter",
                              callback=lambda: self._refresh_log_window(force=True))
                dpg.add_spacer(width=20)
                dpg.add_text("Record level:")
                dpg.add_combo(LEVELS, default_value=current, width=110,
                              callback=self._on_log_level_changed)
                dpg.add_button(label="Clear", callback=self._on_log_clear)
            dpg.add_input_text(tag="log_window_text", multiline=True, readonly=True,
                               width=-1, height=-1)
        self._refresh_log_window(force=True)

    def _on_log_level_changed(self, sender, app_data):
        set_level(app_data)
        self.preferences['log_level'] = app_data
        self._save_settings()

    def _on_log_clear(self):
        ring = ring_buffer()
        if ring is not None:
            ring.clear()
        self._refresh_log_window(force=True)

    def _refresh_log_window(self, force: bool = False):
        ring = ring_buffer()
        if ring is None or not dpg.does_item_exist("log_window_text"):
            return
        if not force and ring.version == self._log_window_version:
            return
        if not force and not dpg.is_item_shown("log_window"):
            return
        self._log_window_version = ring.version
        min_level = getattr(logging, dpg.get_value("log_window_filter") or "INFO")
        dpg.set_value("log_window_text", "\n".join(ring.lines(min_level)[-1000:]))
