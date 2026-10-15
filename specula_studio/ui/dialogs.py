"""Confirmation, startup, about and help dialogs.

Mixin of :class:`specula_studio.app.SpeculaEditor`; it relies on the editor's
attributes (``nm``, ``fh``, ``preferences``, ``theme_manager``, ...).
"""

import logging

import dearpygui.dearpygui as dpg

from ..core.overrides import OverrideManager

try:
    from ..help_window import show_help_window
    _HELP_AVAILABLE = True
except ImportError:
    _HELP_AVAILABLE = False

    def show_help_window(*_):
        pass


log = logging.getLogger(__name__)


class DialogsMixin:
    def _center_dialog(self, dialog_tag):
        """Center a dialog window on the viewport."""
        if dpg.does_item_exist(dialog_tag):
            try:
                viewport_width  = dpg.get_viewport_width()
                viewport_height = dpg.get_viewport_height()
                dialog_width    = dpg.get_item_width(dialog_tag)
                dialog_height   = dpg.get_item_height(dialog_tag)
                center_x = (viewport_width  - dialog_width)  // 2
                center_y = (viewport_height - dialog_height) // 2
                dpg.set_item_pos(dialog_tag, [center_x, center_y])
            except SystemError:
                pass

    def _on_new_simulation_clicked(self):
        if self.current_simulation_name is None or not self.is_dirty():
            self._show_startup_dialog()
        else:
            self._center_dialog("new_simulation_confirmation_dialog")
            dpg.show_item("new_simulation_confirmation_dialog")

    def _on_new_simulation_save_and_proceed(self):
        if self.current_simulation_path:
            self._save_to(self.current_simulation_path)
        else:
            dpg.hide_item("new_simulation_confirmation_dialog")
            self._center_dialog("save_before_new_dialog")
            dpg.show_item("save_before_new_dialog")
            return
        dpg.hide_item("new_simulation_confirmation_dialog")
        self._show_startup_dialog()

    def _on_save_before_new_cb(self, sender, app_data):
        self._save_to(app_data['file_path_name'])
        self._show_startup_dialog()

    def _on_new_simulation_discard(self):
        dpg.hide_item("new_simulation_confirmation_dialog")
        self._show_startup_dialog()

    def _on_new_simulation_cancel(self):
        dpg.hide_item("new_simulation_confirmation_dialog")

    def _on_delete_requested(self):
        if self.nm._selected_link_id:
            self.pending_deletion_items = [self.nm._selected_link_id]
            self.pending_deletion_type  = "link"
            self._show_delete_confirmation_dialog("Delete 1 connection?")
            return
        selected_nodes = self.nm.get_selected_nodes()
        if not selected_nodes:
            return
        self.pending_deletion_items = selected_nodes
        self.pending_deletion_type  = "nodes"
        self._show_delete_confirmation_dialog(f"Delete {len(selected_nodes)} node(s)?")

    def _show_delete_confirmation_dialog(self, message):
        if dpg.does_item_exist("delete_confirmation_dialog"):
            dpg.delete_item("delete_confirmation_dialog")
        with dpg.window(
            label="Confirm Deletion", tag="delete_confirmation_dialog",
            modal=True, show=True, width=400, height=150, no_resize=True
        ):
            dpg.add_text(message)
            dpg.add_spacer()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Delete", width=100, callback=self._on_delete_confirm)
                dpg.add_button(label="Cancel", width=100, callback=self._on_delete_cancel)
        self._center_dialog("delete_confirmation_dialog")

    def _on_delete_confirm(self):
        dpg.hide_item("delete_confirmation_dialog")
        if self.pending_deletion_type == "nodes":
            for node_uuid in self.pending_deletion_items:
                self.nm.delete_node(node_uuid)
        elif self.pending_deletion_type == "link":
            for link_id in self.pending_deletion_items:
                self.nm.delink_callback(None, link_id)
        self.pending_deletion_items = []
        self.pending_deletion_type  = None

    def _on_delete_cancel(self):
        dpg.hide_item("delete_confirmation_dialog")
        self.pending_deletion_items = []
        self.pending_deletion_type  = None

    def _on_exit_requested(self):
        if not self.is_dirty():
            dpg.stop_dearpygui()
            return
        self._center_dialog("exit_confirmation_dialog")
        dpg.show_item("exit_confirmation_dialog")

    def _on_exit_confirm(self):
        dpg.hide_item("exit_confirmation_dialog")
        dpg.stop_dearpygui()

    def _on_exit_save_and_confirm(self):
        if self.current_simulation_path:
            self._save_to(self.current_simulation_path)
            dpg.stop_dearpygui()
        else:
            dpg.hide_item("exit_confirmation_dialog")
            self._center_dialog("save_and_exit_dialog")
            dpg.show_item("save_and_exit_dialog")

    def _on_save_and_exit_cb(self, sender, app_data):
        self._save_to(app_data['file_path_name'])
        dpg.stop_dearpygui()

    def _on_exit_cancel(self):
        dpg.hide_item("exit_confirmation_dialog")

    def setup_dialogs(self):
        with dpg.file_dialog(label="Save Simulation", show=False, callback=self._save_simulation_cb,
                             id="save_simulation_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")
        with dpg.file_dialog(label="Load Simulation", show=False, callback=self._load_simulation_cb,
                             id="load_simulation_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")
        with dpg.file_dialog(label="Save Simulation Before Exit", show=False, callback=self._on_save_and_exit_cb,
                             id="save_and_exit_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")
        with dpg.file_dialog(label="Save Simulation Before New", show=False, callback=self._on_save_before_new_cb,
                             id="save_before_new_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")
        self._create_exit_confirmation_dialog()
        self._create_new_simulation_confirmation_dialog()
        self._setup_add_multiple_dialog()

    def _create_exit_confirmation_dialog(self):
        with dpg.window(label="Confirm Exit", tag="exit_confirmation_dialog",
                        modal=True, show=False, width=450, height=180, no_resize=True):
            dpg.add_text("Are you sure you want to exit?")
            dpg.add_text(
                "Would you like to save your current simulation before exiting?",
                color=self.theme_manager.get_color("text_muted")
            )
            dpg.add_spacer()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save and Exit",      width=140, callback=self._on_exit_save_and_confirm)
                dpg.add_button(label="Exit without Saving",width=140, callback=self._on_exit_confirm)
                dpg.add_button(label="Cancel",             width=100, callback=self._on_exit_cancel)

    def _create_new_simulation_confirmation_dialog(self):
        with dpg.window(label="Create New Simulation?", tag="new_simulation_confirmation_dialog",
                        modal=True, show=False, width=450, height=180, no_resize=True):
            dpg.add_text("Create a new simulation?")
            dpg.add_text(
                "Your current simulation will be cleared. Would you like to save it first?",
                color=self.theme_manager.get_color("text_muted")
            )
            dpg.add_spacer()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save and Continue", width=140, callback=self._on_new_simulation_save_and_proceed)
                dpg.add_button(label="Discard",           width=100, callback=self._on_new_simulation_discard)
                dpg.add_button(label="Cancel",            width=100, callback=self._on_new_simulation_cancel)

    def _show_startup_dialog(self):
        if dpg.does_item_exist("startup_dialog"):
            dpg.set_value("startup_simulation_name", "")
            self._center_dialog("startup_dialog")
            dpg.show_item("startup_dialog")
        else:
            self._create_startup_dialog()

    def _on_startup_open_existing(self, sender, app_data):
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")
        dpg.show_item("load_simulation_dialog")

    def _create_startup_dialog(self):
        if dpg.does_item_exist("startup_dialog"):
            self._center_dialog("startup_dialog")
            dpg.show_item("startup_dialog")
            return
        with dpg.window(label="Welcome", tag="startup_dialog", modal=True, show=True, width=640, height=160):
            dpg.add_text("Create a new simulation or open an existing one.")
            dpg.add_spacer()
            with dpg.group(horizontal=True):
                dpg.add_text("Simulation name:")
                dpg.add_input_text(tag="startup_simulation_name", width=420,
                                   hint="Enter simulation name for new/import")
            dpg.add_spacer()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Create New Simulation",    callback=self._startup_create_new)
                dpg.add_button(label="Open Existing Simulation", callback=self._on_startup_open_existing)
                dpg.add_button(label="Cancel", callback=lambda s, a: dpg.hide_item("startup_dialog"))
        self._center_dialog("startup_dialog")

    def _startup_create_new(self, sender, app_data):
        name = dpg.get_value("startup_simulation_name").strip() if dpg.does_item_exist("startup_simulation_name") else ""
        if not name:
            if dpg.does_item_exist("startup_simulation_name"):
                dpg.set_value("startup_simulation_name", "")
                dpg.focus_item("startup_simulation_name")
            log.info("Please enter a simulation name before creating a new simulation.")
            return
        self.nm.clear_all()
        self.nm.graph.clear()
        self.override_manager = OverrideManager()
        self._refresh_overrides_menu()
        self.current_simulation_name = name
        self.current_simulation_path = None
        self._mark_clean()
        log.info(f"[SIMULATION] Created new simulation: {name}")
        self._update_status_bar()
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")

    def _show_about_dialog(self):
        tag = "about_specula_dialog"
        if dpg.does_item_exist(tag):
            dpg.show_item(tag)
            self._center_dialog(tag)
            return
        with dpg.window(
            label="About SPECULA Studio", tag=tag,
            modal=True, show=True, width=440, height=200, no_resize=True,
        ):
            dpg.add_text(
                "SPECULA Studio",
                color=self.theme_manager.get_color("accent")
            )
            dpg.add_text(
                "Visual node editor for the SPECULA AO simulation framework.",
                color=self.theme_manager.get_color("text_secondary"),
                wrap=400
            )
            dpg.add_spacer(height=8)
            dpg.add_text(
                "Documentation is extracted live from the installed SPECULA package.",
                color=self.theme_manager.get_color("text_hint"),
                wrap=400
            )
            dpg.add_spacer(height=12)
            dpg.add_button(
                label="Close", width=-1,
                callback=lambda: dpg.hide_item(tag),
            )
        self._center_dialog(tag)

    def _show_help_for_selected(self):
        """
        Show the full-help popup for the currently selected node (H key / menu).

        If exactly one node is selected its class and instance name are used.
        If no node is selected but the property panel is showing a node, use that.
        """
        if not _HELP_AVAILABLE:
            log.info("[HELP] help_window.py or help_provider.py not available.")
            return

        # Prefer the node whose panel is currently displayed
        selected = self.nm.get_selected_nodes()
        node_uuid = selected[0] if len(selected) == 1 else None

        # Fallback: if the property panel is visible it tracks the last selection
        if node_uuid is None and hasattr(self.nm, "_last_selected_node"):
            node_uuid = self.nm._last_selected_node

        if node_uuid is None or node_uuid not in self.nm.graph.nodes:
            log.info("[HELP] No node selected — select a node first.")
            return

        node_data  = self.nm.graph.nodes[node_uuid]
        class_name = node_data.get("type", "")
        node_name  = node_data.get("name", class_name)

        if not class_name:
            log.info("[HELP] Selected node has no type information.")
            return

        show_help_window(class_name, node_name)
