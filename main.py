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

        # 3. Initialize Simulation Control
        from simulation_control import SimulationControl
        self.sim_control = SimulationControl(self)

        
        # Track current simulation name and path
        self.current_simulation_name = None
        self.current_simulation_path = None
        
        # Track items pending deletion
        self.pending_deletion_type = None  # 'node', 'link'
        self.pending_deletion_items = []
        
        # 3. Setup UI
        self.create_ui()
        # IMPORTANT: Do NOT call self.nm.setup_handlers() - it has an automatic Delete handler
        # Instead, register handlers manually without the Delete key handler
        self._setup_custom_handlers()


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

    def _setup_custom_handlers(self):
        """Register handlers without the automatic Delete key handler from nm.setup_handlers()."""
        with dpg.handler_registry():
            dpg.add_mouse_click_handler(callback=self.nm.on_click_editor)
            dpg.add_key_press_handler(key=dpg.mvKey_D, callback=self.nm.delete_selected_link)
            # DO NOT register Delete key here - we handle it in our own _on_key_press
            dpg.add_mouse_double_click_handler(callback=self.nm._on_canvas_double_click)
            dpg.add_mouse_move_handler(callback=self.nm._on_mouse_move)
    
    def _center_dialog(self, dialog_tag):
        """Center a dialog window on the viewport."""
        if dpg.does_item_exist(dialog_tag):
            try:
                viewport_width = dpg.get_viewport_width()
                viewport_height = dpg.get_viewport_height()
                
                dialog_width = dpg.get_item_width(dialog_tag)
                dialog_height = dpg.get_item_height(dialog_tag)
                
                center_x = (viewport_width - dialog_width) // 2
                center_y = (viewport_height - dialog_height) // 2
                
                dpg.set_item_pos(dialog_tag, [center_x, center_y])
            except SystemError:
                # File dialogs don't support set_item_pos, skip silently
                pass
    
    # Callback in SpeculaEditor class
    def _toggle_export_defaults(self, sender, app_data):
        print('_toggle_export_defaults', app_data)
        self.export_include_defaults = app_data

    # ------------------------------------------------------------------
    # Status Bar Management
    # ------------------------------------------------------------------

    def _update_status_bar(self):
        """Update the status bar to show the current simulation name."""
        if self.current_simulation_name:
            status_text = f"Simulation: {self.current_simulation_name}"
        else:
            status_text = "Simulation: (Unsaved)"
        
        if dpg.does_item_exist("status_bar_text"):
            dpg.set_value("status_bar_text", status_text)

    # ------------------------------------------------------------------
    # New Simulation Handling
    # ------------------------------------------------------------------

    def _on_new_simulation_clicked(self):
        """Handle 'New Simulation' menu click. Offer to save current simulation."""
        if self.current_simulation_name is None:
            # No simulation loaded, just show startup dialog
            self._show_startup_dialog()
        else:
            # Simulation exists, ask if user wants to save
            self._center_dialog("new_simulation_confirmation_dialog")
            dpg.show_item("new_simulation_confirmation_dialog")

    def _on_new_simulation_save_and_proceed(self):
        """User chose to save before creating new simulation."""
        if self.current_simulation_path:
            # Save to existing path
            self.fh.save_simulation(self.current_simulation_path)
        else:
            # No path set, show save dialog
            dpg.hide_item("new_simulation_confirmation_dialog")
            self._center_dialog("save_before_new_dialog")
            dpg.show_item("save_before_new_dialog")
            return
        
        # Proceed with new simulation
        dpg.hide_item("new_simulation_confirmation_dialog")
        self._show_startup_dialog()

    def _on_save_before_new_cb(self, sender, app_data):
        """Callback from save dialog before creating new simulation."""
        path = app_data['file_path_name']
        self.fh.save_simulation(path)
        self.current_simulation_path = path
        self.current_simulation_name = pathlib.Path(path).stem
        self._update_status_bar()
        
        # Proceed with new simulation
        self._show_startup_dialog()

    def _on_new_simulation_discard(self):
        """User chose not to save, just proceed with new simulation."""
        dpg.hide_item("new_simulation_confirmation_dialog")
        self._show_startup_dialog()

    def _on_new_simulation_cancel(self):
        """User cancelled creating new simulation."""
        dpg.hide_item("new_simulation_confirmation_dialog")

    # ------------------------------------------------------------------
    # Delete Confirmation Dialog
    # ------------------------------------------------------------------

    def _on_delete_requested(self):
        """Called when user presses Delete key."""
        # Check if a link is selected first
        if self.nm._selected_link_id:
            self.pending_deletion_items = [self.nm._selected_link_id]
            self.pending_deletion_type = "link"
            self._show_delete_confirmation_dialog("Delete 1 connection?")
            return
        
        # Get selected nodes
        selected_nodes = self.nm.get_selected_nodes()
        
        if not selected_nodes:
            # Nothing selected
            return
        
        # Store pending deletion items
        self.pending_deletion_items = selected_nodes
        self.pending_deletion_type = "nodes"
        self._show_delete_confirmation_dialog(f"Delete {len(selected_nodes)} node(s)?")

    def _show_delete_confirmation_dialog(self, message):
        """Show the delete confirmation dialog."""
        if dpg.does_item_exist("delete_confirmation_dialog"):
            dpg.delete_item("delete_confirmation_dialog")
        
        with dpg.window(
            label="Confirm Deletion",
            tag="delete_confirmation_dialog",
            modal=True,
            show=True,
            width=400,
            height=150,
            no_resize=True
        ):
            dpg.add_text(message)
            dpg.add_spacing(count=2)
            
            with dpg.group(horizontal=True):
                dpg.add_button(label="Delete", width=100, callback=self._on_delete_confirm)
                dpg.add_button(label="Cancel", width=100, callback=self._on_delete_cancel)
        
        # Center the dialog after creation
        self._center_dialog("delete_confirmation_dialog")

    def _on_delete_confirm(self):
        """User confirmed deletion."""
        dpg.hide_item("delete_confirmation_dialog")
        
        if self.pending_deletion_type == "nodes":
            # Delete nodes using the original node_manager method
            for node_uuid in self.pending_deletion_items:
                self.nm.delete_node(node_uuid)
        elif self.pending_deletion_type == "link":
            # Delete links using the original delink_callback
            for link_id in self.pending_deletion_items:
                self.nm.delink_callback(None, link_id)
        
        # Clear pending deletion
        self.pending_deletion_items = []
        self.pending_deletion_type = None

    def _on_delete_cancel(self):
        """User cancelled deletion."""
        dpg.hide_item("delete_confirmation_dialog")
        # Clear pending deletion
        self.pending_deletion_items = []
        self.pending_deletion_type = None

    # ------------------------------------------------------------------
    # Exit and Close Window Handling
    # ------------------------------------------------------------------
    
    def _on_exit_requested(self):
        """Called when user clicks Exit menu or closes the main window."""
        # Show confirmation dialog
        self._center_dialog("exit_confirmation_dialog")
        dpg.show_item("exit_confirmation_dialog")
    
    def _on_exit_confirm(self):
        """User confirmed exit without saving."""
        dpg.hide_item("exit_confirmation_dialog")
        dpg.stop_dearpygui()
    
    def _on_exit_save_and_confirm(self):
        """User confirmed exit and wants to save first."""
        # If we have a current simulation path, save to that path directly
        if self.current_simulation_path:
            self.fh.save_simulation(self.current_simulation_path)
            dpg.stop_dearpygui()
        else:
            # Show save dialog
            dpg.hide_item("exit_confirmation_dialog")
            self._center_dialog("save_and_exit_dialog")
            dpg.show_item("save_and_exit_dialog")
    
    def _on_save_and_exit_cb(self, sender, app_data):
        """Callback from save dialog when saving before exit."""
        path = app_data['file_path_name']
        self.fh.save_simulation(path)
        self.current_simulation_path = path
        self.current_simulation_name = pathlib.Path(path).stem
        self._update_status_bar()
        dpg.stop_dearpygui()
    
    def _on_exit_cancel(self):
        """User cancelled the exit."""
        dpg.hide_item("exit_confirmation_dialog")

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
        self._center_dialog("add_multiple_dialog")
        dpg.show_item("add_multiple_dialog")

    def _on_add_multiple_close(self):
        """Called when the window X button is pressed."""
        self._multi_add_queue.clear()

    # ── helpers ──────────��───────────────────────────────────────────

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


    def _on_key_press(self, sender, app_data):
        """Handle global key presses."""
        # Delete key to delete selected nodes or links with confirmation
        if app_data == dpg.mvKey_Delete:
            self._on_delete_requested()


    def create_ui(self):

        self.export_include_defaults = False
        dpg.create_context()
        # --- Themes & Fonts ---
        dpg_utils.set_zebra_theme() # Applied from the utility file
        self.nm.init_themes() 
        
        with dpg.font_registry():
            if os.path.exists(FONT_PATH):
                dpg.bind_font(dpg.add_font(FONT_PATH, FONT_SIZE))

        # --- Main Window ---
        
        with dpg.window(label="SPECULA Editor", tag='main_window', on_close=self._on_exit_requested):
            
            # 1. Menu Bar
            with dpg.menu_bar():
                with dpg.menu(label="File"):
                    dpg.add_menu_item(label="New Simulation", callback=self._on_new_simulation_clicked)                    
                    dpg.add_menu_item(label="Load Simulation", callback=lambda: dpg.show_item("load_simulation_dialog"))
                    dpg.add_separator()
                    dpg.add_menu_item(label="Save Simulation", callback=self._on_save_simulation_clicked)
                    dpg.add_menu_item(label="Save Simulation As", callback=lambda: dpg.show_item("save_simulation_dialog"))                    
                    dpg.add_separator()                    
                    dpg.add_menu_item(label="Include Defaults in saved simulations", check=True, callback=self._toggle_export_defaults)                    
                    dpg.add_separator()
                    dpg.add_menu_item(label="Exit", callback=self._on_exit_requested)

                with dpg.menu(label="Processing Objects"):
                    for node_type in sorted(self.proc_obj_templates.keys()):
                        dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)
                
                with dpg.menu(label="Data Objects"):
                    for node_type in sorted(self.data_obj_templates.keys()):
                        dpg.add_menu_item(label=node_type, callback=self._on_menu_create, user_data=node_type)

                dpg.add_menu_item(label="Add Multiple Objects", callback=self._show_add_multiple_dialog)

                with dpg.menu(label="Simulation"):
                    dpg.add_menu_item(label="Control Panel", callback=lambda: self.sim_control.show_control_window())

                with dpg.menu(label="Layout"):
                    dpg.add_menu_item(label="Auto Layout", callback=lambda: auto_layout_nodes(self.nm.graph, self.nm.uuid_to_dpg))
                    dpg.add_menu_item(label="Debug Info", callback=lambda: print(f"Nodes: {len(self.nm.graph.nodes)}, Connections: {len(self.nm.graph.connections)}"))
   
            # 2. Main content area (horizontal: editor + properties)
            with dpg.group(horizontal=False):
                with dpg.group(horizontal=True):
                    # Left Side: The Editor
                    with dpg.child_window(width=-450, tag="specula_editor_parent", border=False):
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

                # 3. Status Bar at the bottom
                with dpg.child_window(height=30, tag="status_bar",border=False):
                    dpg.add_text("Simulation: (Unsaved)", tag="status_bar_text", color=(180, 180, 180))

        # --- Global Handlers ---
        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key_press)

        dpg.create_viewport(title="SPECULA Node Editor", width=1600, height=900)

        dpg.set_viewport_resize_callback(self._resize_callback)

        # --- Setup File Dialogs ---
        self.setup_dialogs()

        # --- Show Startup Dialog ---
        self._show_startup_dialog()

        dpg.setup_dearpygui()
        dpg.show_viewport()

        self.nm.after_dpg_init()

        dpg.set_primary_window('main_window', True)


    def _resize_callback(self):
        # Get current viewport height
        h = dpg.get_viewport_height()
        # Subtract roughly 80px (for menu bar + status bar + margins)
        new_height = h - 80 
        dpg.set_item_height("specula_editor_parent", new_height)
        dpg.set_item_height("property_panel", new_height)


    def setup_dialogs(self):
        # We point these to FileHandler's logic via thin wrappers
        
        with dpg.file_dialog(label="Save Simulation", show=False, callback=self._save_simulation_cb, id="save_simulation_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")

        with dpg.file_dialog(label="Load Simulation", show=False, callback=self._load_simulation_cb, id="load_simulation_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")

        with dpg.file_dialog(label="Save Simulation Before Exit", show=False, callback=self._on_save_and_exit_cb, id="save_and_exit_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")

        with dpg.file_dialog(label="Save Simulation Before New", show=False, callback=self._on_save_before_new_cb, id="save_before_new_dialog", width=700, height=400):
            dpg.add_file_extension(".yml")

        # Exit Confirmation Dialog
        self._create_exit_confirmation_dialog()

        # New Simulation Confirmation Dialog
        self._create_new_simulation_confirmation_dialog()

        # Add Multiple Objects modal
        self._setup_add_multiple_dialog()

    def _create_exit_confirmation_dialog(self):
        """Create the exit confirmation modal dialog."""
        with dpg.window(
            label="Confirm Exit",
            tag="exit_confirmation_dialog",
            modal=True,
            show=False,
            width=450,
            height=180,
            no_resize=True
        ):
            dpg.add_text("Are you sure you want to exit?")
            dpg.add_text("Would you like to save your current simulation before exiting?", color=[180, 180, 180])
            dpg.add_spacing(count=2)
            
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save and Exit", width=140, callback=self._on_exit_save_and_confirm)
                dpg.add_button(label="Exit without Saving", width=140, callback=self._on_exit_confirm)
                dpg.add_button(label="Cancel", width=100, callback=self._on_exit_cancel)

    def _create_new_simulation_confirmation_dialog(self):
        """Create the new simulation confirmation modal dialog."""
        with dpg.window(
            label="Create New Simulation?",
            tag="new_simulation_confirmation_dialog",
            modal=True,
            show=False,
            width=450,
            height=180,
            no_resize=True
        ):
            dpg.add_text("Create a new simulation?")
            dpg.add_text("Your current simulation will be cleared. Would you like to save it first?", color=[180, 180, 180])
            dpg.add_spacing(count=2)
            
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save and Continue", width=140, callback=self._on_new_simulation_save_and_proceed)
                dpg.add_button(label="Discard", width=100, callback=self._on_new_simulation_discard)
                dpg.add_button(label="Cancel", width=100, callback=self._on_new_simulation_cancel)

    # --- Startup dialog helpers ---
    def _show_startup_dialog(self):
        """Show the startup dialog (reused for new simulations)."""
        if dpg.does_item_exist("startup_dialog"):
            dpg.set_value("startup_simulation_name", "")
            self._center_dialog("startup_dialog")
            dpg.show_item("startup_dialog")
        else:
            self._create_startup_dialog()

    def _on_startup_open_existing(self, sender, app_data):
        """Open the load simulation dialog from startup dialog and close startup dialog."""
        if dpg.does_item_exist("startup_dialog"):
            dpg.hide_item("startup_dialog")
        dpg.show_item("load_simulation_dialog")

    def _create_startup_dialog(self):
        """Create a modal startup dialog shown at application launch."""
        if dpg.does_item_exist("startup_dialog"):
            self._center_dialog("startup_dialog")
            dpg.show_item("startup_dialog")
            return

        with dpg.window(label="Welcome", tag="startup_dialog", modal=True, show=True, width=640, height=160):
            dpg.add_text("Create a new simulation, open an existing simulation, or import a Specula config into a new simulation.")
            dpg.add_spacing(count=1)
            with dpg.group(horizontal=True):
                dpg.add_text("Simulation name:")
                dpg.add_input_text(tag="startup_simulation_name", width=420, hint="Enter simulation name for new/import")
            dpg.add_spacing(count=1)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Create New Simulation", callback=self._startup_create_new)
                dpg.add_button(label="Open Existing Simulation", callback=self._on_startup_open_existing)                
                dpg.add_button(label="Cancel", callback=lambda s, a: dpg.hide_item("startup_dialog"))
        
        # Center the startup dialog after creation
        self._center_dialog("startup_dialog")

    def _startup_create_new(self, sender, app_data):
        name = dpg.get_value("startup_simulation_name").strip() if dpg.does_item_exist("startup_simulation_name") else ""
        if not name:
            if dpg.does_item_exist("startup_simulation_name"):
                dpg.set_value("startup_simulation_name", "")
                dpg.focus_item("startup_simulation_name")
            print("Please enter a simulation name before creating a new simulation.")
            return

        # Clear existing graph and set simulation name
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


    # --- Callbacks ---
    def _on_menu_create(self, sender, app_data, user_data):
        self.nm.create_node(node_type=user_data)

    def _on_save_simulation_clicked(self):
        """Handle 'Save Simulation' menu click."""
        if self.current_simulation_path:
            self.fh.save_simulation(self.current_simulation_path, self.export_include_defaults)
        else:
            dpg.show_item("save_simulation_dialog")

    # --- File Bridge Callbacks ---
    
    def _save_simulation_cb(self, s, a):
        path = a['file_path_name']
        self.fh.save_simulation(path)
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

    def run(self):
        try:
            self.nm.start_periodic_tasks()
            dpg.start_dearpygui()
        finally:
            dpg.destroy_context()

if __name__ == "__main__":
    editor = SpeculaEditor(yaml_folder="specula_yaml_docs") 
    editor.run()