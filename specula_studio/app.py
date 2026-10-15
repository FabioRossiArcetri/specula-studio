"""
SPECULA Studio main window.

:class:`SpeculaEditor` owns the model (graph + templates), the node canvas
(:class:`~.node_manager.NodeManager`), file I/O and simulation control.  The
menus and dialogs are implemented in the mixins of :mod:`specula_studio.ui`.
"""

import argparse
import logging
import os
import pathlib

import dearpygui.dearpygui as dpg

from . import __version__, render_scale, settings
from .core.model import Graph
from .core.overrides import OverrideManager
from .core.templates import TEMPLATES_DIR, load_template_dir
from .file_handler import FileHandler, auto_layout_nodes
from .logs import LEVELS, setup_logging
from .node_manager import NodeManager
from .theme_manager import ThemeManager
from .ui.add_objects import AddObjectsMixin
from .ui.dialogs import DialogsMixin
from .ui.edit_actions import EditMixin
from .ui.log_window import LogWindowMixin
from .ui.overrides_menu import OverridesMixin
from .ui.preferences import PreferencesMixin

log = logging.getLogger(__name__)


class SpeculaEditor(
    EditMixin,
    DialogsMixin,
    AddObjectsMixin,
    PreferencesMixin,
    OverridesMixin,
    LogWindowMixin,
):
    def __init__(self, yaml_folder=TEMPLATES_DIR):
        # 1. Templates (class metadata)
        self.data_obj_templates = load_template_dir(os.path.join(yaml_folder, 'data_objects'))
        self.proc_obj_templates = load_template_dir(os.path.join(yaml_folder, 'processing_objects'))
        self.all_templates = {**self.data_obj_templates, **self.proc_obj_templates}

        # 2. Model, canvas and file I/O
        self.graph = Graph(self.all_templates)
        self.nm = NodeManager(self.graph, self.all_templates)
        self.fh = FileHandler(self.nm)
        self.fh.editor = self          # give FileHandler access to override_manager

        # 3. Simulation control and overrides
        from .simulation_control import SimulationControl
        self.sim_control = SimulationControl(self)
        self.override_manager = OverrideManager()
        self._override_item_tags = []  # tracks dynamic override menu items

        # Current file
        self.current_simulation_name = None
        self.current_simulation_path = None

        # Items pending deletion (delete confirmation dialog)
        self.pending_deletion_type = None
        self.pending_deletion_items = []

        # 4. Persistent preferences (single settings file, see settings.py)
        self.preferences = settings.store()
        self._recent_menu_items: list = []
        self._log_window_version = -1
        self._init_edit_state()

        # 5. Apply render scale before any UI is built
        render_scale.set_size(self.preferences['render_size'])

        # 6. Build the UI
        self.create_ui()
        self._setup_custom_handlers()
        self._mark_clean()

    # ── Input handlers ───────────────────────────────────────────────────────

    def _setup_custom_handlers(self):
        """Mouse handlers of the node canvas (keyboard: EditMixin._on_key_press)."""
        with dpg.handler_registry():
            dpg.add_mouse_click_handler(callback=self.nm.on_click_editor)
            dpg.add_mouse_double_click_handler(callback=self.nm._on_canvas_double_click)
            dpg.add_mouse_move_handler(callback=self.nm._on_mouse_move)

    # ── Status bar / title ───────────────────────────────────────────────────

    def _update_status_bar(self):
        name = self.current_simulation_name or "(Unsaved)"
        marker = " *" if getattr(self, "_dirty", False) else ""
        if dpg.does_item_exist("status_bar_text"):
            dpg.set_value("status_bar_text", f"Simulation: {name}{marker}")
        try:
            dpg.set_viewport_title(f"SPECULA Studio - {name}{marker}")
        except Exception:
            pass   # no viewport yet

    # ── Recent files management ────────────────────────────────────────────────

    def _add_to_recent_files(self, file_path: str):
        """Move *file_path* to the top of the recent-files list."""
        if not file_path:
            return
        self.preferences.add_recent_file(file_path)
        self._refresh_recent_files_menu()

    def _refresh_recent_files_menu(self):
        """Rebuild the Load Recent submenu with current recent files."""
        if not dpg.does_item_exist("load_recent_submenu"):
            return

        for item in self._recent_menu_items:
            if dpg.does_item_exist(item):
                dpg.delete_item(item)
        self._recent_menu_items.clear()

        recent_files = self.preferences.get('recent_files', [])
        if not recent_files:
            dpg.hide_item("load_recent_submenu")
            return

        dpg.show_item("load_recent_submenu")
        for afile_path in recent_files:
            item = dpg.add_menu_item(
                label=pathlib.Path(afile_path).name,
                parent="load_recent_submenu",
                user_data=afile_path,
                callback=lambda s, a, u: self._on_load_recent_file(u),
            )
            with dpg.tooltip(parent=item):
                dpg.add_text(afile_path)
            self._recent_menu_items.append(item)

    def _on_load_recent_file(self, file_path: str):
        """Load a recent file by path."""
        if not file_path:
            return
        if not os.path.exists(file_path):
            self.preferences.remove_recent_file(file_path)
            self._refresh_recent_files_menu()
            log.warning("Recent file not found, removed from list: %s", file_path)
            return
        self._open_simulation(file_path)

    def create_ui(self):
        dpg.create_context()

        self.nm.init_themes()
        self.theme_manager = ThemeManager()
        self.theme_manager.apply_theme()

        self._font_handle = None
        font_path = self._font_path()

        if os.path.exists(font_path):
            with dpg.font_registry():
                current_fs = render_scale.font_size()
                self._font_handle = dpg.add_font(font_path, current_fs)
                log.info(f"[FONT] Loaded font from {font_path} at {current_fs} px (handle={self._font_handle})")
            dpg.bind_font(self._font_handle)
        else:
            log.warning(f"[FONT] Font path not found: {font_path}, using default")

        initial_scale = 1.0 # render_scale.global_font_scale()
        dpg.set_global_font_scale(initial_scale)
        log.info(f"[FONT] Initial global font scale: {initial_scale} ({self.preferences['render_size']})")

        # ── Main Window ───────────────────────────────────────────────────────
        with dpg.window(label="SPECULA Editor", tag='main_window', on_close=self._on_exit_requested):
            with dpg.menu_bar():
                with dpg.menu(label="File", tag="file_menu"):
                    dpg.add_menu_item(label="New Simulation",    callback=self._on_new_simulation_clicked)
                    dpg.add_menu_item(label="Load Simulation",   shortcut="Ctrl+O",
                                      callback=lambda: dpg.show_item("load_simulation_dialog"))

                    # Load Recent submenu
                    with dpg.menu(label="Load Recent", tag="load_recent_submenu"):
                        # Items will be added dynamically by _refresh_recent_files_menu()
                        pass

                    dpg.add_separator()
                    dpg.add_menu_item(label="Save Simulation",   shortcut="Ctrl+S",
                                      callback=self._on_save_simulation_clicked)
                    dpg.add_menu_item(label="Save Simulation As",callback=lambda: dpg.show_item("save_simulation_dialog"))
                    dpg.add_separator()
                    dpg.add_menu_item(label="Preferences",       callback=self._show_preferences_dialog)
                    dpg.add_menu_item(label="Exit",              callback=self._on_exit_requested)

                with dpg.menu(label="Edit"):
                    dpg.add_menu_item(label="Undo", shortcut="Ctrl+Z", callback=self.undo)
                    dpg.add_menu_item(label="Redo", shortcut="Ctrl+Y", callback=self.redo)
                    dpg.add_separator()
                    dpg.add_menu_item(label="Copy", shortcut="Ctrl+C", callback=self.copy_selection)
                    dpg.add_menu_item(label="Paste", shortcut="Ctrl+V", callback=self.paste)
                    dpg.add_menu_item(label="Duplicate", shortcut="Ctrl+D",
                                      callback=self.duplicate_selection)
                    dpg.add_separator()
                    dpg.add_menu_item(label="Delete Selection", shortcut="Del",
                                      callback=self._on_delete_requested)

                with dpg.menu(label="Add Objects"):
                    dpg.add_menu_item(label="Add Multiple Objects", callback=self._show_add_multiple_dialog)
                    with dpg.menu(label="Processing Objects"):
                        for node_type in sorted(self.proc_obj_templates.keys()):
                            dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)

                    with dpg.menu(label="Data Objects"):
                        for node_type in sorted(self.data_obj_templates.keys()):
                            dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)

                with dpg.menu(label="Overrides", tag="overrides_menu"):
                    dpg.add_menu_item(label="Load Override File(s)",
                                      callback=self._show_load_overrides_dialog,
                                      tag="mi_load_overrides")
                    dpg.add_separator(tag="overrides_sep_top")
                    # ↑ dynamic per-override toggle items are inserted here ↑
                    dpg.add_separator(tag="overrides_sep_bottom")
                    dpg.add_menu_item(label="Remove...",
                                      callback=self._show_remove_override_menu,
                                      tag="remove_override_menu")

                with dpg.menu(label="Simulation"):
                    dpg.add_menu_item(label="Control Panel", callback=lambda: self.sim_control.show_control_window())
                    dpg.add_menu_item(label="Display Yaml", callback=lambda: self.sim_control.show_yaml_window())

                with dpg.menu(label="Layout"):
                    dpg.add_menu_item(label="Auto Layout",
                                      callback=lambda: auto_layout_nodes(self.nm.graph, self.nm.uuid_to_dpg))
                    dpg.add_menu_item(label="Debug Info",
                                      callback=lambda: log.info(f"Nodes: {len(self.nm.graph.nodes)}, "
                                                             f"Connections: {len(self.nm.graph.connections)}"))
                with dpg.menu(label="View"):
                    dpg.add_menu_item(label="Log", callback=self._show_log_window)
                with dpg.menu(label="Help"):
                    dpg.add_menu_item(
                        label="Selected Object          [H]",
                        tag="help_menu_item_selected",
                        callback=self._show_help_for_selected,
                    )
                    dpg.add_separator()
                    dpg.add_menu_item(
                        label="About SPECULA Studio",
                        callback=self._show_about_dialog,
                    )

            with dpg.group(horizontal=False):
                with dpg.group(horizontal=True, tag="editor_group"):
                    with dpg.child_window(width=-1, tag="specula_editor_parent", border=False):
                        with dpg.node_editor(
                            tag="specula_editor",
                            callback=self.nm.link_callback,
                            delink_callback=self.nm.delink_callback,
                            minimap=True
                        ):
                            pass

                    with dpg.child_window(width=0, tag="property_panel", border=True, show=False):
                        pass

                with dpg.child_window(height=30, tag="status_bar", border=False):
                    dpg.add_text(
                        "Simulation: (Unsaved)",
                        tag="status_bar_text",
                        color=self.theme_manager.get_color("status_text")
                    )


        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key_press)

        dpg.create_viewport(title="SPECULA Studio", width=1600, height=900)
        dpg.set_viewport_resize_callback(self._resize_callback)

        self.setup_dialogs()


        # Initialize the recent files menu
        self._refresh_recent_files_menu()

        self._show_startup_dialog()

        dpg.setup_dearpygui()
        dpg.show_viewport()

        self.nm.after_dpg_init()
        dpg.set_primary_window('main_window', True)

    def _resize_callback(self):
        h = dpg.get_viewport_height()
        new_height = h - 80

        # Only resize editor if panel is not visible
        if dpg.does_item_exist("property_panel") and not dpg.is_item_shown("property_panel"):
            dpg.set_item_width("specula_editor_parent", -1)

        dpg.set_item_height("specula_editor_parent", new_height)
        dpg.set_item_height("property_panel", new_height)


    # ── File menu ────────────────────────────────────────────────────────────

    def _on_save_simulation_clicked(self):
        if self.current_simulation_path:
            self._save_to(self.current_simulation_path)
        else:
            dpg.show_item("save_simulation_dialog")

    def _save_simulation_cb(self, s, a):
        self._save_to(a['file_path_name'])

    def _load_simulation_cb(self, s, a):
        self._open_simulation(a['file_path_name'])

    # ── Shared load / save helpers ────────────────────────────────────────────

    def _save_to(self, path: str) -> None:
        """Save the current graph to *path* and make it the current file.

        While an override preview is active the file receives the *base*
        simulation (with the user's edits folded in), never values that only
        come from an override.
        """
        if self.override_manager.has_base_snapshot():
            base = self.override_manager.commit_edits(self._current_export())
            self.fh.export_simulation(path, yaml_data=self.fh._add_overrides_metadata(base))
            log.info("Saved base simulation (overrides kept separate) to %s", path)
        else:
            self.fh.save_simulation(path, self.preferences['include_defaults'])
        self.current_simulation_path = path
        self.current_simulation_name = pathlib.Path(path).stem
        self._mark_saved()
        self._update_status_bar()
        self._add_to_recent_files(path)

    def _open_simulation(self, path: str) -> None:
        """Load *path* into the editor and make it the current file."""
        path = str(pathlib.Path(path).resolve())
        # Overrides belong to a simulation file: start from a clean registry,
        # then restore the ones recorded in the file and re-apply them.
        self.override_manager = OverrideManager()
        if not self.fh.load_simulation(path):
            return
        if self.override_manager.any_enabled():
            self._apply_overrides_to_simulation()
        self._refresh_overrides_menu()
        self.current_simulation_path = path
        self.current_simulation_name = pathlib.Path(path).stem
        self._mark_clean()
        self._update_status_bar()
        self._add_to_recent_files(path)
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")

    def run(self):
        # Cache the bridge reference once — avoids a module lookup every frame.
        try:
            from .matplotlib_dpg_bridge import MatplotlibDPGBridge as _MplBridge
        except ImportError:
            _MplBridge = None

        try:
            self.nm.start_periodic_tasks()
            dpg.set_viewport_resize_callback(None)

            while dpg.is_dearpygui_running():
                self.nm.monitors.tick()
                if _MplBridge is not None:
                    _MplBridge.tick()
                self._refresh_log_window()
                self._poll_history()
                dpg.render_dearpygui_frame()
        finally:
            self.shutdown()
            dpg.destroy_context()

    def shutdown(self):
        """Stop a running simulation and close monitor windows/processes."""
        try:
            if self.sim_control.is_running:
                self.sim_control.abort_sim()
        except Exception:
            log.exception("Error while aborting the simulation on exit")
        try:
            self.nm.cleanup()
        except Exception:
            log.exception("Error while closing monitors on exit")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="specula-studio",
        description="Node-based visual editor for SPECULA AO simulations.",
    )
    parser.add_argument("--log-level", choices=LEVELS, default=None,
                        help="Logging verbosity (default: saved preference, INFO)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Shortcut for --log-level DEBUG")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    level = "DEBUG" if args.verbose else (
        args.log_level or settings.store().get("log_level", "INFO"))
    setup_logging(level)
    editor = SpeculaEditor(yaml_folder=str(TEMPLATES_DIR))
    editor.run()


if __name__ == "__main__":
    main()
