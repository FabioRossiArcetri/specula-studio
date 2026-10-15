"""
DPG node-graph UI manager and orchestrator.

After refactoring, this module is responsible ONLY for:
  - Node / link CRUD (create, delete, clear)
  - DPG attribute registries (via NodeRegistry)
  - Theme management
  - Mouse / keyboard event handlers
  - Delegation to sub-components

Three sub-components handle specialized responsibilities:
  - SocketIOClient   (socketio_client.py)  – server connection & pub/sub
  - MonitorManager   (monitor_manager.py)  – live-data monitor windows
  - PropertyPanel    (property_panel.py)   – property inspector UI
"""

import time
import uuid
import dearpygui.dearpygui as dpg

from .dpg_utils import (
    apply_link_style,
    create_data_node_theme,
    create_proc_node_theme,
    create_data_node_theme_incomplete,
    create_proc_node_theme_incomplete,
)
from .constants import SOCKETIO_SERVER
from .constants import (
    DATA_SHAPE_EMPTY,
    DATA_SHAPE_FILLED,
    DATA_MULTIPLE_SHAPE_EMPTY,
    DATA_MULTIPLE_SHAPE_FILLED,
    REF_SHAPE_EMPTY,
    REF_SHAPE_FILLED,
)
from . import render_scale
from .core import validation, yaml_io
from .core.syntax import add_ref_value, is_ref_attr, remove_ref_value, validate_node_name
from .node_registry import NodeRegistry
from .socketio_client import SocketIOClient
from .monitor_bus import MonitorBus
from .monitor_manager import MonitorManager
from .property_panel import PropertyPanel
from .theme_manager import ThemeManager
import logging

log = logging.getLogger(__name__)


# ── Documentation / class introspection (graceful fallback) ───────────────────
try:
    from .help_provider import is_input_optional as _is_input_optional
    _HELP_PROVIDER_OK = True
except ImportError:
    _HELP_PROVIDER_OK = False
    def _is_input_optional(class_name, input_name):
        return False   # conservative: treat everything as required


class NodeManager:
    """
    Orchestrates the DPG node editor and its supporting sub-components.

    Delegates specialized tasks to:
    - SocketIOClient: Server communication and real-time updates
    - MonitorManager: Live data visualization windows
    - PropertyPanel: Node and connection property inspection
    """

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

        # ===== 1. SHARED REGISTRY =====
        self.registry = NodeRegistry()

        # Convenience aliases for backward compatibility
        self.dpg_to_uuid          = self.registry.dpg_to_uuid
        self.uuid_to_dpg          = self.registry.uuid_to_dpg
        self.input_attr_registry  = self.registry.input_attr_registry
        self.output_attr_registry = self.registry.output_attr_registry
        self.link_registry        = self.registry.link_registry

        # ===== 2. SOCKET.IO CLIENT =====
        self.sio_client = SocketIOClient(
            server_url=socketio_server,
            on_connect=self._on_server_connect,
            on_disconnect=self._on_server_disconnect,
            on_connect_error=self._on_server_connect_error,
            on_params=self._on_server_params,
            on_data_update=self._on_data_update,
            debug=debug,
        )

        # ===== 3. MONITOR BUS =====
        self.monitor_bus = MonitorBus()

        # ===== 4. MONITOR MANAGER =====
        self.monitors = MonitorManager(
            sio_client=self.sio_client,
            graph=self.graph,
            monitor_bus=self.monitor_bus,
            debug=debug,
        )

        # Backward-compat alias
        self.active_monitors = self.monitors.active_monitors

        # ===== 5. PROPERTY PANEL =====
        self.property_panel = PropertyPanel(
            graph=self.graph,
            all_templates=self.all_templates,
            registry=self.registry,
            monitor_manager=self.monitors,
            delink_callback=self.delink_callback,
            refresh_node_theme=self._refresh_node_theme,
            rename_node=self.rename_node,
        )

        # ===== 6. UI STATE =====
        self._last_selected_uuid = None
        self._selected_link_id   = None
        self.class_name_counters = {}

        # ===== 7. THEMES =====
        self.data_theme             = None
        self.proc_theme             = None
        self.data_theme_incomplete  = None
        self.proc_theme_incomplete  = None

        # ===== 8. MOUSE-MOVE THROTTLE =====
        self._last_mouse_move_time: float = 0.0
        self._MOUSE_MOVE_INTERVAL: float  = 0.05   # 50 ms → ≤20 checks/s

        self.editor = None

        # ===== 9. THEME MANAGER =====
        self.theme_manager = ThemeManager()

    # ==========================================================================
    # LOGGING
    # ==========================================================================

    def _log(self, message: str):
        log.debug(message)

    # ==========================================================================
    # SERVER EVENT CALLBACKS
    # ==========================================================================

    def _on_server_connect(self):
        self.monitors.on_server_connect()

    def _on_server_disconnect(self):
        self.monitors.on_server_disconnect()

    def _on_server_connect_error(self, data):
        self.monitors.on_server_connect_error(data)

    def _on_server_params(self, data: dict):
        self.sio_client.bind_nodes_to_server(self.graph.nodes, data)
        self.sio_client.update_uuid_mapping(self.graph.nodes)
        self.monitors.on_server_params(data)

    def _on_data_update(self, name: str, raw_data):
        """Data pushed by a DisplayServer to the editor's own connection."""
        self.monitors.on_data_update(name, raw_data)

    # ==========================================================================
    # PUBLIC DELEGATION HELPERS
    # ==========================================================================

    def update_property_panel(self, node_uuid: str, panel_tag: str):
        self.property_panel.update_node_panel(node_uuid, panel_tag)

    def update_connection_panel(self, link_id, panel_tag: str):
        self.property_panel.update_connection_panel(link_id, panel_tag)

    def get_connections_for_node(self, node_uuid: str):
        return self.property_panel.get_connections_for_node(node_uuid)

    def get_connection_filename(self, node_uuid, src_uuid, src_attr):
        return self.property_panel.get_connection_filename(
            node_uuid, src_uuid, src_attr
        )

    def update_connection_filename(self, node_uuid, src_uuid, src_attr, new_filename):
        self.property_panel.update_connection_filename(
            node_uuid, src_uuid, src_attr, new_filename
        )

    def is_data_class_type(self, type_name: str) -> bool:
        return yaml_io.is_data_class_type(type_name, self.all_templates)

    def after_dpg_init(self):
        self._log("DPG initialised, setting up periodic tasks")
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 100, self.monitors.start_periodic_tasks)
        self.monitors.after_dpg_init()

    def start_periodic_tasks(self):
        self.monitors.start_periodic_tasks()

    def cleanup(self):
        self.monitors.cleanup()

    # ==========================================================================
    # THEME MANAGEMENT
    # ==========================================================================

    def init_themes(self):
        self.data_theme            = create_data_node_theme()
        self.proc_theme            = create_proc_node_theme()
        self.data_theme_incomplete = create_data_node_theme_incomplete()
        self.proc_theme_incomplete = create_proc_node_theme_incomplete()

    def _apply_node_theme(self, dpg_id, node_type: str, node_uuid: str):
        is_complete = self.is_node_complete(node_uuid)
        is_data_obj     = self._is_data_obj_node(node_type)

        if is_data_obj:
            theme = self.data_theme if is_complete else self.data_theme_incomplete
        else:
            theme = self.proc_theme if is_complete else self.proc_theme_incomplete

        if theme:
            dpg.bind_item_theme(dpg_id, theme)

    def _refresh_node_theme(self, node_uuid: str):
        if node_uuid not in self.uuid_to_dpg:
            return
        dpg_id    = self.uuid_to_dpg[node_uuid]
        node_data = self.graph.nodes.get(node_uuid, {})
        node_type = node_data.get("type", "")
        if dpg_id and dpg.does_item_exist(dpg_id):
            self._apply_node_theme(dpg_id, node_type, node_uuid)

    # ==========================================================================
    # NODE COMPLETENESS
    # ==========================================================================

    def node_issues(self, node_uuid: str) -> list:
        """Problems that make the node incomplete (see core.validation)."""
        return validation.node_issues(self.graph, node_uuid, _is_input_optional)

    def is_node_complete(self, node_uuid: str) -> bool:
        """True when all required parameters, references and inputs are set."""
        return not any(i.severity == validation.ERROR for i in self.node_issues(node_uuid))

    def debug_node_completeness(self, node_uuid: str) -> bool:
        issues = self.node_issues(node_uuid)
        name = self.graph.nodes.get(node_uuid, {}).get("name", node_uuid)
        if not issues:
            log.debug("Node %s is complete", name)
        for issue in issues:
            log.debug("Node %s incomplete: %s", name, issue.describe(self.graph))
        return not issues

    # ==========================================================================
    # NODE CREATION
    # ==========================================================================

    def _generate_unique_name(self, class_name: str) -> str:
        """Next free ``a<Class><n>`` name (skips names already in the graph)."""
        existing = self.graph.node_names()
        counter = self.class_name_counters.get(class_name, 0)
        while f"a{class_name}{counter}" in existing:
            counter += 1
        self.class_name_counters[class_name] = counter + 1
        return f"a{class_name}{counter}"

    def rename_node(self, node_uuid: str, new_name: str) -> str | None:
        """
        Rename a node, keeping everything that refers to it by name in sync.

        Returns an error message (and changes nothing) if *new_name* is invalid.
        """
        node = self.graph.nodes.get(node_uuid)
        if node is None:
            return "Unknown node"
        old_name = node.get("name")
        if new_name == old_name:
            return None
        error = validate_node_name(new_name, self.graph.node_names(exclude_uuid=node_uuid))
        if error:
            return error

        node["name"] = new_name
        self.graph.rename_references(old_name, new_name)

        dpg_id = self.uuid_to_dpg.get(node_uuid)
        if dpg_id and dpg.does_item_exist(dpg_id):
            dpg.set_item_label(dpg_id, new_name)

        # AtmoPropagation exposes one output per source: out_<source>_ef
        old_out, new_out = f"out_{old_name}_ef", f"out_{new_name}_ef"
        for atmo_uuid, atmo in self.graph.nodes.items():
            if atmo.get("type") != "AtmoPropagation":
                continue
            for key in ("outputs_extra", "file_outputs"):
                if key in atmo:
                    atmo[key] = [new_out if o == old_out else o for o in atmo[key]]
            for attr_id, (uid, name) in list(self.output_attr_registry.items()):
                if uid == atmo_uuid and name == old_out:
                    self.output_attr_registry[attr_id] = (uid, new_out)
                    self._relabel_attribute(attr_id, new_out)
            self._rename_connection_source(atmo_uuid, old_out, new_out)

        self._log(f"Renamed '{old_name}' -> '{new_name}'")
        return None

    def _relabel_attribute(self, attr_id, text: str) -> None:
        if not dpg.does_item_exist(attr_id):
            return
        stack = list(dpg.get_item_children(attr_id, slot=1) or [])
        while stack:
            child = stack.pop()
            if dpg.get_item_type(child) == "mvAppItemType::mvText":
                dpg.set_value(child, text)
                return
            stack.extend(dpg.get_item_children(child, slot=1) or [])

    def _rename_connection_source(self, src_uuid: str, old_attr: str, new_attr: str) -> None:
        for i, conn in enumerate(list(self.graph.connections)):
            if conn[0] == src_uuid and conn[1] == old_attr:
                new_conn = (src_uuid, new_attr, conn[2], conn[3])
                self.graph.connections[i] = new_conn
                props = self.graph.connection_properties.pop(conn, {"delay": 0})
                self.graph.connection_properties[new_conn] = props
        for link_id, conn in list(self.link_registry.items()):
            if conn[0] == src_uuid and conn[1] == old_attr:
                self.link_registry[link_id] = (src_uuid, new_attr, conn[2], conn[3])

    def _is_data_obj_node(self, node_type: str) -> bool:
        """Return True if *node_type* is a BaseDataObj subclass."""
        template = self.all_templates.get(node_type, {})
        bases    = template.get("bases", "")
        if isinstance(bases, list):
            return any("BaseDataObj" in b or "Layer" in b for b in bases)
        return "BaseDataObj" in str(bases) or "Layer" in str(bases)

    def create_node(self, node_type, pos=None, existing_uuid=None, name_override=None):
        """Create a new node in the graph and UI."""
        node_uuid = existing_uuid if existing_uuid else str(uuid.uuid4())[:8]

        if node_uuid not in self.graph.nodes:
            self.graph.add_node(node_uuid, node_type)

        node_data = self.graph.nodes[node_uuid]
        template  = self.all_templates.get(node_type, {})

        node_name = (
            name_override if name_override
            else self._generate_unique_name(node_type)
        )
        node_data["name"] = node_name
        final_pos = pos if pos else [100, 100]

        header_spacer_w = render_scale.node_header_spacer_width()
        tm = self.theme_manager

        with dpg.node(label=node_name, parent="specula_editor") as dpg_id:
            dpg.set_item_pos(dpg_id, final_pos)
            self.dpg_to_uuid[dpg_id]    = node_uuid
            self.uuid_to_dpg[node_uuid] = dpg_id

            # Static header
            with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                dpg.add_text(
                    f"Class: {node_type}",
                    color=tm.get_color("node_class_label")
                )
                dpg.add_spacer(width=header_spacer_w)

            # Reference parameter inputs
            param_modes = node_data.get("param_modes", {})
            for param_name, param_meta in template.get("parameters", {}).items():
                if isinstance(param_meta, dict) and param_meta.get("kind") == "reference":
                    display_name = f"{param_name}_ref"
                    mode         = param_modes.get(param_name, "ref")
                    pin_show     = (mode != "object")
                    with dpg.node_attribute(
                        attribute_type=dpg.mvNode_Attr_Input,
                        shape=REF_SHAPE_EMPTY,
                        show=pin_show,
                    ) as attr_id:
                        dpg.add_text(
                            display_name,
                            color=tm.get_color("node_ref_input_label")
                        )
                        self.input_attr_registry[attr_id] = (node_uuid, display_name)

            # Standard inputs (non-reference)
            for in_attr, meta in node_data.get("inputs", {}).items():
                if in_attr.endswith("_ref") or in_attr == "layer_list":
                    continue

                kind      = meta.get("kind", "single")
                pin_shape = (
                    DATA_MULTIPLE_SHAPE_EMPTY if kind == "variadic" else DATA_SHAPE_EMPTY
                )

                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Input, shape=pin_shape
                ) as attr_id:
                    label = f"{in_attr} [*]" if kind == "variadic" else in_attr
                    dpg.add_text(
                        label,
                        color=tm.get_color("node_input_label")
                    )
                    self.input_attr_registry[attr_id] = (node_uuid, in_attr)

            # Outputs
            self._create_node_outputs(dpg_id, node_uuid, node_type, node_data)

            # Apply theme
            self._apply_node_theme(dpg_id, node_type, node_uuid)

        return node_uuid

    def _create_node_outputs(self, dpg_id, node_uuid, node_type, node_data):
        """
        Create output pins for a node.

        Rules
        -----
        1.  Every BaseDataObj subclass gets a square REF output pin labelled
            "ref" so other processing objects can reference it via *param_ref*.
        2.  SimulParams gets only the ref pin (no regular data outputs).
        3.  AtmoPropagation gets its special dynamic output list.
        """
        output_spacer_w = render_scale.node_output_spacer_width()
        is_data_obj     = self._is_data_obj_node(node_type)
        tm = self.theme_manager

        if node_type == "SimulParams":
            with dpg.node_attribute(
                attribute_type=dpg.mvNode_Attr_Output, shape=REF_SHAPE_EMPTY
            ) as attr_id:
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=output_spacer_w)
                    dpg.add_text("ref", color=tm.get_color("node_hint_text"))
                self.output_attr_registry[attr_id] = (node_uuid, "ref")
            return

        if node_type == "AtmoPropagation":
            all_outputs = list(node_data.get("outputs", []))
            if "outputs_extra" in node_data:
                all_outputs.extend(node_data["outputs_extra"])

            for out in all_outputs:
                out_name = self._extract_output_name(out)
                if not out_name or "{" in out_name or "}" in out_name:
                    continue
                if out_name.startswith("out_' + ") and out_name.endswith(" + '_ef'"):
                    continue

                display_label = (
                    out_name.replace(":", " [") + "]" if ":" in out_name else out_name
                )
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output, shape=DATA_SHAPE_EMPTY
                ) as attr_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=output_spacer_w)
                        dpg.add_text(display_label)
                    self.output_attr_registry[attr_id] = (node_uuid, out_name)
            return

        # ── regular processing objects ────────────────────────────────────────
        all_outputs = list(node_data.get("outputs", []))
        if "outputs_extra" in node_data:
            all_outputs.extend(node_data["outputs_extra"])

        for out in all_outputs:
            out_name = self._extract_output_name(out)
            if not out_name or "{" in out_name or "}" in out_name:
                continue

            display_label = (
                out_name.replace(":", " [") + "]" if ":" in out_name else out_name
            )
            with dpg.node_attribute(
                attribute_type=dpg.mvNode_Attr_Output, shape=DATA_SHAPE_EMPTY
            ) as attr_id:
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=output_spacer_w)
                    dpg.add_text(display_label, color=tm.get_color("node_output_label"))
                self.output_attr_registry[attr_id] = (node_uuid, out_name)

        # ── ref output pin for ALL BaseDataObj nodes ──────────────────────────
        if is_data_obj:
            already_has_ref = any(
                name == "ref"
                for _, (uid, name) in self.output_attr_registry.items()
                if uid == node_uuid
            )
            if not already_has_ref:
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output, shape=REF_SHAPE_EMPTY
                ) as attr_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=output_spacer_w)
                        dpg.add_text(
                            "ref",
                            color=tm.get_color("node_data_obj_ref_output")
                        )
                    self.output_attr_registry[attr_id] = (node_uuid, "ref")

    def _extract_output_name(self, output):
        if isinstance(output, dict):
            return output.get("name")
        elif isinstance(output, str):
            return output if output else None
        return None

    def _add_dynamic_atmo_output(self, node_uuid: str, source_name: str):
        dpg_id = self.uuid_to_dpg.get(node_uuid)
        if not dpg_id or not dpg.does_item_exist(dpg_id):
            return

        node_data = self.graph.nodes.get(node_uuid, {})
        if not node_data:
            return

        new_output = f"out_{source_name}_ef"
        node_data.setdefault("outputs_extra", [])

        if new_output in node_data["outputs_extra"]:
            return

        node_data["outputs_extra"].append(new_output)
        self._refresh_node_theme(node_uuid)

        output_spacer_w = render_scale.node_output_spacer_width()
        tm = self.theme_manager
        with dpg.node_attribute(
            attribute_type=dpg.mvNode_Attr_Output,
            shape=DATA_SHAPE_EMPTY,
            parent=dpg_id,
        ) as attr_id:
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=output_spacer_w)
                dpg.add_text(
                    new_output,
                    color=tm.get_color("node_dynamic_output_label")
                )
            self.output_attr_registry[attr_id] = (node_uuid, new_output)
            self._log(f"Created dynamic output '{new_output}'")

    # ==========================================================================
    # RENDER SCALE REBUILD
    # ==========================================================================

    def current_positions(self) -> dict:
        """{node_uuid: [x, y]} for every node currently on the canvas."""
        return {
            node_uuid: list(dpg.get_item_pos(dpg_id))
            for node_uuid, dpg_id in self.uuid_to_dpg.items()
            if dpg.does_item_exist(dpg_id)
        }

    def sync_ui_from_graph(self, positions: dict | None = None):
        """
        Re-create the whole canvas from the graph model.

        The model is not modified: nodes are drawn at *positions* (falling
        back to each node's ``gui_pos``), then every connection in the model
        is drawn as a link.
        """
        positions = positions or {}
        self.registry.clear()
        dpg.delete_item("specula_editor", children_only=True)
        self._last_selected_uuid = None
        self._selected_link_id   = None

        for node_uuid, node_data in self.graph.nodes.items():
            node_type = node_data.get("type", "")
            self.create_node(
                node_type=node_type,
                pos=positions.get(node_uuid) or node_data.get("gui_pos") or [100, 100],
                existing_uuid=node_uuid,
                name_override=node_data.get("name", node_type),
            )

        for conn in list(self.graph.connections):
            delay = self.graph.connection_properties.get(conn, {}).get("delay", 0)
            if self._create_link_ui(*conn, delay=delay) is None:
                self._log(f"Could not draw connection {conn}")

        for (src_u, src_a, dst_u, dst_a) in self.graph.connections:
            self._update_input_pin_shape(dst_u, dst_a)
            self._update_output_pin_shape(src_u, src_a)
        for node_uuid in self.graph.nodes:
            self._refresh_node_theme(node_uuid)
            self.property_panel.restore_param_mode_pins(node_uuid)

    def rebuild_all_nodes_ui(self):
        """Redraw every node (render scale / theme / font change), keeping positions."""
        self._log("Rebuilding all node UI items")
        self.sync_ui_from_graph(self.current_positions())

    # ==========================================================================
    # LINK MANAGEMENT
    # ==========================================================================

    def link_callback(self, sender, app_data):
        out_attr_id, in_attr_id = app_data
        out_node_uuid, out_name = self.output_attr_registry.get(out_attr_id, (None, None))
        in_node_uuid,  in_name  = self.input_attr_registry.get(in_attr_id,  (None, None))

        if not out_node_uuid or not in_node_uuid:
            return

        if not self._can_connect_to_input(in_node_uuid, in_name):
            self._log(
                f"Connection rejected: input '{in_name}' on node "
                f"{in_node_uuid} is single and already connected"
            )
            return

        connection_props = {"delay": -1 if ":-1" in str(out_name) else 0}
        is_feedback      = ":-" in str(out_name)

        link_id = dpg.add_node_link(out_attr_id, in_attr_id, parent=sender)
        self.link_registry[link_id] = (out_node_uuid, out_name, in_node_uuid, in_name)
        self.graph.add_connection(
            out_node_uuid, out_name, in_node_uuid, in_name, connection_props
        )

        dst_node = self.graph.nodes.get(in_node_uuid, {})
        src_node = self.graph.nodes.get(out_node_uuid, {})

        if not dst_node or not src_node:
            return

        dst_node.setdefault("values", {})
        src_name          = src_node.get("name", out_node_uuid)
        is_ref_connection = in_name.endswith("_ref") or in_name == "layer_list"

        if is_ref_connection:
            add_ref_value(dst_node["values"], in_name, src_name)
            if in_name == "source_dict_ref" and dst_node.get("type") == "AtmoPropagation":
                self._add_dynamic_atmo_output(in_node_uuid, src_name)
            self._log(f"Set reference parameter {in_name} -> {src_name}")

        tm = self.theme_manager
        if is_feedback:
            apply_link_style(link_id, color=tm.get_color("node_feedback_link_color"))
        elif is_ref_connection:
            apply_link_style(link_id, color=tm.get_color("node_ref_link_color"))

        if self._last_selected_uuid == in_node_uuid:
            self.update_property_panel(in_node_uuid, "property_panel")

        self._update_input_pin_shape(in_node_uuid, in_name)
        self._update_output_pin_shape(out_node_uuid, out_name)

        self._refresh_node_theme(in_node_uuid)
        self._refresh_node_theme(out_node_uuid)

    def _can_connect_to_input(self, node_uuid: str, input_name: str) -> bool:
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            return True

        if input_name.endswith("dict_ref"):
            return True

        node_inputs = node_data.get("inputs", {})
        input_meta  = node_inputs.get(input_name, {})
        input_kind  = input_meta.get("kind", "single")

        if input_kind == "variadic":
            return True

        for _, _, dst_u, dst_a in self.graph.connections:
            if dst_u == node_uuid and dst_a == input_name:
                return False

        return True

    def _update_input_pin_shape(self, node_uuid: str, input_name: str):
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            return

        is_ref      = input_name.endswith("_ref") or input_name == "layer_list"
        node_inputs = node_data.get("inputs", {})
        input_meta  = node_inputs.get(input_name, {})
        input_kind  = input_meta.get("kind", "single")

        attr_id = None
        for aid, (uid, name) in self.input_attr_registry.items():
            if uid == node_uuid and name == input_name:
                attr_id = aid
                break

        if attr_id is None:
            return

        has_connection = any(
            dst_u == node_uuid and dst_a == input_name
            for _, _, dst_u, dst_a in self.graph.connections
        )

        if is_ref:
            new_shape = REF_SHAPE_FILLED if has_connection else REF_SHAPE_EMPTY
        else:
            if has_connection:
                new_shape = (
                    DATA_MULTIPLE_SHAPE_FILLED
                    if input_kind == "variadic"
                    else DATA_SHAPE_FILLED
                )
            else:
                new_shape = (
                    DATA_MULTIPLE_SHAPE_EMPTY
                    if input_kind == "variadic"
                    else DATA_SHAPE_EMPTY
                )

        if dpg.does_item_exist(attr_id):
            dpg.configure_item(attr_id, shape=new_shape)

    def _update_output_pin_shape(self, node_uuid: str, output_name: str):
        is_ref = output_name == "ref"

        attr_id = None
        for aid, (uid, name) in self.output_attr_registry.items():
            if uid == node_uuid and name == output_name:
                attr_id = aid
                break

        if attr_id is None:
            return

        has_connection = any(
            src_u == node_uuid and src_a == output_name
            for src_u, src_a, _, _ in self.graph.connections
        )

        if is_ref:
            new_shape = REF_SHAPE_FILLED if has_connection else REF_SHAPE_EMPTY
        else:
            new_shape = DATA_SHAPE_FILLED if has_connection else DATA_SHAPE_EMPTY

        if dpg.does_item_exist(attr_id):
            dpg.configure_item(attr_id, shape=new_shape)

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
        values   = dst_node.get("values", {})

        if dst_attr.endswith("_ref") or dst_attr == "layer_list":
            remove_ref_value(values, dst_attr, src_name)

        if dst_attr == "source_dict_ref":
            if dst_node.get("type") == "AtmoPropagation":
                dynamic_output = f"out_{src_name}_ef"
                if src_name not in values.get("source_dict_ref", []):
                    if dynamic_output in dst_node.get("file_outputs", []):
                        dst_node["file_outputs"].remove(dynamic_output)
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


        if self._last_selected_uuid == dst_uuid:
            self.update_property_panel(dst_uuid, "property_panel")

        if dpg.does_item_exist(link_id):
            dpg.delete_item(link_id)

        self._update_input_pin_shape(dst_uuid, dst_attr)
        self._update_output_pin_shape(src_uuid, src_attr)

        self._refresh_node_theme(dst_uuid)
        self._refresh_node_theme(src_uuid)

    def _create_link_ui(self, src_uuid, src_attr, dst_uuid, dst_attr, delay=0):
        """
        Draw one link on the canvas (creating missing pins on the fly) and
        register it.  The graph model is not touched.  Returns the link id,
        or None if either endpoint node is not on the canvas.
        """
        is_feedback = delay == -1
        tm = self.theme_manager

        src_id = next(
            (d for d, (u, n) in self.output_attr_registry.items()
             if u == src_uuid and n == src_attr),
            None,
        )
        if src_id is None:
            parent = self.uuid_to_dpg.get(src_uuid)
            if parent:
                is_ref_link = dst_attr.endswith("_ref") or "params" in dst_attr.lower()
                shape = REF_SHAPE_EMPTY if is_ref_link else DATA_SHAPE_EMPTY
                if is_feedback:
                    color = tm.get_color("node_feedback_link_color")
                elif is_ref_link:
                    color = tm.get_color("node_ref_input_label")
                else:
                    color = tm.get_color("node_output_label")
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output, parent=parent, shape=shape
                ) as src_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=render_scale.node_output_spacer_width())
                        dpg.add_text(f"{src_attr}:-1" if is_feedback else src_attr,
                                     color=color)
                    self.output_attr_registry[src_id] = (src_uuid, src_attr)

        dst_id = next(
            (d for d, (u, n) in self.input_attr_registry.items()
             if u == dst_uuid and n == dst_attr),
            None,
        )
        if dst_id is None:
            parent = self.uuid_to_dpg.get(dst_uuid)
            if parent:
                pin_shape = REF_SHAPE_EMPTY if is_ref_attr(dst_attr) else DATA_SHAPE_EMPTY
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Input, parent=parent, shape=pin_shape
                ) as dst_id:
                    dpg.add_text(dst_attr, color=tm.get_color("node_ref_input_label"))
                    self.input_attr_registry[dst_id] = (dst_uuid, dst_attr)

        if not (src_id and dst_id):
            return None

        link_id = dpg.add_node_link(src_id, dst_id, parent="specula_editor")
        if is_feedback:
            apply_link_style(link_id, color=tm.get_color("node_feedback_link_color"))
        elif dst_attr.endswith("_ref") or "params" in dst_attr.lower():
            apply_link_style(link_id, color=tm.get_color("node_ref_link_color"))
        self.link_registry[link_id] = (src_uuid, src_attr, dst_uuid, dst_attr)
        return link_id

    def manual_link(self, src_uuid, src_attr, dst_uuid, dst_attr, delay=0) -> bool:
        """Add a connection to the model and draw it."""
        if self._create_link_ui(src_uuid, src_attr, dst_uuid, dst_attr, delay) is None:
            self._log(f"Failed manual link: {src_uuid}.{src_attr} -> {dst_uuid}.{dst_attr}")
            return False

        self.graph.add_connection(src_uuid, src_attr, dst_uuid, dst_attr, {"delay": delay})
        if is_ref_attr(dst_attr):
            dst_node = self.graph.nodes.get(dst_uuid)
            src_node = self.graph.nodes.get(src_uuid)
            if dst_node and src_node:
                add_ref_value(dst_node.setdefault("values", {}), dst_attr,
                              src_node.get("name", src_uuid))

        self._update_input_pin_shape(dst_uuid, dst_attr)
        self._update_output_pin_shape(src_uuid, src_attr)
        self._refresh_node_theme(dst_uuid)
        self._refresh_node_theme(src_uuid)
        return True

    # ==========================================================================
    # EVENT HANDLERS
    # ==========================================================================

    def _on_link_click(self, sender, app_data, link_id):
        if self._selected_link_id and self._selected_link_id != link_id:
            self._reset_link_style(self._selected_link_id)

        self._selected_link_id = link_id
        self._highlight_link(link_id)
        dpg.clear_selected_nodes("specula_editor")
        self._last_selected_uuid = None
        self._show_connection_panel(link_id)

    def on_click_editor(self, sender, app_data):
        for link_id in self.link_registry:
            if dpg.is_item_hovered(link_id):
                self._on_link_click(sender, app_data, link_id)
                return

        if dpg.is_item_hovered("specula_editor"):
            if not self.get_selected_nodes():
                self._clear_link_selection()

        selected = self.get_selected_nodes()

        if len(selected) == 1:
            node_uuid = selected[0]
            self._log(f"Node clicked: {node_uuid}")
            if log.isEnabledFor(logging.DEBUG):
                self.debug_node_completeness(node_uuid)

            if node_uuid != self._last_selected_uuid:
                self._last_selected_uuid = node_uuid
                self._clear_link_selection()
                self._show_property_panel(node_uuid)

        elif len(selected) == 0:
            if not self._selected_link_id:
                self._hide_property_panel()
                self._last_selected_uuid = None
        else:
            self._hide_property_panel()

    def _show_property_panel(self, node_uuid: str):
        if not dpg.does_item_exist("property_panel"):
            return
        try:
            viewport_width = dpg.get_viewport_width()
            property_width = int(viewport_width * 0.25)
            dpg.configure_item("property_panel", width=property_width, show=True)
            dpg.configure_item("specula_editor_parent", width=-(property_width + 5))
            self.update_property_panel(node_uuid, "property_panel")
            self._log(f"Property panel shown for {node_uuid} (width: {property_width}px)")
        except Exception as e:
            self._log(f"Error showing property panel: {e}")

    def _hide_property_panel(self):
        if not dpg.does_item_exist("property_panel"):
            return
        try:
            dpg.configure_item("property_panel", width=0, show=False)
            dpg.delete_item("property_panel", children_only=True)
            dpg.configure_item("specula_editor_parent", width=-1)
        except Exception as e:
            self._log(f"Error hiding property panel: {e}")

    def _show_connection_panel(self, link_id: int):
        if not dpg.does_item_exist("property_panel"):
            return
        try:
            viewport_width = dpg.get_viewport_width()
            property_width = int(viewport_width * 0.25)
            dpg.configure_item("property_panel", width=property_width, show=True)
            dpg.configure_item("specula_editor_parent", width=-(property_width + 5))
            self.update_connection_panel(link_id, "property_panel")
            self._log(f"Property panel shown for connection (width: {property_width}px)")
        except Exception as e:
            self._log(f"Error showing property panel: {e}")

    def _highlight_link(self, link_id):
        if dpg.does_item_exist(link_id):
            dpg.configure_item(link_id)

    def _reset_link_style(self, link_id):
        if not dpg.does_item_exist(link_id):
            return
        if link_id in self.link_registry:
            src_uuid, src_attr, dst_uuid, dst_attr = self.link_registry[link_id]
            tm = self.theme_manager
            if dst_attr.endswith("_ref") or "params" in dst_attr.lower():
                apply_link_style(link_id, color=tm.get_color("node_ref_link_color"))
            elif ":-" in str(src_attr):
                apply_link_style(link_id, color=tm.get_color("node_feedback_link_color"))
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
            self._log("No link selected to delete")
            return
        self.delink_callback(sender, self._selected_link_id)
        self._selected_link_id = None

    def _on_mouse_move(self, sender, app_data):
        now = time.monotonic()
        if now - self._last_mouse_move_time < self._MOUSE_MOVE_INTERVAL:
            return
        self._last_mouse_move_time = now

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

    def delete_node(self, node_uuid: str):
        if node_uuid not in self.uuid_to_dpg:
            return

        dpg_id = self.uuid_to_dpg[node_uuid]

        links_to_remove = [
            lid for lid, (s, _, d, _) in list(self.link_registry.items())
            if s == node_uuid or d == node_uuid
        ]

        for link_id in links_to_remove:
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

        self._log(f"Deleted node: {node_uuid}")

    def clear_all(self):
        self.registry.clear()
        dpg.delete_item("specula_editor", children_only=True)

    def get_selected_nodes(self) -> list:
        selected_dpg_ids = dpg.get_selected_nodes("specula_editor")
        return [
            self.dpg_to_uuid[d_id]
            for d_id in selected_dpg_ids
            if d_id in self.dpg_to_uuid
        ]

    def get_connection_for_yaml(self, src_uuid, src_attr, dst_uuid, dst_attr) -> str:
        return yaml_io.connection_to_yaml(self.graph, (src_uuid, src_attr, dst_uuid, dst_attr))

