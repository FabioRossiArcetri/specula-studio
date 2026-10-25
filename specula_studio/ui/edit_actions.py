"""
Edit actions: undo/redo, copy/paste/duplicate, keyboard shortcuts and the
"unsaved changes" flag.

Mixin of :class:`specula_studio.app.SpeculaEditor`.
"""

import json
import logging
import time

import dearpygui.dearpygui as dpg

from ..core import clipboard
from ..core.history import History, fingerprint

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.3


class EditMixin:
    # ── state ─────────────────────────────────────────────────────────────────

    def _init_edit_state(self):
        self.history = History()
        self._clipboard = None
        self._saved_fingerprint = None
        self._dirty = False
        self._last_poll = 0.0

    def _capture_state(self) -> dict:
        return {"graph": self.graph.snapshot(), "positions": self.nm.current_positions()}

    def _mark_clean(self):
        """Current graph = saved file: reset history and the dirty flag."""
        self._restart_history()
        self._saved_fingerprint = self._document_fingerprint()
        self._set_dirty(False)

    def _mark_saved(self):
        self._saved_fingerprint = self._document_fingerprint()
        self._set_dirty(False)

    def _restart_history(self):
        """Start the undo history from the current graph."""
        self.history.reset(self._capture_state(), fingerprint(self.graph))

    def _document_fingerprint(self) -> str:
        """What saving would write: the graph (during an override preview, the
        base with the edits folded in) and the list of override files.  The
        graph shown in a preview is not the document: toggling an override
        changes only the list."""
        om = self.override_manager
        if om.has_base_snapshot():
            data = om.rebased(self._current_export())
        else:
            data = self.fh.export_to_yaml_dict(
                include_defaults=self.preferences.get("include_defaults", False))
        data = {name: ({k: v for k, v in obj.items() if k != "gui_pos"}
                       if isinstance(obj, dict) else obj) for name, obj in data.items()}
        return json.dumps([data, om.to_dict()], default=str)

    def is_dirty(self) -> bool:
        return self._document_fingerprint() != self._saved_fingerprint

    def _set_dirty(self, dirty: bool):
        if dirty != self._dirty:
            self._dirty = dirty
            self._update_status_bar()

    def _poll_history(self, force: bool = False):
        """Record a history entry when the model changed (called every frame)."""
        now = time.monotonic()
        if not force:
            if now - self._last_poll < _POLL_INTERVAL_S:
                return
            # Do not snapshot in the middle of a drag / click.
            if dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
                return
        self._last_poll = now
        fp = fingerprint(self.graph)
        if self.history.record(self._capture_state(), fp):
            log.debug("History: recorded change")
            if dpg.does_item_exist("issues_window") and dpg.is_item_shown("issues_window"):
                self._refresh_issues()
        self._set_dirty(self._document_fingerprint() != self._saved_fingerprint)

    # ── undo / redo ───────────────────────────────────────────────────────────

    def undo(self, *_):
        self._poll_history(force=True)       # include a change made just now
        state = self.history.undo()
        if state is None:
            log.info("Nothing to undo")
            return
        self._restore_state(state)

    def redo(self, *_):
        state = self.history.redo()
        if state is None:
            log.info("Nothing to redo")
            return
        self._restore_state(state)

    def _restore_state(self, state: dict):
        self.graph.restore(state["graph"])
        self.nm._hide_property_panel()
        self.nm.sync_ui_from_graph(state["positions"])
        self._set_dirty(self._document_fingerprint() != self._saved_fingerprint)

    # ── clipboard ─────────────────────────────────────────────────────────────

    def copy_selection(self, *_):
        selected = self.nm.get_selected_nodes()
        if not selected:
            log.info("Copy: no node selected")
            return
        self.fh.capture_positions()
        self._clipboard = clipboard.copy_nodes(self.graph, selected)
        log.info("Copied %d object(s)", len(self._clipboard))

    def paste(self, *_):
        if not self._clipboard:
            log.info("Paste: clipboard is empty")
            return
        self._paste(self._clipboard)

    def duplicate_selection(self, *_):
        selected = self.nm.get_selected_nodes()
        if not selected:
            log.info("Duplicate: no node selected")
            return
        self.fh.capture_positions()
        self._paste(clipboard.copy_nodes(self.graph, selected))

    def _paste(self, clip):
        positions = self.nm.current_positions()
        new_uuids = clipboard.paste_nodes(self.graph, clip)
        self.nm.sync_ui_from_graph(positions)
        log.info("Pasted %d object(s)", len(new_uuids))
        self._poll_history(force=True)

    # ── keyboard ──────────────────────────────────────────────────────────────

    def _canvas_has_focus(self) -> bool:
        """
        Editing shortcuts act on the node canvas only, so typing in the
        property panel or a dialog can never delete nodes or links.
        """
        return (dpg.does_item_exist("specula_editor")
                and (dpg.is_item_hovered("specula_editor")
                     or dpg.is_item_hovered("specula_editor_parent")))

    @staticmethod
    def _modifier_down(*keys) -> bool:
        return any(dpg.is_key_down(k) for k in keys)

    def _on_key_press(self, sender, key):
        ctrl = self._modifier_down(dpg.mvKey_LControl, dpg.mvKey_RControl)
        shift = self._modifier_down(dpg.mvKey_LShift, dpg.mvKey_RShift)
        on_canvas = self._canvas_has_focus()

        if ctrl:
            if key == dpg.mvKey_F:
                self._show_find_object()
                return
            if key == dpg.mvKey_Spacebar:
                self._show_add_object_search()
                return
            if key == dpg.mvKey_S:
                self._on_save_simulation_clicked()
            elif key == dpg.mvKey_O:
                dpg.show_item("load_simulation_dialog")
            elif not on_canvas:
                return
            elif key == dpg.mvKey_Z:
                self.redo() if shift else self.undo()
            elif key == dpg.mvKey_Y:
                self.redo()
            elif key == dpg.mvKey_C:
                self.copy_selection()
            elif key == dpg.mvKey_V:
                self.paste()
            elif key == dpg.mvKey_D:
                self.duplicate_selection()
            return

        if not on_canvas:
            return
        if key == dpg.mvKey_Delete:
            self._on_delete_requested()
        elif key == dpg.mvKey_D:
            self.nm.delete_selected_link(sender, key)
        elif key == dpg.mvKey_H:
            self._show_help_for_selected()
