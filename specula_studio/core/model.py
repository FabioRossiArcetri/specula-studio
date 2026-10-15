"""
In-memory model of a SPECULA simulation graph (no DearPyGui dependency).

Node records are plain dicts (kept for compatibility with the UI code)::

    {
        "type":          "Modalrec",          # SPECULA class name
        "name":          "rec",               # object name in the YAML
        "values":        {...},               # parameter values as stored
        "suffixes":      set(),               # params stored as <p>_object
        "param_modes":   {p: "ref"|"object"}, # ref-or-object toggle state
        "outputs_extra": [...],               # dynamic outputs (AtmoPropagation)
        "file_outputs":  [...],               # 'outputs' list read from the file
        "filename_map":  {"<src_uuid>.<attr>": alias},  # input_list aliases
        "gui_pos":       [x, y],
        "inputs" / "outputs" / "parameters": copies of the template entries
    }

Connections are ``(src_uuid, src_attr, dst_uuid, dst_attr)`` tuples with a
properties dict (currently only ``{"delay": 0|-1}``).
"""

from __future__ import annotations

import copy
import logging

from .syntax import REQUIRED

log = logging.getLogger(__name__)

Connection = tuple  # (src_uuid, src_attr, dst_uuid, dst_attr)


class Graph:
    def __init__(self, templates: dict):
        self.templates = templates
        self.nodes: dict = {}                     # {node_uuid: node_data}
        self.connections: list = []               # [Connection]
        self.connection_properties: dict = {}     # {Connection: {"delay": int}}

    # ── nodes ─────────────────────────────────────────────────────────────────

    def add_node(self, node_uuid, node_type):
        if node_type not in self.templates:
            log.warning("Unknown node type '%s'. Using generic fallback.", node_type)
            # Create a dummy template so the editor doesn't crash
            self.templates[node_type] = {
                "inputs": {},
                "outputs": [],
                "parameters": {},
                "name": node_type,
            }

        template = self.templates[node_type]
        self.nodes[node_uuid] = {
            "type": node_type,
            "name": template.get("name", node_type),
            "inputs": copy.deepcopy(template.get("inputs", {})),
            "outputs": list(template.get("outputs", [])),
            "parameters": copy.deepcopy(template.get("parameters", {})),
            "values": {},
            "filename_map": {},
        }

        # Initialize default values.  "REQUIRED" is a template marker meaning
        # "no default" — it must never be stored (or exported) as a value.
        for param, meta in self.nodes[node_uuid]["parameters"].items():
            if isinstance(meta, dict) and "default" in meta and meta["default"] != REQUIRED:
                self.nodes[node_uuid]["values"][param] = copy.deepcopy(meta["default"])

    def remove_node(self, node_id):
        """Delete a node and its connections."""
        self.nodes.pop(node_id, None)
        for conn in [c for c in self.connections if c[0] == node_id or c[2] == node_id]:
            self.remove_connection(*conn)

    def clear(self):
        self.nodes.clear()
        self.connections.clear()
        self.connection_properties.clear()

    def node_names(self, exclude_uuid=None) -> set:
        return {nd.get("name") for u, nd in self.nodes.items() if u != exclude_uuid}

    def find_by_name(self, name: str):
        for u, nd in self.nodes.items():
            if nd.get("name") == name:
                return u
        return None

    def name_to_uuid(self) -> dict:
        return {nd.get("name"): u for u, nd in self.nodes.items()}

    def rename_references(self, old: str, new: str) -> None:
        """Replace *old* by *new* in every stored reference value."""
        for nd in self.nodes.values():
            values = nd.get("values", {})
            for key, val in list(values.items()):
                if not (key.endswith("_ref") or key == "layer_list"):
                    continue
                if val == old:
                    values[key] = new
                elif isinstance(val, list):
                    values[key] = [new if v == old else v for v in val]

    # ── connections ───────────────────────────────────────────────────────────

    def add_connection(self, output_node, output_attr, input_node, input_attr, properties=None):
        """Create a connection between an output and an input."""
        conn = (output_node, output_attr, input_node, input_attr)
        if conn not in self.connections:
            self.connections.append(conn)
        self.connection_properties[conn] = (
            dict(properties) if properties is not None else {"delay": 0}
        )
        return True

    def remove_connection(self, output_node, output_attr, input_node, input_attr):
        conn = (output_node, output_attr, input_node, input_attr)
        if conn in self.connections:
            self.connections.remove(conn)
        self.connection_properties.pop(conn, None)

    def update_connection_properties(self, output_node, output_attr, input_node, input_attr,
                                     properties):
        conn = (output_node, output_attr, input_node, input_attr)
        if conn in self.connections:
            self.connection_properties.setdefault(conn, {}).update(properties)
            return True
        return False

    def get_connection_properties(self, output_node, output_attr, input_node, input_attr):
        conn = (output_node, output_attr, input_node, input_attr)
        return self.connection_properties.get(conn, {"delay": 0})

    def incoming(self, node_uuid) -> list:
        return [c for c in self.connections if c[2] == node_uuid]

    def outgoing(self, node_uuid) -> list:
        return [c for c in self.connections if c[0] == node_uuid]

    def has_input_connection(self, node_uuid, input_name) -> bool:
        return any(d == node_uuid and a == input_name for _, _, d, a in self.connections)

    # ── snapshots (undo / overrides) ──────────────────────────────────────────

    def snapshot(self) -> dict:
        """Deep copy of the full model state (templates excluded)."""
        return copy.deepcopy({
            "nodes": self.nodes,
            "connections": self.connections,
            "connection_properties": self.connection_properties,
        })

    def restore(self, snap: dict) -> None:
        snap = copy.deepcopy(snap)
        self.nodes.clear()
        self.nodes.update(snap["nodes"])
        self.connections[:] = snap["connections"]
        self.connection_properties.clear()
        self.connection_properties.update(snap["connection_properties"])


# Backward-compatible name used throughout the UI code.
GraphManager = Graph
