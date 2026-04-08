import dearpygui.dearpygui as dpg
import os
import yaml
from collections import OrderedDict
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
                dpg.bind_font(dpg.add_font(FONT_PATH, 16))

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
                # with dpg.child_window(width=430, tag="property_panel", border=True):
                #     self._show_property()

        # --- Global Handlers ---
        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key_press)  # Add this line


        # --- Setup File Dialogs ---
        self.setup_dialogs()

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
        self.fh.import_simulation(str(path))

    def _export_cb(self, s, a): self.fh.export_simulation(a['file_path_name'], self.export_include_defaults)
    def _save_scene_cb(self, s, a): self.fh.save_scene(a['file_path_name'])
    def _load_scene_cb(self, s, a): self.fh.load_scene(a['file_path_name'])

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
