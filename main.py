import dearpygui.dearpygui as dpg
import os
import yaml
from collections import OrderedDict
from node_manager import NodeManager
from file_handler import FileHandler
from graph_manager import GraphManager
import dpg_utils
import node_manager


# Constants
# FONT_PATH = "C:/Windows/Fonts/DejaVuSerif.ttf"
FONT_PATH = "/opt/anaconda3/envs/base11/lib/python3.11/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSerif.ttf"

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

    # Update the Export Bridge Callback
    def _export_cb(self, s, a): 
        self.fh.export_simulation(
            a['file_path_name'], 
            include_defaults=self.export_include_defaults
        )


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

                with dpg.menu(label="Monitors"):
                    dpg.add_menu_item(label="Close All Monitors", callback=lambda: self.nm.close_all_monitors())
  

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
                    self._show_property()

        # --- Global Handlers ---
        with dpg.handler_registry():
            dpg.add_mouse_click_handler(button=0, callback=self._on_canvas_click)
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

    def _on_canvas_click(self):
        if dpg.is_item_hovered("property_panel"): return
        
        selected = dpg.get_selected_nodes("specula_editor")
        if selected:
            node_uuid = self.nm.dpg_to_uuid.get(selected[0])
            if self.nm._last_selected_uuid != node_uuid:
                self.nm._last_selected_uuid = node_uuid
                self.nm.update_property_panel(node_uuid, "property_panel")
        else:
            self.nm._last_selected_uuid = None
            self._show_property()

    def _show_property(self):
        dpg.delete_item("property_panel", children_only=True)
        
        selected = dpg.get_selected_nodes("specula_editor")
        if not selected: return
        
        node_uuid = selected[0].split("_")[1]
        node_data = self.nm.graph.nodes[node_uuid]
        node_values = node_data.get('values', {})
        template = self.nm.all_templates.get(node_data['type'], {})
        template_params = template.get('parameters', {})

        # --- UI Header ---
        dpg.add_text(f"Class: {node_data['type']}", color=[100, 200, 255], parent="property_panel")
        dpg.add_input_text(label="Name", default_value=node_data['name'], 
                           callback=self.nm.update_node_name, user_data=node_uuid, parent="property_panel")
        dpg.add_separator(parent="property_panel")

        # --- Parameters ---
        # We combine template keys and actual value keys to ensure nothing is hidden
        all_param_names = set(template_params.keys()) | set(node_values.keys())

        for p_name in sorted(all_param_names):
            p_meta = template_params.get(p_name, {})
            val = node_values.get(p_name, p_meta.get('default'))
            p_type = p_meta.get('type', 'str')
            
            # Determine if this is an _object reference
            is_data_ref = self.nm.is_data_class_type(p_type) or p_name in node_data.get('suffixes', [])

            with dpg.group(parent="property_panel"):
                color = [150, 255, 150] if is_data_ref else [255, 255, 255]
                dpg.add_text(f"{p_name}:", color=color)
                
                # Use Input Text for all strings or Data Object filenames
                if isinstance(val, bool):
                    dpg.add_checkbox(default_value=val, callback=self.nm.update_node_value, user_data=(node_uuid, p_name))
                elif isinstance(val, (int, float)) and not is_data_ref:
                    dpg.add_input_double(default_value=float(val), width=-1, step=0,
                                         callback=self.nm.update_node_value, user_data=(node_uuid, p_name))
                else:
                    # This covers strings and Data Objects
                    dpg.add_input_text(default_value=str(val) if val is not None else "", 
                                       width=-1, callback=self.nm.update_node_value, user_data=(node_uuid, p_name))
                    
    # --- File Bridge Callbacks ---
    def _import_cb(self, s, a): self.fh.import_simulation(a['file_path_name'])
    def _export_cb(self, s, a): self.fh.export_simulation(a['file_path_name'], self.export_include_defaults)
    def _save_scene_cb(self, s, a): self.fh.save_scene(a['file_path_name'])
    def _load_scene_cb(self, s, a): self.fh.load_scene(a['file_path_name'])

    def run(self):
        try:
            self.nm.start_periodic_tasks()
            dpg.start_dearpygui()
        finally:
            # Clean up monitors before destroying context
            self.nm.close_all_monitors()
            dpg.destroy_context()

if __name__ == "__main__":
    # Ensure this points to your actual config folder
    editor = SpeculaEditor(yaml_folder="specula_yaml_docs") 
    editor.run()
