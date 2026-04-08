"""
node_manager.py
===============
DPG node-graph UI manager and orchestrator.

After refactoring, this module is responsible only for:
  - Node / link CRUD (create, delete, clear).
  - DPG attribute registries (via NodeRegistry).
  - Theme management.
  - Mouse / keyboard event handlers.

Three sub-components handle the previously embedded responsibilities:
  - SocketIOClient   (socketio_client.py)  – server connection & pub/sub.
  - MonitorManager   (monitor_manager.py)  – live-data monitor windows.
  - PropertyPanel    (property_panel.py)   – property inspector UI.
"""

import uuid
import dearpygui.dearpygui as dpg

from dpg_utils import (
    apply_link_style,
    create_data_node_theme,
    create_proc_node_theme,
    create_data_node_theme_incomplete,
    create_proc_node_theme_incomplete,
)
from constants import SOCKETIO_SERVER
from node_registry import NodeRegistry
from socketio_client import SocketIOClient
from monitor_manager import MonitorManager
from property_panel import PropertyPanel

# Reference shapes (Squares) vs Data shapes (Circles)
REF_SHAPE = dpg.mvNode_PinShape_QuadFilled
DATA_SHAPE = dpg.mvNode_PinShape_CircleFilled


class NodeManager:
    """Orchestrates the DPG node editor and its supporting sub-components."""

    def __init__(
        self,
        graph_manager,
        all_templates: dict,
        socketio_server: str = SOCKETIO_SERVER,
        debug: bool = True,
    ):
        self.graph = graph_manager
        self.all_templates = all_templates
        self.debug = debug

        # --- 1. Shared registry (DPG id <-> UUID) ----------------------------
        self.registry = NodeRegistry()

        # Convenience aliases so existing callers (FileHandler, main.py) work
        # unchanged.  Both names point at the same underlying dict.
        self.dpg_to_uuid = self.registry.dpg_to_uuid
        self.uuid_to_dpg = self.registry.uuid_to_dpg
        self.input_attr_registry = self.registry.input_attr_registry
        self.output_attr_registry = self.registry.output_attr_registry
        self.link_registry = self.registry.link_registry

        # --- 2. Socket.IO client ---------------------------------------------
        self.sio_client = SocketIOClient(
            server_url=socketio_server,
            on_connect=self._on_server_connect,
            on_disconnect=self._on_server_disconnect,
            on_connect_error=self._on_server_connect_error,
            on_params=self._on_server_params,
            on_data_update=self._on_data_update,
            debug=debug,
        )

        # --- 3. Monitor manager ----------------------------------------------
        self.monitors = MonitorManager(self.sio_client, self.graph, debug=debug)

        # Backward-compat alias
        self.active_monitors = self.monitors.active_monitors

        # --- 4. Property panel -----------------------------------------------
        self.property_panel = PropertyPanel(
            graph=self.graph,
            all_templates=self.all_templates,
            registry=self.registry,
            monitor_manager=self.monitors,
            delink_callback=self.delink_callback,
            refresh_node_theme=self._refresh_node_theme,
        )

        # --- 5. UI state -----------------------------------------------------
        self._last_selected_uuid = None
        self._selected_link_id = None
        self.class_name_counters: dict = {}
        self.node_item_registry: dict = {}

        # Themes (initialised later by init_themes)
        self.data_theme = None
        self.proc_theme = None
        self.data_theme_incomplete = None
        self.proc_theme_incomplete = None

    # ------------------------------------------------------------------
    # Server event callbacks (called from SocketIOClient background thread)
    # ------------------------------------------------------------------

    def _on_server_connect(self):
        self.monitors.on_server_connect()

    def _on_server_disconnect(self):
        self.monitors.on_server_disconnect()

    def _on_server_connect_error(self, data):
        self.monitors.on_server_connect_error(data)

    def _on_server_params(self, data: dict):
        """Handle the server 'params' event: update UUID mapping, notify monitors."""
        self.sio_client.bind_nodes_to_server(self.graph.nodes, data)
        self.sio_client.update_uuid_mapping(self.graph.nodes)
        for monitor_id in self.monitors.active_monitors:
            self.monitors._safe_update_monitor_status(monitor_id, "connected")

    def _on_data_update(self, name: str, raw_data):
        self.monitors.on_data_update(name, raw_data)

    # ------------------------------------------------------------------
    # Public delegation helpers (keep the same API surface as before)
    # ------------------------------------------------------------------

    def update_property_panel(self, node_uuid: str, panel_tag: str):
        self.property_panel.update_node_panel(node_uuid, panel_tag)

    def update_connection_panel(self, link_id, panel_tag: str):
        self.property_panel.update_connection_panel(link_id, panel_tag)

    def get_connections_for_node(self, node_uuid: str):
        return self.property_panel.get_connections_for_node(node_uuid)

    def get_connection_filename(self, node_uuid, src_uuid, src_attr):
        return self.property_panel.get_connection_filename(node_uuid, src_uuid, src_attr)

    def update_connection_filename(self, node_uuid, src_uuid, src_attr, new_filename):
        self.property_panel.update_connection_filename(
            node_uuid, src_uuid, src_attr, new_filename
        )

    def is_data_class_type(self, type_name: str) -> bool:
        return self.property_panel.is_data_class_type(type_name)

    def after_dpg_init(self):
        print("[NODE_MANAGER] DPG initialised, setting up periodic tasks")
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 100, self.monitors.start_periodic_tasks)
        self.monitors.after_dpg_init()

    def start_periodic_tasks(self):
        self.monitors.start_periodic_tasks()

    def cleanup(self):
        self.monitors.cleanup()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, message: str):
        if self.debug:
            print(f"[NODE_MANAGER] {message}")

    # ------------------------------------------------------------------
    # Click / keyboard handlers
    # ------------------------------------------------------------------

    def setup_handlers(self):
        with dpg.handler_registry():
            dpg.add_mouse_click_handler(callback=self.on_click_editor)
            dpg.add_key_press_handler(key=dpg.mvKey_D, callback=self.delete_selected_link)
            dpg.add_key_press_handler(dpg.mvKey_Delete, callback=self.delete_selection)
            dpg.add_mouse_double_click_handler(callback=self._on_canvas_double_click)
            dpg.add_mouse_move_handler(callback=self._on_mouse_move)

    def on_click_editor(self, sender, app_data):
        for link_id in self.link_registry:
            if dpg.is_item_hovered(link_id):
                print(f"[CLICK] Found hovered link: {link_id}")
                self._on_link_click(sender, app_data, link_id)
                return

        if dpg.is_item_hovered("specula_editor"):
            if not self.get_selected_nodes():
                self._clear_link_selection()

        selected = self.get_selected_nodes()

        if len(selected) == 1:
            node_uuid = selected[0]
            print(f"\n[CLICK] Node clicked: {node_uuid}")
            self.debug_node_completeness(node_uuid)
            if node_uuid != self._last_selected_uuid:
                self._last_selected_uuid = node_uuid
                self._clear_link_selection()
                self.debug_node_completeness(node_uuid)
                self.update_property_panel(node_uuid, "property_panel")
        elif len(selected) == 0:
            if not self._selected_link_id:
                dpg.delete_item("property_panel", children_only=True)
                self._last_selected_uuid = None

    def _on_link_click(self, sender, app_data, link_id):
        if self._selected_link_id and self._selected_link_id != link_id:
            self._reset_link_style(self._selected_link_id)
        self._selected_link_id = link_id
        self._highlight_link(link_id)
        dpg.clear_selected_nodes("specula_editor")
        self._last_selected_uuid = None
        self.update_connection_panel(link_id, "property_panel")

    def _highlight_link(self, link_id):
        if dpg.does_item_exist(link_id):
            dpg.configure_item(link_id)

    def _reset_link_style(self, link_id):
        if not dpg.does_item_exist(link_id):
            return
        if link_id in self.link_registry:
            src_uuid, src_attr, dst_uuid, dst_attr = self.link_registry[link_id]
            if dst_attr.endswith("_ref") or "params" in dst_attr.lower():
                apply_link_style(link_id, color=[200, 200, 200, 60])
            elif ":-" in str(src_attr):
                apply_link_style(link_id, color=[255, 0, 0, 255])
            else:
                dpg.configure_item(link_id)

    def _clear_link_selection(self):
        if self._selected_link_id:
            self._reset_link_style(self._selected_link_id)
            self._selected_link_id = None

    def _on_canvas_double_click(self, sender, app_data):
        if not dpg.is_item_hovered("specula_editor"):
            return
        for link_id in self.link_registry:
            if dpg.is_item_hovered(link_id):
                self._on_link_click(sender, app_data, link_id)
                break

    def delete_selected_link(self, sender, app_data):
        if not self._selected_link_id:
            print("[LINK] No link selected to delete")
            return
        link_id = self._selected_link_id
        print(f"[LINK] Deleting selected link: {link_id}")
        self.delink_callback(sender, link_id)
        self._selected_link_id = None

    def _on_mouse_move(self, sender, app_data):
        if not dpg.is_item_hovered("specula_editor"):
            return
        for link_id in self.link_registry:
            if dpg.is_item_hovered(link_id):
                if link_id != self._selected_link_id:
                    dpg.configure_item(link_id)
                break
            else:
                if link_id != self._selected_link_id:
                    self._reset_link_style(link_id)

    # ------------------------------------------------------------------
    # Node completeness
    # ------------------------------------------------------------------

    def is_node_complete(self, node_uuid: str) -> bool:
        if node_uuid not in self.graph.nodes:
            return True
        node_data = self.graph.nodes[node_uuid]
        template = self.all_templates.get(node_data.get("type", ""), {})
        template_params = template.get("parameters", {})
        current_values = node_data.get("values", {})
        for param_name, param_meta in template_params.items():
            if isinstance(param_meta, dict) and param_meta.get("kind") == "reference":
                default_val = param_meta.get("default")
                is_required = (
                    default_val == "REQUIRED" or param_meta.get("required", False)
                )
                if is_required and not current_values.get(f"{param_name}_ref"):
                    return False
        return True

    def debug_node_completeness(self, node_uuid: str) -> bool:
        if node_uuid not in self.graph.nodes:
            print(f"[COMPLETE_DEBUG] Node {node_uuid} not found in graph")
            return False
        node_data = self.graph.nodes[node_uuid]
        node_type = node_data.get("type", "")
        node_name = node_data.get("name", "Unknown")
        print(f"\n=== NODE COMPLETENESS DEBUG ===")
        print(f"Node: {node_name} ({node_type}) | UUID: {node_uuid}")
        template = self.all_templates.get(node_type, {})
        if not template:
            print("No template found. Assuming complete.\n=== END DEBUG ===\n")
            return True
        template_params = template.get("parameters", {})
        if not template_params:
            print("No parameters. Node is complete.\n=== END DEBUG ===\n")
            return True
        current_values = node_data.get("values", {})
        complete = True
        for param_name, param_meta in template_params.items():
            if isinstance(param_meta, dict) and param_meta.get("kind") == "reference":
                ref_key = f"{param_name}_ref"
                default_val = param_meta.get("default")
                is_required = (
                    default_val == "REQUIRED" or param_meta.get("required", False)
                )
                if is_required:
                    if not current_values.get(ref_key):
                        print(f" - Missing REQUIRED reference for: {param_name} ({ref_key})")
                        complete = False
                    else:
                        print(f" + Required {param_name} connected to: {current_values[ref_key]}")
                else:
                    if current_values.get(ref_key):
                        print(f" + Optional {param_name} connected to: {current_values[ref_key]}")
                    else:
                        print(f" - Optional {param_name} not connected (OK)")
        print("Node is complete." if complete else "Node is INCOMPLETE.")
        print("=== END DEBUG ===\n")
        return complete

    # ------------------------------------------------------------------
    # Theme management
    # ------------------------------------------------------------------

    def init_themes(self):
        self.data_theme = create_data_node_theme()
        self.proc_theme = create_proc_node_theme()
        self.data_theme_incomplete = create_data_node_theme_incomplete()
        self.proc_theme_incomplete = create_proc_node_theme_incomplete()

    def _apply_node_theme(self, dpg_id, node_type: str, node_uuid: str):
        template = self.all_templates.get(node_type, {})
        category = template.get("bases", "")
        is_complete = self.is_node_complete(node_uuid)
        if "BaseDataObj" in category:
            dpg.bind_item_theme(
                dpg_id, self.data_theme if is_complete else self.data_theme_incomplete
            )
        else:
            dpg.bind_item_theme(
                dpg_id, self.proc_theme if is_complete else self.proc_theme_incomplete
            )

    def _refresh_node_theme(self, node_uuid: str):
        if node_uuid not in self.uuid_to_dpg:
            return
        dpg_id = self.uuid_to_dpg[node_uuid]
        node_data = self.graph.nodes.get(node_uuid, {})
        node_type = node_data.get("type", "")
        if dpg_id and dpg.does_item_exist(dpg_id):
            self._apply_node_theme(dpg_id, node_type, node_uuid)

    # ------------------------------------------------------------------
    # Node creation
    # ------------------------------------------------------------------

    def _generate_unique_name(self, class_name: str) -> str:
        self.class_name_counters.setdefault(class_name, 0)
        counter = self.class_name_counters[class_name]
        self.class_name_counters[class_name] += 1
        return f"a{class_name}{counter}"

    def create_node(self, node_type, pos=None, existing_uuid=None, name_override=None):
        node_uuid = existing_uuid if existing_uuid else str(uuid.uuid4())[:8]

        if node_uuid not in self.graph.nodes:
            self.graph.add_node(node_uuid, node_type)

        node_data = self.graph.nodes[node_uuid]
        template = self.all_templates.get(node_type, {})

        node_name = name_override if name_override else self._generate_unique_name(node_type)
        node_data["name"] = node_name
        final_pos = pos if pos else [100, 100]

        with dpg.node(label=node_name, parent="specula_editor") as dpg_id:
            self.node_item_registry[node_uuid] = dpg_id
            dpg.set_item_pos(dpg_id, final_pos)
            self.dpg_to_uuid[dpg_id] = node_uuid
            self.uuid_to_dpg[node_uuid] = dpg_id

            # Static header
            with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                dpg.add_text(f"Class: {node_type}", color=[130, 130, 130])
                dpg.add_spacer(width=200)

            # Reference parameter inputs
            for param_name, param_meta in template.get("parameters", {}).items():
                if isinstance(param_meta, dict) and param_meta.get("kind") == "reference":
                    display_name = f"{param_name}_ref"
                    with dpg.node_attribute(
                        attribute_type=dpg.mvNode_Attr_Input, shape=REF_SHAPE
                    ) as attr_id:
                        dpg.add_text(display_name, color=[150, 255, 150])
                        self.input_attr_registry[attr_id] = (node_uuid, display_name)

            # Standard inputs (non-reference)
            for in_attr, meta in node_data.get("inputs", {}).items():
                if in_attr.endswith("_ref") or in_attr == "layer_list":
                    continue
                kind = meta.get("kind", "single")
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Input, shape=DATA_SHAPE
                ) as attr_id:
                    label = f"{in_attr} [*]" if kind == "variadic" else in_attr
                    dpg.add_text(label, color=[255, 255, 255])
                    self.input_attr_registry[attr_id] = (node_uuid, in_attr)

            # Outputs
            if node_type == "AtmoPropagation":
                all_outputs = list(node_data.get("outputs", []))
                if "outputs_extra" in node_data:
                    all_outputs.extend(node_data["outputs_extra"])
                for out in all_outputs:
                    if out.startswith("out_' + ") and out.endswith(" + '_ef'"):
                        continue
                    if "name" in out and "+" in out and "'" in out:
                        continue
                    display_label = out.replace(":", " [") + "]" if ":" in out else out
                    with dpg.node_attribute(
                        attribute_type=dpg.mvNode_Attr_Output, shape=DATA_SHAPE
                    ) as attr_id:
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(width=100)
                            dpg.add_text(display_label)
                        self.output_attr_registry[attr_id] = (node_uuid, out)

            elif node_type == "SimulParams":
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output, shape=REF_SHAPE
                ) as attr_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=100)
                        dpg.add_text("ref", color=[150, 150, 150])
                    self.output_attr_registry[attr_id] = (node_uuid, "ref")

            else:
                all_outputs = list(node_data.get("outputs", []))
                if "outputs_extra" in node_data:
                    all_outputs.extend(node_data["outputs_extra"])
                for out in all_outputs:
                    if "name" in out and "+" in out and "'" in out:
                        continue
                    display_label = out.replace(":", " [") + "]" if ":" in out else out
                    with dpg.node_attribute(
                        attribute_type=dpg.mvNode_Attr_Output, shape=DATA_SHAPE
                    ) as attr_id:
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(width=100)
                            dpg.add_text(display_label)
                        self.output_attr_registry[attr_id] = (node_uuid, out)

            # Special overrides
            if node_type in ("Source", "Pupilstop"):
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output, shape=REF_SHAPE
                ) as attr_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=100)
                        dpg.add_text("ref", color=[100, 200, 255])
                    self.output_attr_registry[attr_id] = (node_uuid, "ref")

            category = template.get("bases", "")
            if "BaseDataObj" in category:
                dpg.bind_item_theme(dpg_id, self.data_theme)
            else:
                dpg.bind_item_theme(dpg_id, self.proc_theme)
            self._apply_node_theme(dpg_id, node_type, node_uuid)

        return node_uuid

    # ------------------------------------------------------------------
    # Dynamic output management
    # ------------------------------------------------------------------

    def _add_dynamic_atmo_output(self, in_node_uuid: str, source_name: str):
        dpg_id = self.uuid_to_dpg.get(in_node_uuid) or self.node_item_registry.get(
            in_node_uuid
        )
        if not dpg_id:
            print(f"[DYNAMIC] ERROR: No DPG ID for node {in_node_uuid}")
            return
        in_node_data = self.graph.nodes.get(in_node_uuid, {})
        if not in_node_data:
            return
        new_output = f"out_{source_name}_ef"
        in_node_data.setdefault("outputs_extra", [])
        if new_output in in_node_data["outputs_extra"]:
            return
        in_node_data["outputs_extra"].append(new_output)
        if not dpg.does_item_exist(dpg_id):
            return
        self._refresh_node_theme(in_node_uuid)
        with dpg.node_attribute(
            attribute_type=dpg.mvNode_Attr_Output, shape=DATA_SHAPE, parent=dpg_id
        ) as attr_id:
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=100)
                dpg.add_text(new_output, color=[100, 255, 255])
            self.output_attr_registry[attr_id] = (in_node_uuid, new_output)
            print(f"[DYNAMIC] Created output '{new_output}' with attr_id {attr_id}")

    def add_dynamic_io(self, node_uuid: str):
        parent = self.uuid_to_dpg[node_uuid]
        with dpg.node_attribute(
            attribute_type=dpg.mvNode_Attr_Input, parent=parent, shape=REF_SHAPE
        ) as attr_id:
            dpg.add_text("Sources (Ref)", color=[150, 255, 150])
            self.input_attr_registry[attr_id] = (node_uuid, "source_dict_ref")
        with dpg.node_attribute(
            attribute_type=dpg.mvNode_Attr_Output, parent=parent
        ) as attr_id:
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=100)
                dpg.add_text("output", color=[255, 200, 100])
            self.output_attr_registry[attr_id] = (node_uuid, "output")

    def add_data_output(self, node_uuid: str):
        parent = self.uuid_to_dpg[node_uuid]
        with dpg.node_attribute(
            attribute_type=dpg.mvNode_Attr_Output, parent=parent
        ) as attr_id:
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=100)
                dpg.add_text("Output: ref")
            self.output_attr_registry[attr_id] = (node_uuid, "ref")

    # ------------------------------------------------------------------
    # Link management
    # ------------------------------------------------------------------

    def link_callback(self, sender, app_data):
        out_attr_id, in_attr_id = app_data
        out_node_uuid, out_name = self.output_attr_registry.get(out_attr_id, (None, None))
        in_node_uuid, in_name = self.input_attr_registry.get(in_attr_id, (None, None))
        if not out_node_uuid or not in_node_uuid:
            return

        is_feedback = ":-" in str(out_name)
        connection_props = {"delay": -1 if ":-1" in str(out_name) else 0}

        link_id = dpg.add_node_link(out_attr_id, in_attr_id, parent=sender)
        self.link_registry[link_id] = (out_node_uuid, out_name, in_node_uuid, in_name)
        self.graph.add_connection(out_node_uuid, out_name, in_node_uuid, in_name, connection_props)

        dst_node = self.graph.nodes.get(in_node_uuid, {})
        src_node = self.graph.nodes.get(out_node_uuid, {})
        if not dst_node or not src_node:
            return

        dst_node.setdefault("values", {})
        src_name = src_node.get("name", out_node_uuid)
        is_ref_connection = in_name.endswith("_ref") or in_name == "layer_list"

        if is_ref_connection:
            if in_name == "source_dict_ref":
                dst_node["values"].setdefault(in_name, [])
                if src_name not in dst_node["values"][in_name]:
                    dst_node["values"][in_name].append(src_name)
                if dst_node.get("type") == "AtmoPropagation":
                    self._add_dynamic_atmo_output(in_node_uuid, src_name)
            elif in_name == "layer_list":
                dst_node["values"].setdefault(in_name, [])
                if src_name not in dst_node["values"][in_name]:
                    dst_node["values"][in_name].append(src_name)
            else:
                dst_node["values"][in_name] = src_name
                print(f"[LINK] Set reference parameter {in_name} = {src_name}")

        if is_feedback:
            apply_link_style(link_id, color=[255, 0, 0, 255])
        elif is_ref_connection:
            apply_link_style(link_id, color=[200, 200, 200, 60])

        if self._last_selected_uuid == in_node_uuid:
            self.update_property_panel(in_node_uuid, "property_panel")

        self._refresh_node_theme(in_node_uuid)
        self._refresh_node_theme(out_node_uuid)

    def delink_callback(self, sender, app_data):
        link_id = app_data
        if link_id not in self.link_registry:
            return

        src_uuid, src_attr, dst_uuid, dst_attr = self.link_registry.pop(link_id)
        self.graph.remove_connection(src_uuid, src_attr, dst_uuid, dst_attr)

        dst_node = self.graph.nodes.get(dst_uuid, {})
        src_node = self.graph.nodes.get(src_uuid, {})

        if not dst_node or not src_node:
            if dpg.does_item_exist(link_id):
                dpg.delete_item(link_id)
            return

        src_name = src_node.get("name", src_uuid)
        values = dst_node.get("values", {})

        if dst_attr == "source_dict_ref":
            lst = values.get("source_dict_ref", [])
            if src_name in lst:
                lst.remove(src_name)
            if not lst:
                values.pop("source_dict_ref", None)
            if dst_node.get("type") == "AtmoPropagation":
                dynamic_output = f"out_{src_name}_ef"
                if src_name not in values.get("source_dict_ref", []):
                    if dynamic_output in dst_node.get("outputs_extra", []):
                        dst_node["outputs_extra"].remove(dynamic_output)
                        attr_to_remove = next(
                            (
                                aid
                                for aid, (uid, name) in self.output_attr_registry.items()
                                if uid == dst_uuid and name == dynamic_output
                            ),
                            None,
                        )
                        if attr_to_remove:
                            del self.output_attr_registry[attr_to_remove]
                            if dpg.does_item_exist(attr_to_remove):
                                dpg.delete_item(attr_to_remove)

        elif dst_attr == "layer_list":
            lst = values.get("layer_list", [])
            if src_name in lst:
                lst.remove(src_name)
            if not lst:
                values.pop("layer_list", None)

        elif dst_attr.endswith("_ref"):
            if values.get(dst_attr) == src_name:
                values.pop(dst_attr, None)
                print(f"[REF] Cleared {dst_attr}")

        if self._last_selected_uuid == dst_uuid:
            self.update_property_panel(dst_uuid, "property_panel")

        if dpg.does_item_exist(link_id):
            dpg.delete_item(link_id)

        self._refresh_node_theme(dst_uuid)
        self._refresh_node_theme(src_uuid)

    def manual_link(self, src_uuid, src_attr, dst_uuid, dst_attr, delay=0) -> bool:
        is_feedback = delay == -1
        base_src_attr = src_attr

        src_id = next(
            (d for d, (u, n) in self.output_attr_registry.items()
             if u == src_uuid and n == base_src_attr),
            None,
        )
        if src_id is None:
            parent = self.uuid_to_dpg.get(src_uuid)
            if parent:
                is_ref_link = dst_attr.endswith("_ref") or "params" in dst_attr.lower()
                shape = REF_SHAPE if is_ref_link else DATA_SHAPE
                color = (
                    [255, 100, 100] if is_feedback
                    else ([150, 150, 150] if is_ref_link else [255, 255, 255])
                )
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output, parent=parent, shape=shape
                ) as new_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=100)
                        text = f"{base_src_attr}:-1" if is_feedback else base_src_attr
                        dpg.add_text(text, color=color)
                    self.output_attr_registry[new_id] = (src_uuid, base_src_attr)
                    src_id = new_id

        if dst_attr.endswith("_ref") or dst_attr == "layer_list":
            dst_node = self.graph.nodes.get(dst_uuid)
            src_node = self.graph.nodes.get(src_uuid)
            if dst_node and src_node:
                dst_node.setdefault("values", {})
                dst_node["values"][dst_attr] = src_node.get("name", src_uuid)

        dst_id = next(
            (d for d, (u, n) in self.input_attr_registry.items()
             if u == dst_uuid and n == dst_attr),
            None,
        )
        if dst_id is None:
            parent = self.uuid_to_dpg.get(dst_uuid)
            if parent:
                is_ref = dst_attr.endswith("_ref") or dst_attr == "layer_list"
                pin_shape = REF_SHAPE if is_ref else DATA_SHAPE
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Input, parent=parent, shape=pin_shape
                ) as new_id:
                    dpg.add_text(dst_attr, color=[150, 255, 150])
                    self.input_attr_registry[new_id] = (dst_uuid, dst_attr)
                    dst_id = new_id

        if src_id and dst_id:
            link_id = dpg.add_node_link(src_id, dst_id, parent="specula_editor")
            if is_feedback:
                apply_link_style(link_id, color=[255, 0, 0, 255])
            elif dst_attr.endswith("_ref") or "params" in dst_attr.lower():
                apply_link_style(link_id, color=[200, 200, 200, 60])
            self.link_registry[link_id] = (src_uuid, base_src_attr, dst_uuid, dst_attr)
            self.graph.add_connection(src_uuid, base_src_attr, dst_uuid, dst_attr, {"delay": delay})
            self._refresh_node_theme(dst_uuid)
            self._refresh_node_theme(src_uuid)
            return True

        print(f"[MANUAL_LINK] Failed: src_id={src_id}, dst_id={dst_id} "
              f"for {src_uuid}.{src_attr} -> {dst_uuid}.{dst_attr}")
        return False

    def manual_link_with_filename(self, src_uuid, src_attr, dst_uuid, dst_attr, filename):
        self.manual_link(src_uuid, src_attr, dst_uuid, dst_attr)
        self.graph.nodes[dst_uuid].setdefault("filename_map", {})
        self.graph.nodes[dst_uuid]["filename_map"][f"{src_uuid}.{src_attr}"] = filename

    # ------------------------------------------------------------------
    # Node deletion
    # ------------------------------------------------------------------

    def delete_selection(self, *_):
        for node_uuid in self.get_selected_nodes():
            self.delete_node(node_uuid)

    def delete_node(self, node_uuid: str):
        if node_uuid not in self.uuid_to_dpg:
            return
        dpg_id = self.uuid_to_dpg[node_uuid]

        for link_id in [
            lid for lid, (s, _, d, _) in list(self.link_registry.items())
            if s == node_uuid or d == node_uuid
        ]:
            if dpg.does_item_exist(link_id):
                dpg.delete_item(link_id)
            conn_data = self.link_registry.pop(link_id)
            self.graph.remove_connection(*conn_data)

        for attr in [k for k, v in self.input_attr_registry.items() if v[0] == node_uuid]:
            del self.input_attr_registry[attr]
        for attr in [k for k, v in self.output_attr_registry.items() if v[0] == node_uuid]:
            del self.output_attr_registry[attr]

        if dpg.does_item_exist(dpg_id):
            dpg.delete_item(dpg_id)

        del self.dpg_to_uuid[dpg_id]
        del self.uuid_to_dpg[node_uuid]

        if node_uuid in self.graph.nodes:
            self.graph.remove_node(node_uuid)

        print(f"Deleted node: {node_uuid}")

    def clear_all(self):
        self.registry.clear()
        dpg.delete_item("specula_editor", children_only=True)

    # ------------------------------------------------------------------
    # Selection / value helpers
    # ------------------------------------------------------------------

    def get_selected_nodes(self) -> list:
        selected_dpg_ids = dpg.get_selected_nodes("specula_editor")
        return [
            self.dpg_to_uuid[d_id]
            for d_id in selected_dpg_ids
            if d_id in self.dpg_to_uuid
        ]

    def update_node_value(self, sender, app_data, user_data):
        node_uuid, param_name = user_data
        self.graph.nodes[node_uuid]["values"][param_name] = app_data

    def get_connection_for_yaml(self, src_uuid, src_attr, dst_uuid, dst_attr) -> str:
        props = self.graph.get_connection_properties(src_uuid, src_attr, dst_uuid, dst_attr)
        delay = props.get("delay", 0)
        src_name = self.graph.nodes.get(src_uuid, {}).get("name", "")
        base_str = src_name if src_attr == "ref" else f"{src_name}.{src_attr}"
        if delay == -1:
            return f"{base_str}:-1"
        if delay != 0:
            return f"{base_str}:{delay}"
        return base_str

    # ------------------------------------------------------------------
    # Debug
    # ------------------------------------------------------------------

    def debug_panel_info(self):
        print("\n=== PANEL DEBUG INFO ===")
        print(f"'property_panel' exists: {dpg.does_item_exist('property_panel')}")
        print("=======================\n")
