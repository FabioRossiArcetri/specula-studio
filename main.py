import dearpygui.dearpygui as dpg
import os
import yaml
from collections import OrderedDict
from constants import FONT_SIZE
from node_manager import NodeManager
from file_handler import FileHandler, auto_layout_nodes
from graph_manager import GraphManager
import dpg_utils
import pathlib

# Constants
import matplotlib
FONT_PATH = matplotlib.get_data_path() + '/fonts/ttf/'
FONT_PATH += "DejaVuSerif.ttf"

# Define a loader that preserves order
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
        
        # Track current scene name
        self.current_scene_name = None
        
        # 3. Setup UI
        self.create_ui()
        self.nm.setup_handlers()


    def load_templates(self, folder):
        templates = OrderedDict() # Use OrderedDict
        if os.path.exists(folder):
            for file in os.listdir(folder):
                if file.endswith(".yml"):
                    with open(os.path.join(folder, file), 'r') as f:
                        # Use the ordered loader
                        data = ordered_load(f)
                        if data:
                            templates.update(data)
        return templates

    
    # Callback in SpeculaEditor class
    def _toggle_export_defaults(self, sender, app_data):
        print('_toggle_export_defaults', app_data)
        self.export_include_defaults = app_data

    # ------------------------------------------------------------------
    # Add Multiple Objects dialog
    # ------------------------------------------------------------------

    def _mo_on_double_click(self, sender, app_data):
        # app_data[1] is the item that was double-clicked
        clicked_id = app_data[1]
        
        # 1. Check if the clicked item itself is the listbox
        # 2. Or check if the parent of the clicked item is the listbox
        # (DPG sometimes reports the internal selectable item)
        parent_id = dpg.get_item_info(clicked_id)["parent"]
        
        # Resolve IDs to Tags/Aliases for comparison
        alias = dpg.get_item_alias(clicked_id)
        parent_alias = dpg.get_item_alias(parent_id)

        if alias == "_mo_proc_listbox" or parent_alias == "_mo_proc_listbox":
            self._mo_add_proc()
        elif alias == "_mo_data_listbox" or parent_alias == "_mo_data_listbox":
            self._mo_add_data()

    def _setup_add_multiple_dialog(self):
        """Build the 'Add Multiple Objects' modal window (created once)."""
        self._multi_add_queue = []   # list of node_type strings staged for creation

        proc_types = sorted(self.proc_obj_templates.keys())
        data_types = sorted(self.data_obj_templates.keys())

        LISTBOX_H = 320   # pixel height of each listbox
        COL_W     = 260   # width of each column
        
        # Use a unique tag and check existence
        if not dpg.does_item_exist("mo_double_click_handler"):
            with dpg.item_handler_registry(tag="mo_double_click_handler"):
                dpg.add_item_double_clicked_handler(callback=self._mo_on_double_click)
                        
        with dpg.window(
            label="Add Multiple Objects",
            tag="add_multiple_dialog",
            modal=True,
            show=False,
            width=1000,
            height=550,
            no_resize=True,
            on_close=self._on_add_multiple_close,
        ):
            # ── header hint ──────────────────────────────────────────
            dpg.add_text(
                "Select items from the lists, use the arrows to stage them, "
                "then click Confirm.",
                color=[180, 180, 180],
            )
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ── three-column layout ──────────────────────────────────
            with dpg.group(horizontal=True):

                # Processing Objects Column
                with dpg.group(width=COL_W):
                    dpg.add_text("Processing Objects")
                    dpg.add_listbox(
                        items=proc_types,
                        tag="_mo_proc_listbox",
                        num_items=16,
                        width=COL_W,
                    )
                    # Bind the handler
                    dpg.bind_item_handler_registry("_mo_proc_listbox", "mo_double_click_handler")

                dpg.add_spacer(width=8)

                # Data Objects Column
                with dpg.group(width=COL_W):
                    dpg.add_text("Data Objects")
                    dpg.add_listbox(
                        items=data_types,
                        tag="_mo_data_listbox",
                        num_items=16,
                        width=COL_W,
                    )
                    # Bind the handler
                    dpg.bind_item_handler_registry("_mo_data_listbox", "mo_double_click_handler")                    
                
                dpg.add_spacer(width=8)

                # ── center arrow buttons ─────────────────────────────
                with dpg.group(width=70):
                    dpg.add_spacer(height=90)
                    dpg.add_button(
                        label="Add Proc →",
                        width=70,
                        callback=self._mo_add_proc,
                    )
                    dpg.add_spacer(height=12)
                    dpg.add_button(
                        label="Add Data →",
                        width=70,
                        callback=self._mo_add_data,
                    )
                    dpg.add_spacer(height=12)
                    dpg.add_button(
                        label="← Remove",
                        width=70,
                        callback=self._mo_remove,
                    )

                dpg.add_spacer(width=8)

                # ── col 3 : Staged / Selected ────────────────────────
                with dpg.group(width=COL_W):
                    dpg.add_text("Staged to Add", color=[150, 255, 150])
                    dpg.add_listbox(
                        items=[],
                        tag="_mo_staged_listbox",
                        num_items=16,
                        width=COL_W,
                    )

            # ── footer ───────────────────────────────────────────────
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Confirm",
                    tag="_mo_confirm_btn",
                    width=160,
                    callback=self._mo_confirm,
                )
                dpg.add_spacer(width=8)
                dpg.add_button(
                    label="Cancel",
                    width=100,
                    callback=self._mo_cancel,
                )
                dpg.add_spacer(width=20)
                dpg.add_text("", tag="_mo_status_text", color=[200, 200, 100])

    def _show_add_multiple_dialog(self):
        """Open the dialog and reset staged list."""
        self._multi_add_queue.clear()
        self._mo_refresh_staged()
        dpg.show_item("add_multiple_dialog")

    def _on_add_multiple_close(self):
        """Called when the window X button is pressed."""
        self._multi_add_queue.clear()

    # ── helpers ──────────────────────────────────────────────────────

    def _mo_refresh_staged(self):
        """Rebuild the staged listbox from the internal queue."""
        dpg.configure_item("_mo_staged_listbox", items=list(self._multi_add_queue))
        count = len(self._multi_add_queue)
        label = f"Confirm  ({count} node{'s' if count != 1 else ''})"
        dpg.configure_item("_mo_confirm_btn", label=label)
        dpg.set_value("_mo_status_text", "")

    def _mo_add_from_listbox(self, listbox_tag: str):
        """Stage whichever item is currently selected in *listbox_tag*."""
        selected = dpg.get_value(listbox_tag)
        if selected and selected.strip():
            self._multi_add_queue.append(selected)
            self._mo_refresh_staged()

    def _mo_add_proc(self):
        self._mo_add_from_listbox("_mo_proc_listbox")

    def _mo_add_data(self):
        self._mo_add_from_listbox("_mo_data_listbox")

    def _mo_remove(self):
        """Remove the currently selected item from the staged list."""
        selected = dpg.get_value("_mo_staged_listbox")
        if selected and selected in self._multi_add_queue:
            # Remove the last occurrence so duplicates are handled gracefully
            idx = len(self._multi_add_queue) - 1 - self._multi_add_queue[::-1].index(selected)
            self._multi_add_queue.pop(idx)
            self._mo_refresh_staged()

    def _mo_confirm(self):
        """Create all staged nodes and close the dialog."""
        if not self._multi_add_queue:
            dpg.set_value("_mo_status_text", "Nothing staged.")
            return
        for node_type in self._multi_add_queue:
            self.nm.create_node(node_type=node_type)
        count = len(self._multi_add_queue)
        print(f"[ADD_MULTIPLE] Created {count} node(s): {self._multi_add_queue}")
        self._multi_add_queue.clear()
        dpg.hide_item("add_multiple_dialog")

    def _mo_cancel(self):
        """Close without creating anything."""
        self._multi_add_queue.clear()
        dpg.hide_item("add_multiple_dialog")


    # In the create_ui method, add this to the global handlers section:
    def _on_key_press(self, sender, app_data):
        """Handle global key presses."""
        # D key to delete selected link
        if app_data == dpg.mvKey_D:
            # Forward to node manager
            self.nm.delete_selected_link(sender, app_data)


    def create_ui(self):

        self.export_include_defaults = False
        dpg.create_context()
        # --- Themes & Fonts ---
        dpg_utils.set_zebra_theme() # Applied from the utility file
        self.nm.init_themes() 
        
        with dpg.font_registry():
            if os.path.exists(FONT_PATH):
                dpg.bind_font(dpg.add_font(FONT_PATH, FONT_SIZE))

        dpg.create_viewport(title="SPECULA Node Editor", width=1600, height=900)

        # --- Main Window ---
        with dpg.window(label="SPECULA Editor", tag='main_window'):
            
            # 1. Menu Bar
            with dpg.menu_bar():
                with dpg.menu(label="File"):

                    dpg.add_menu_item(label="Save Scene", callback=lambda: dpg.show_item("save_scene_dialog"))
                    dpg.add_menu_item(label="Load Scene", callback=lambda: dpg.show_item("load_scene_dialog"))
                    dpg.add_separator()                    
                    dpg.add_menu_item(label="Import Specula Sim", callback=lambda: dpg.show_item("import_sim_dialog"))
                    dpg.add_menu_item(label="Include Defaults in Export", check=True, callback=self._toggle_export_defaults)
                    dpg.add_menu_item(label="Export Specula Sim", callback=lambda: dpg.show_item("export_sim_dialog"))

          
                with dpg.menu(label="Processing Objects"):
                    for node_type in sorted(self.proc_obj_templates.keys()):
                        dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)
                
                with dpg.menu(label="Data Objects"):
                    for node_type in sorted(self.data_obj_templates.keys()):
                        dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)

                # Add Multiple Objects shortcut
                dpg.add_menu_item(
                    label="Add Multiple Objects",
                    callback=self._show_add_multiple_dialog,
                )

                # In your main UI setup code
                with dpg.menu(label="Layout"):
                    dpg.add_menu_item(label="Auto Layout", callback=lambda: auto_layout_nodes(self.nm.graph, self.nm.uuid_to_dpg))
                    dpg.add_menu_item(label="Debug Info", callback=lambda: print(f"Nodes: {len(self.nm.graph.nodes)}, Connections: {len(self.nm.graph.connections)}"))
  

            # 2. Split Workspace
            with dpg.group(horizontal=True):
                # Left Side: The Editor
                with dpg.child_window(width=-450, border=False):
                    with dpg.node_editor(
                        tag="specula_editor", 
                        callback=self.nm.link_callback, 
                        delink_callback=self.nm.delink_callback,
                        minimap=True
                    ):
                        pass

                # Right Side: Properties Panel
                with dpg.child_window(width=430, tag="property_panel", border=True):
                    pass

        # --- Global Handlers ---
        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key_press)  # Add this line


        # --- Setup File Dialogs ---
        self.setup_dialogs()

        # --- Show Startup Dialog ---
        self._create_startup_dialog()

        dpg.setup_dearpygui()
        dpg.show_viewport()

        self.nm.after_dpg_init()
    

        dpg.set_primary_window('main_window', True)




    def setup_dialogs(self):
        # We point these to FileHandler's logic via thin wrappers
        with dpg.file_dialog(label="Import Specula Simulation", show=False, callback=self._import_cb, id="import_sim_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")
        
        with dpg.file_dialog(label="Export Specula Simulation", show=False, callback=self._export_cb, id="export_sim_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")

        with dpg.file_dialog(label="Save Scene", show=False, callback=self._save_scene_cb, id="save_scene_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")

        with dpg.file_dialog(label="Load Scene", show=False, callback=self._load_scene_cb, id="load_scene_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")

        # Add Multiple Objects modal
        self._setup_add_multiple_dialog()

    # --- Startup dialog helpers ---
    def _create_startup_dialog(self):
        """Create a modal startup dialog shown at application launch."""
        # Avoid recreating if exists
        if dpg.does_item_exist("startup_dialog"):
            dpg.show_item("startup_dialog")
            return

        with dpg.window(label="Welcome", tag="startup_dialog", modal=True, show=True, width=640, height=160):
            dpg.add_text("Create a new scene, open an existing scene, or import a simulation into a new scene.")
            dpg.add_spacing(count=1)
            with dpg.group(horizontal=True):
                dpg.add_text("Scene name:")
                dpg.add_input_text(tag="startup_scene_name", width=420, hint="Enter scene name for new/import")
            dpg.add_spacing(count=1)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Create New Scene", callback=self._startup_create_new)
                dpg.add_button(label="Open Existing Scene", callback=lambda s, a: dpg.show_item("load_scene_dialog"))
                dpg.add_button(label="Import Simulation (new scene)", callback=lambda s, a: dpg.show_item("import_sim_dialog"))
                #dpg.add_same_line(spacing=10)
                dpg.add_button(label="Cancel", callback=lambda s, a: dpg.hide_item("startup_dialog"))

    def _startup_create_new(self, sender, app_data):
        name = dpg.get_value("startup_scene_name").strip() if dpg.does_item_exist("startup_scene_name") else ""
        if not name:
            # simple feedback - focus the input
            if dpg.does_item_exist("startup_scene_name"):
                dpg.set_value("startup_scene_name", "")
                dpg.focus_item("startup_scene_name")
            print("Please enter a scene name before creating a new scene.")
            return

        # Clear existing graph and set scene name
        self.nm.clear_all()
        self.nm.graph.nodes.clear()
        self.nm.graph.connections.clear()
        self.nm.graph.connection_properties.clear()
        self.current_scene_name = name
        print(f"[SCENE] Created new scene: {name}")

        # Hide the startup dialog
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")


    # --- Callbacks ---
    def _on_menu_create(self, sender, app_data, user_data):
        self.nm.create_node(node_type=user_data)

    # --- File Bridge Callbacks ---
    def _import_cb(self, s, a):
        path = pathlib.Path(a['file_path_name']).resolve()
        # Optional: restrict to allowed directories
        # if not path.is_relative_to(pathlib.Path.cwd()):
        #     print(f"[SECURITY] Blocked path traversal: {path}")
        #     return
        # If user provided a scene name in the startup dialog, use it as the scene name for the imported scene
        if dpg.does_item_exist("startup_scene_name"):
            name = dpg.get_value("startup_scene_name").strip()
            if name:
                self.current_scene_name = name
        else:
            self.current_scene_name = path.stem

        self.fh.import_simulation(str(path))

        # Hide startup dialog if still visible
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")

    def _export_cb(self, s, a): self.fh.export_simulation(a['file_path_name'], self.export_include_defaults)
    def _save_scene_cb(self, s, a): self.fh.save_scene(a['file_path_name'])
    def _load_scene_cb(self, s, a):
        self.fh.load_scene(a['file_path_name'])
        # Set current scene name from file
        try:
            path = pathlib.Path(a['file_path_name'])
            self.current_scene_name = path.stem
        except Exception:
            pass

        # Hide startup dialog if still visible
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")

    def run(self):
        try:
            self.nm.start_periodic_tasks()
            dpg.start_dearpygui()
        finally:
            # Clean up monitors before destroying context
            dpg.destroy_context()

if __name__ == "__main__":
    # Ensure this points to your actual config folder
    editor = SpeculaEditor(yaml_folder="specula_yaml_docs") 
    editor.run()