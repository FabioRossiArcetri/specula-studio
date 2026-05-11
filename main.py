import dearpygui.dearpygui as dpg
import json
import os
import pathlib
import yaml
from collections import OrderedDict

import render_scale
from constants import DEFAULT_AUTO_SIMUL_PARAMS, DEFAULT_RENDER_SIZE
from node_manager import NodeManager
from file_handler import FileHandler, auto_layout_nodes
from graph_manager import GraphManager
import dpg_utils
from override_manager import OverrideManager

# Font path via matplotlib
import matplotlib
FONT_PATH = matplotlib.get_data_path() + '/fonts/ttf/DejaVuSerif.ttf'

# Persistent settings file (lives in the user's home directory)
_SETTINGS_PATH = pathlib.Path.home() / ".specula_studio_settings.json"


# ── YAML helpers ──────────────────────────────────────────────────────────[...]

def ordered_load(stream, Loader=yaml.SafeLoader, object_pairs_hook=OrderedDict):
    class OrderedLoader(Loader):
        pass
    def construct_mapping(loader, node):
        loader.flatten_mapping(node)
        return object_pairs_hook(loader.construct_pairs(node))
    OrderedLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping)
    return yaml.load(stream, OrderedLoader)


# ── Main editor class ────────────────────────────────────────────────────────…[...]

class SpeculaEditor:
    def __init__(self, yaml_folder):
        # 1. Load Templates
        self.data_obj_templates = self.load_templates(os.path.join(yaml_folder, 'data_objects'))
        self.proc_obj_templates = self.load_templates(os.path.join(yaml_folder, 'processing_objects'))
        self.all_templates = {**self.data_obj_templates, **self.proc_obj_templates}
        
        # 2. Initialize Logic Layers
        self.graph = GraphManager(self.all_templates)
        self.nm = NodeManager(self.graph, self.all_templates)        
        self.fh = FileHandler(self.nm)

        # 3. Initialize Simulation Control
        from simulation_control import SimulationControl
        self.sim_control = SimulationControl(self)

        # 3b. Initialize Override Manager
        self.override_manager = OverrideManager()

        # Track current simulation name and path
        self.current_simulation_name = None
        self.current_simulation_path = None
        
        # Track items pending deletion
        self.pending_deletion_type = None
        self.pending_deletion_items = []

        # 4. Preferences (defaults)
        self.preferences = {
            'auto_simul_params': DEFAULT_AUTO_SIMUL_PARAMS,
            'include_defaults': False,
            'render_size': DEFAULT_RENDER_SIZE,
        }

        # 5. Load persisted settings (overrides defaults)
        self._load_settings()

        # 6. Apply render scale before any UI is built
        render_scale.set_size(self.preferences['render_size'])

        # 7. Setup UI
        self.create_ui()
        self._setup_custom_handlers()

    # ── Settings persistence ──────────────────────────────────────────────────

    def _load_settings(self):
        """Load preferences from the JSON settings file (if it exists)."""
        try:
            if _SETTINGS_PATH.exists():
                with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for key in self.preferences:
                    if key in saved:
                        self.preferences[key] = saved[key]
                print(f"[SETTINGS] Loaded from {_SETTINGS_PATH}")
        except Exception as e:
            print(f"[SETTINGS] Could not load settings: {e}")

    def _save_settings(self):
        """Persist current preferences to the JSON settings file."""
        try:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.preferences, f, indent=2)
            print(f"[SETTINGS] Saved to {_SETTINGS_PATH}")
        except Exception as e:
            print(f"[SETTINGS] Could not save settings: {e}")

    # ── Template loading ──────────────────────────────────────────────────────

    def load_templates(self, folder):
        templates = OrderedDict()
        if os.path.exists(folder):
            for file in os.listdir(folder):
                if file.endswith(".yml"):
                    with open(os.path.join(folder, file), 'r') as f:
                        data = ordered_load(f)
                        if data:
                            templates.update(data)
        return templates

    # ── Input handlers ───────────────────────────────────────────────────────……[...]

    def _setup_custom_handlers(self):
        """Register handlers without the automatic Delete key handler."""
        with dpg.handler_registry():
            dpg.add_mouse_click_handler(callback=self.nm.on_click_editor)
            dpg.add_key_press_handler(key=dpg.mvKey_D, callback=self.nm.delete_selected_link)
            dpg.add_mouse_double_click_handler(callback=self.nm._on_canvas_double_click)
            dpg.add_mouse_move_handler(callback=self.nm._on_mouse_move)
    
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
  
    # ── Status Bar ─────────────────────────────────────────────────────────[...]

    def _update_status_bar(self):
        if self.current_simulation_name:
            status_text = f"Simulation: {self.current_simulation_name}"
        else:
            status_text = "Simulation: (Unsaved)"
        if dpg.does_item_exist("status_bar_text"):
            dpg.set_value("status_bar_text", status_text)

    # ── New Simulation ───────────────────────────────────────────────────────……[...]

    def _on_new_simulation_clicked(self):
        if self.current_simulation_name is None:
            self._show_startup_dialog()
        else:
            self._center_dialog("new_simulation_confirmation_dialog")
            dpg.show_item("new_simulation_confirmation_dialog")

    def _on_new_simulation_save_and_proceed(self):
        if self.current_simulation_path:
            self.fh.save_simulation(self.current_simulation_path)
        else:
            dpg.hide_item("new_simulation_confirmation_dialog")
            self._center_dialog("save_before_new_dialog")
            dpg.show_item("save_before_new_dialog")
            return
        dpg.hide_item("new_simulation_confirmation_dialog")
        self._show_startup_dialog()

    def _on_save_before_new_cb(self, sender, app_data):
        path = app_data['file_path_name']
        self.fh.save_simulation(path)
        self.current_simulation_path = path
        self.current_simulation_name = pathlib.Path(path).stem
        self._update_status_bar()
        self._show_startup_dialog()

    def _on_new_simulation_discard(self):
        dpg.hide_item("new_simulation_confirmation_dialog")
        self._show_startup_dialog()

    def _on_new_simulation_cancel(self):
        dpg.hide_item("new_simulation_confirmation_dialog")

    # ── Delete Confirmation ───────────────────────────────────────────────────

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
            dpg.add_spacing(count=2)
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

    # ── Exit ───────────────────────────────────────────────────────────[...]
    
    def _on_exit_requested(self):
        self._center_dialog("exit_confirmation_dialog")
        dpg.show_item("exit_confirmation_dialog")
    
    def _on_exit_confirm(self):
        dpg.hide_item("exit_confirmation_dialog")
        dpg.stop_dearpygui()
    
    def _on_exit_save_and_confirm(self):
        if self.current_simulation_path:
            self.fh.save_simulation(self.current_simulation_path)
            dpg.stop_dearpygui()
        else:
            dpg.hide_item("exit_confirmation_dialog")
            self._center_dialog("save_and_exit_dialog")
            dpg.show_item("save_and_exit_dialog")
    
    def _on_save_and_exit_cb(self, sender, app_data):
        path = app_data['file_path_name']
        self.fh.save_simulation(path)
        self.current_simulation_path = path
        self.current_simulation_name = pathlib.Path(path).stem
        self._update_status_bar()
        dpg.stop_dearpygui()
    
    def _on_exit_cancel(self):
        dpg.hide_item("exit_confirmation_dialog")

    # ── Add Multiple Objects dialog ───────────────────────────────────────────

    def _mo_on_double_click(self, sender, app_data):
        clicked_id   = app_data[1]
        parent_id    = dpg.get_item_info(clicked_id)["parent"]
        alias        = dpg.get_item_alias(clicked_id)
        parent_alias = dpg.get_item_alias(parent_id)
        if alias == "_mo_proc_listbox" or parent_alias == "_mo_proc_listbox":
            self._mo_add_proc()
        elif alias == "_mo_data_listbox" or parent_alias == "_mo_data_listbox":
            self._mo_add_data()

    def _setup_add_multiple_dialog(self):
        self._multi_add_queue = []
        proc_types = sorted(self.proc_obj_templates.keys())
        data_types = sorted(self.data_obj_templates.keys())
        COL_W = 260

        if not dpg.does_item_exist("mo_double_click_handler"):
            with dpg.item_handler_registry(tag="mo_double_click_handler"):
                dpg.add_item_double_clicked_handler(callback=self._mo_on_double_click)
                        
        with dpg.window(
            label="Add Multiple Objects", tag="add_multiple_dialog",
            modal=True, show=False, width=1000, height=550,
            no_resize=True, on_close=self._on_add_multiple_close,
        ):
            dpg.add_text(
                "Select items from the lists, use the arrows to stage them, then click Confirm.",
                color=[180, 180, 180],
            )
            dpg.add_separator()
            dpg.add_spacer(height=4)
            with dpg.group(horizontal=True):
                with dpg.group(width=COL_W):
                    dpg.add_text("Processing Objects")
                    dpg.add_listbox(items=proc_types, tag="_mo_proc_listbox", num_items=16, width=COL_W)
                    dpg.bind_item_handler_registry("_mo_proc_listbox", "mo_double_click_handler")
                dpg.add_spacer(width=8)
                with dpg.group(width=COL_W):
                    dpg.add_text("Data Objects")
                    dpg.add_listbox(items=data_types, tag="_mo_data_listbox", num_items=16, width=COL_W)
                    dpg.bind_item_handler_registry("_mo_data_listbox", "mo_double_click_handler")
                dpg.add_spacer(width=8)
                with dpg.group(width=70):
                    dpg.add_spacer(height=90)
                    dpg.add_button(label="Add Proc →", width=70, callback=self._mo_add_proc)
                    dpg.add_spacer(height=12)
                    dpg.add_button(label="Add Data →", width=70, callback=self._mo_add_data)
                    dpg.add_spacer(height=12)
                    dpg.add_button(label="← Remove",  width=70, callback=self._mo_remove)
                dpg.add_spacer(width=8)
                with dpg.group(width=COL_W):
                    dpg.add_text("Staged to Add", color=[150, 255, 150])
                    dpg.add_listbox(items=[], tag="_mo_staged_listbox", num_items=16, width=COL_W)
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Confirm", tag="_mo_confirm_btn", width=160, callback=self._mo_confirm)
                dpg.add_spacer(width=8)
                dpg.add_button(label="Cancel", width=100, callback=self._mo_cancel)
                dpg.add_spacer(width=20)
                dpg.add_text("", tag="_mo_status_text", color=[200, 200, 100])

    def _show_add_multiple_dialog(self):
        self._multi_add_queue.clear()
        self._mo_refresh_staged()
        self._center_dialog("add_multiple_dialog")
        dpg.show_item("add_multiple_dialog")

    def _on_add_multiple_close(self):
        self._multi_add_queue.clear()

    def _mo_refresh_staged(self):
        dpg.configure_item("_mo_staged_listbox", items=list(self._multi_add_queue))
        count = len(self._multi_add_queue)
        dpg.configure_item("_mo_confirm_btn", label=f"Confirm  ({count} node{'s' if count != 1 else ''})")
        dpg.set_value("_mo_status_text", "")

    def _mo_add_from_listbox(self, listbox_tag: str):
        selected = dpg.get_value(listbox_tag)
        if selected and selected.strip():
            self._multi_add_queue.append(selected)
            self._mo_refresh_staged()

    def _mo_add_proc(self):  self._mo_add_from_listbox("_mo_proc_listbox")
    def _mo_add_data(self):  self._mo_add_from_listbox("_mo_data_listbox")

    def _mo_remove(self):
        selected = dpg.get_value("_mo_staged_listbox")
        if selected and selected in self._multi_add_queue:
            idx = len(self._multi_add_queue) - 1 - self._multi_add_queue[::-1].index(selected)
            self._multi_add_queue.pop(idx)
            self._mo_refresh_staged()

    def _mo_confirm(self):
        if not self._multi_add_queue:
            dpg.set_value("_mo_status_text", "Nothing staged.")
            return
        nodes_to_create = list(self._multi_add_queue)
        if self.preferences['auto_simul_params']:
            simul_params_nodes = [n for n in nodes_to_create if n == "SimulParams"]
            other_nodes        = [n for n in nodes_to_create if n != "SimulParams"]
            nodes_to_create    = simul_params_nodes + other_nodes
        created_uuids = []
        for node_type in nodes_to_create:
            node_uuid = self.nm.create_node(node_type=node_type)
            created_uuids.append((node_uuid, node_type))
        dpg.split_frame()
        dpg.split_frame()
        if self.preferences['auto_simul_params']:
            self._apply_auto_simul_params(created_uuids)
        print(f"[ADD_MULTIPLE] Created {len(self._multi_add_queue)} node(s): {self._multi_add_queue}")
        self._multi_add_queue.clear()
        dpg.hide_item("add_multiple_dialog")

    def _apply_auto_simul_params(self, created_uuids):
        simul_params_uuid = None
        for uuid, node_type in created_uuids:
            if node_type == "SimulParams":
                simul_params_uuid = uuid
                break
        if not simul_params_uuid:
            for uuid, node_data in self.nm.graph.nodes.items():
                if node_data.get("type") == "SimulParams":
                    simul_params_uuid = uuid
                    break
        if not simul_params_uuid:
            print("[AUTOSIMULPARAMS] No SimulParams node found, skipping auto-connection")
            return
        simul_params_name = self.nm.graph.nodes[simul_params_uuid].get("name", simul_params_uuid)
        for node_uuid, node_type in created_uuids:
            if node_type == "SimulParams":
                continue
            node_data       = self.nm.graph.nodes.get(node_uuid, {})
            template        = self.nm.all_templates.get(node_type, {})
            template_params = template.get("parameters", {})
            if "simul_params" in template_params:
                param_meta = template_params.get("simul_params", {})
                if param_meta.get("kind") == "reference":
                    ref_key = "simul_params_ref"
                    if not node_data.get("values", {}).get(ref_key):
                        success   = self.nm.manual_link(simul_params_uuid, "ref", node_uuid, ref_key)
                        node_name = node_data.get("name", node_uuid)
                        if success:
                            print(f"[AUTOSIMULPARAMS] Connected '{node_name}' ({node_type}) "
                                  f"to SimulParams '{simul_params_name}'")
                        else:
                            print(f"[AUTOSIMULPARAMS] Failed to connect {node_uuid} ({node_type}) to SimulParams")
                    else:
                        print(f"[AUTOSIMULPARAMS] Node {node_uuid} already has simul_param_ref connected")

    def _mo_cancel(self):
        self._multi_add_queue.clear()
        dpg.hide_item("add_multiple_dialog")

    def _on_key_press(self, sender, app_data):
        if app_data == dpg.mvKey_Delete:
            self._on_delete_requested()

    # ── Preferences Dialog ────────────────────────────────────────────────────

    def _show_preferences_dialog(self):
        if not dpg.does_item_exist("preferences_dialog"):
            self._create_preferences_dialog()
        else:
            dpg.set_value("pref_auto_simul_params_checkbox", self.preferences['auto_simul_params'])
            dpg.set_value("pref_include_defaults_checkbox",  self.preferences['include_defaults'])
            dpg.set_value("pref_render_size_radio",          self.preferences['render_size'])
        self._center_dialog("preferences_dialog")
        dpg.show_item("preferences_dialog")

    def _create_preferences_dialog(self):
        with dpg.window(
            label="Preferences", tag="preferences_dialog",
            modal=True, show=False, width=540, height=500, no_resize=True
        ):
            dpg.add_text("Preferences", color=[200, 200, 100])
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── Render Size ───────────────────────────────────────────────────
            dpg.add_text("Render Size", color=[100, 200, 255])
            dpg.add_radio_button(
                items=render_scale.RENDER_SIZES,
                tag="pref_render_size_radio",
                default_value=self.preferences['render_size'],
                horizontal=True,
                callback=self._on_render_size_changed,
            )
            dpg.add_text(
                "Controls font size and node dimensions.\n"
                "SMALL = 50 %,  MEDIUM = 100 %,  LARGE = 180 %.\n"
                "All text and nodes are updated immediately.",
                color=[150, 150, 150],
                wrap=490,
            )

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── AutoSimulParams ───────────────────────────────────────────────
            with dpg.group(horizontal=False):
                dpg.add_checkbox(
                    label="Auto Connect SimulParams (AutoSimulParams)",
                    tag="pref_auto_simul_params_checkbox",
                    default_value=self.preferences['auto_simul_params'],
                    callback=self._on_auto_simul_params_changed,
                )
                dpg.add_text(
                    "When enabled, newly added nodes with SimulParams reference\n"
                    "will automatically connect to the existing SimulParams node.",
                    color=[150, 150, 150], wrap=490,
                )
            
            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)
            
            # ── Include Defaults ──────────────────────────────────────────────
            with dpg.group(horizontal=False):
                dpg.add_checkbox(
                    label="Include Default Values in Saved Simulations",
                    tag="pref_include_defaults_checkbox",
                    default_value=self.preferences['include_defaults'],
                    callback=self._on_include_defaults_changed,
                )
                dpg.add_text(
                    "When enabled, default parameter values will be included\n"
                    "when saving simulations.",
                    color=[150, 150, 150], wrap=490,
                )
            
            dpg.add_spacer(height=20)
            dpg.add_separator()
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Close", width=100,
                               callback=lambda: dpg.hide_item("preferences_dialog"))

    # ── Preference callbacks ──────────────────────────────────────────────────

    def _on_render_size_changed(self, sender, app_data):
        """
        Called when the Render Size radio button changes.

        Font switching strategy
        -----------------------
        dpg.bind_font() has a known DearPyGui/ImGui limitation: switching to a
        *larger* font handle than the one active at setup_dearpygui() time can
        silently fail because the atlas glyph metrics were pre-computed for the
        smaller size.  The reliable cross-platform alternative is to load a
        single font at MEDIUM size and use dpg.set_global_font_scale() — an
        ImGui render-time scalar that works identically in both directions.

        Order of operations
        -------------------
        1.  Update the render_scale module (spacer widths etc.).
        2.  Apply the new global font scale immediately — affects ALL text.
        3.  Rebuild existing nodes so spacer widths match the new scale.
        4.  Persist the new preference.
        """
        new_size = app_data
        if new_size == self.preferences['render_size']:
            return

        self.preferences['render_size'] = new_size
        render_scale.set_size(new_size)

        # ── Font scale (works reliably in both directions) ─────────────────
        scale = render_scale.global_font_scale()
        dpg.set_global_font_scale(scale)
        print(f"[RENDER] Global font scale → {scale}  (size: {new_size})")

        # ── Node rebuild (spacer widths are baked in at creation time) ─────
        if self.nm.graph.nodes:
            self.nm.rebuild_all_nodes_ui()
            dpg.delete_item("property_panel", children_only=True)

        self._save_settings()
        print(f"[PREFERENCES] Render size set to: {new_size}")

    def _on_auto_simul_params_changed(self, sender, app_data):
        self.preferences['auto_simul_params'] = app_data
        self._save_settings()
        print(f"[PREFERENCES] AutoSimulParams set to: {app_data}")

    def _on_include_defaults_changed(self, sender, app_data):
        self.preferences['include_defaults'] = app_data
        self._save_settings()
        print(f"[PREFERENCES] Include Defaults set to: {app_data}")

    # ── UI creation ────────────────────────────────────────────────────────……[...]

    def create_ui(self):
        self.export_include_defaults = False
        dpg.create_context()

        # ── Themes ───────────────────────────────────────────────────────……[...]
        dpg_utils.set_zebra_theme()
        self.nm.init_themes()

        # ── Font ─────────────────────────────────────────────────────────……[...]
        # A SINGLE font is loaded at MEDIUM size (18 px).  Runtime font-size
        # changes are handled exclusively via dpg.set_global_font_scale(), which
        # is an ImGui render-time scalar and works correctly in BOTH directions
        # (scale-up and scale-down).
        #
        # Why not load three fonts and use bind_font()?
        # dpg.bind_font() has a known issue where switching to a font handle
        # that is LARGER than the font active when setup_dearpygui() was called
        # silently has no effect.  set_global_font_scale() has no such
        # restriction.
        self._font_handle = None
        if os.path.exists(FONT_PATH):
            with dpg.font_registry():
                medium_fs = render_scale.SCALE_DEFS["MEDIUM"]["font_size"]  # 18
                self._font_handle = dpg.add_font(FONT_PATH, medium_fs)
                print(f"[FONT] Loaded DejaVuSerif at {medium_fs} px (handle={self._font_handle})")
            dpg.bind_font(self._font_handle)

        # Apply the scale matching the saved/default preference BEFORE any
        # widgets are rendered (setup_dearpygui has not been called yet here,
        # but the scale is stored in ImGui's IO and takes effect immediately).
        initial_scale = render_scale.global_font_scale()
        dpg.set_global_font_scale(initial_scale)
        print(f"[FONT] Initial global font scale: {initial_scale} ({self.preferences['render_size']})")

        # ── Main Window ───────────────────────────────────────────────────────
        with dpg.window(label="SPECULA Editor", tag='main_window', on_close=self._on_exit_requested):
            with dpg.menu_bar():
                with dpg.menu(label="File"):
                    dpg.add_menu_item(label="New Simulation",    callback=self._on_new_simulation_clicked)
                    dpg.add_menu_item(label="Load Simulation",   callback=lambda: dpg.show_item("load_simulation_dialog"))
                    dpg.add_separator()
                    dpg.add_menu_item(label="Save Simulation",   callback=self._on_save_simulation_clicked)
                    dpg.add_menu_item(label="Save Simulation As",callback=lambda: dpg.show_item("save_simulation_dialog"))
                    dpg.add_separator()
                    dpg.add_menu_item(label="Preferences",       callback=self._show_preferences_dialog)
                    dpg.add_menu_item(label="Exit",              callback=self._on_exit_requested)

                with dpg.menu(label="Add Objects"):
                    dpg.add_menu_item(label="Add Multiple Objects", callback=self._show_add_multiple_dialog)
                    with dpg.menu(label="Processing Objects"):
                        for node_type in sorted(self.proc_obj_templates.keys()):
                            dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)

                    with dpg.menu(label="Data Objects"):
                        for node_type in sorted(self.data_obj_templates.keys()):
                            dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)

                with dpg.menu(label="Overrides"):
                    dpg.add_menu_item(label="Load Override File(s)", callback=self._show_load_overrides_dialog)
                    dpg.add_separator()
                    dpg.add_menu_item(label="Remove...", callback=self._show_remove_override_menu, tag="remove_override_menu")

                with dpg.menu(label="Simulation"):
                    dpg.add_menu_item(label="Control Panel", callback=lambda: self.sim_control.show_control_window())
                    dpg.add_menu_item(label="Display Yaml", callback=lambda: self.sim_control.show_yaml_window())

                with dpg.menu(label="Layout"):
                    dpg.add_menu_item(label="Auto Layout",
                                      callback=lambda: auto_layout_nodes(self.nm.graph, self.nm.uuid_to_dpg))
                    dpg.add_menu_item(label="Debug Info",
                                      callback=lambda: print(f"Nodes: {len(self.nm.graph.nodes)}, "
                                                             f"Connections: {len(self.nm.graph.connections)}"))

            with dpg.group(horizontal=False):
                with dpg.group(horizontal=True):
                    with dpg.child_window(width=-450, tag="specula_editor_parent", border=False):
                        with dpg.node_editor(
                            tag="specula_editor",
                            callback=self.nm.link_callback,
                            delink_callback=self.nm.delink_callback,
                            minimap=True
                        ):
                            pass
                    with dpg.child_window(width=430, tag="property_panel", border=True):
                        pass
                with dpg.child_window(height=30, tag="status_bar", border=False):
                    dpg.add_text("Simulation: (Unsaved)", tag="status_bar_text", color=(180, 180, 180))

        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key_press)

        viewport_id = dpg.create_viewport(title="SPECULA Node Editor", width=1600, height=900)
        # dpg.configure_viewport(viewport_id, vsync=False)
        dpg.set_viewport_resize_callback(self._resize_callback)

        self.setup_dialogs()
        self._show_startup_dialog()

        dpg.setup_dearpygui()
        dpg.show_viewport()

        self.nm.after_dpg_init()
        dpg.set_primary_window('main_window', True)

    def _resize_callback(self):
        h = dpg.get_viewport_height()
        new_height = h - 80
        dpg.set_item_height("specula_editor_parent", new_height)
        dpg.set_item_height("property_panel", new_height)
        
        if dpg.does_item_exist("property_panel"):
            dpg.show_item("property_panel")
            # Force recalculation of layout
            dpg.split_frame()

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
            dpg.add_text("Would you like to save your current simulation before exiting?", color=[180, 180, 180])
            dpg.add_spacing(count=2)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save and Exit",      width=140, callback=self._on_exit_save_and_confirm)
                dpg.add_button(label="Exit without Saving",width=140, callback=self._on_exit_confirm)
                dpg.add_button(label="Cancel",             width=100, callback=self._on_exit_cancel)

    def _create_new_simulation_confirmation_dialog(self):
        with dpg.window(label="Create New Simulation?", tag="new_simulation_confirmation_dialog",
                        modal=True, show=False, width=450, height=180, no_resize=True):
            dpg.add_text("Create a new simulation?")
            dpg.add_text("Your current simulation will be cleared. Would you like to save it first?",
                         color=[180, 180, 180])
            dpg.add_spacing(count=2)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save and Continue", width=140, callback=self._on_new_simulation_save_and_proceed)
                dpg.add_button(label="Discard",           width=100, callback=self._on_new_simulation_discard)
                dpg.add_button(label="Cancel",            width=100, callback=self._on_new_simulation_cancel)

    # ── Startup dialog ───────────────────────────────────────────────────────……[...]

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
            dpg.add_spacing(count=1)
            with dpg.group(horizontal=True):
                dpg.add_text("Simulation name:")
                dpg.add_input_text(tag="startup_simulation_name", width=420,
                                   hint="Enter simulation name for new/import")
            dpg.add_spacing(count=1)
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
            print("Please enter a simulation name before creating a new simulation.")
            return
        self.nm.clear_all()
        self.nm.graph.nodes.clear()
        self.nm.graph.connections.clear()
        self.nm.graph.connection_properties.clear()
        self.current_simulation_name = name
        self.current_simulation_path = None
        print(f"[SIMULATION] Created new simulation: {name}")
        self._update_status_bar()
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")

    # ── Menu callbacks ───────────────────────────────────────────────────────……[...]

    def _on_menu_create(self, sender, app_data, user_data):
        node_uuid = self.nm.create_node(node_type=user_data)
        dpg.split_frame()
        dpg.split_frame()
        if self.preferences['auto_simul_params'] and user_data != "SimulParams":
            self._apply_auto_simul_params([(node_uuid, user_data)])

    def _on_save_simulation_clicked(self):
        if self.current_simulation_path:
            self.fh.save_simulation(self.current_simulation_path, self.preferences['include_defaults'])
        else:
            dpg.show_item("save_simulation_dialog")

    def _save_simulation_cb(self, s, a):
        path = a['file_path_name']
        self.fh.save_simulation(path, self.preferences['include_defaults'])
        self.current_simulation_path = path
        self.current_simulation_name = pathlib.Path(path).stem
        self._update_status_bar()
        
    def _load_simulation_cb(self, s, a):
        path = pathlib.Path(a['file_path_name']).resolve()
        self.fh.load_simulation(str(path))
        try:
            self.current_simulation_path = str(path)
            self.current_simulation_name = path.stem
        except Exception:
            pass
        self._update_status_bar()
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")
    # ── Override Management ───────────────────────────────────────────────────

    def _show_load_overrides_dialog(self):
        """Show file dialog to load override file(s)."""
        if not dpg.does_item_exist("load_overrides_dialog"):
            with dpg.file_dialog(
                label="Load Override File(s)",
                show=False,
                callback=self._on_overrides_loaded,
                id="load_overrides_dialog",
                width=700,
                height=400,
                default_filename=""
            ):
                dpg.add_file_extension(".yml")
                dpg.add_file_extension(".yaml")
        
        dpg.show_item("load_overrides_dialog")
    
    def _on_overrides_loaded(self, sender, app_data):
        """Callback when override file(s) are selected."""
        selections = app_data.get('file_path_name')
        if not selections:
            return
        
        # Handle both single file and multiple file selections
        if isinstance(selections, str):
            selections = [selections]
        
        loaded = self.override_manager.load_overrides(selections)
        print(f"[OVERRIDES] Loaded {loaded} override file(s)")
        self._refresh_overrides_menu()
    
    def _refresh_overrides_menu(self):
        """Refresh the Overrides menu with current overrides."""
        # Get the Overrides menu parent
        try:
            # Find and update the "Remove..." menu
            overrides = self.override_manager.get_all_overrides()
            
            if overrides:
                # We need to update the Remove submenu
                # For now, we'll recreate it
                self._update_remove_override_submenu()
        except Exception as e:
            print(f"[OVERRIDES] Error refreshing menu: {e}")
    
    def _update_remove_override_submenu(self):
        """Update the Remove submenu with current overrides."""
        overrides = self.override_manager.get_all_overrides()
        
        # Clear existing items under Remove menu
        try:
            # Find the Remove menu by iterating menu bar
            # We'll use a simpler approach: recreate the entire Overrides menu
            pass
        except Exception:
            pass
    
    def _show_remove_override_menu(self):
        """Show submenu for removing overrides."""
        overrides = self.override_manager.get_all_overrides()
        
        if not overrides:
            print("[OVERRIDES] No overrides loaded")
            return
        
        # Create a dynamic popup menu
        if dpg.does_item_exist("override_remove_popup"):
            dpg.delete_item("override_remove_popup")
        
        with dpg.window(
            label="Remove Override",
            tag="override_remove_popup",
            modal=True,
            show=True,
            width=500,
            height=300,
            no_resize=False
        ):
            dpg.add_text("Select override file(s) to remove:", color=[200, 200, 100])
            dpg.add_separator()
            
            with dpg.group(horizontal=False):
                for override_path in overrides:
                    is_enabled = self.override_manager.is_enabled(override_path)
                    label_text = f"{'✓' if is_enabled else '○'} {pathlib.Path(override_path).name}"
                    
                    with dpg.drawlayer(width=450, height=25):
                        dpg.draw_text(
                            (10, 5),
                            label_text,
                            color=[180, 180, 180] if is_enabled else [120, 120, 120]
                        )
                    
                    dpg.add_button(
                        label=f"Remove: {pathlib.Path(override_path).name}",
                        width=-1,
                        callback=lambda s, a, path=override_path: self._on_remove_override(path)
                    )
            
            dpg.add_spacing(count=2)
            dpg.add_separator()
            dpg.add_button(label="Close", width=-1, callback=lambda: dpg.delete_item("override_remove_popup"))
    
    def _on_remove_override(self, file_path: str):
        """Remove an override file."""
        self.override_manager.remove_override(file_path)
        dpg.delete_item("override_remove_popup")
        self._refresh_overrides_menu()
    
    def _show_overrides_window(self):
        """Show a window with all loaded overrides and their state."""
        if dpg.does_item_exist("overrides_window"):
            dpg.show_item("overrides_window")
            dpg.focus_item("overrides_window")
            return
        
        with dpg.window(
            label="Override Management",
            tag="overrides_window",
            width=600,
            height=400,
            no_resize=False
        ):
            dpg.add_text("Loaded Override Files", color=[200, 200, 100])
            dpg.add_separator()
            
            overrides = self.override_manager.get_all_overrides()
            
            if not overrides:
                dpg.add_text("No overrides loaded.", color=[150, 150, 150])
            else:
                with dpg.group(horizontal=False):
                    for override_path in overrides:
                        is_enabled = self.override_manager.is_enabled(override_path)
                        file_name = pathlib.Path(override_path).name
                        
                        with dpg.drawlayer(width=550, height=30):
                            with dpg.group(horizontal=True):
                                dpg.add_checkbox(
                                    default_value=is_enabled,
                                    callback=lambda s, v, path=override_path: self.override_manager.toggle_override(path),
                                    width=30
                                )
                                dpg.add_text(file_name, color=[180, 180, 180])
                                dpg.add_text(override_path, color=[120, 120, 120])
            
            dpg.add_spacing(count=2)
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Load More", width=120, callback=self._show_load_overrides_dialog)
                dpg.add_button(label="Close", width=100, callback=lambda: dpg.hide_item("overrides_window"))
                
    # ── Run loop ─────────────────────────────────────────────────────────……[...]
    
    def run(self):
        try:
            self.nm.start_periodic_tasks()
            dpg.set_viewport_resize_callback(None)  # ensure viewport is configured

            while dpg.is_dearpygui_running():
                # Tick in-process monitors on every frame — avoids the fragile
                # set_frame_callback chain which silently dies on any error.
                self.nm.monitors._inprocess_tick_direct()
                dpg.render_dearpygui_frame()
        finally:
            dpg.destroy_context()
     


if __name__ == "__main__":
    editor = SpeculaEditor(yaml_folder="specula_yaml_docs") 
    editor.run()

