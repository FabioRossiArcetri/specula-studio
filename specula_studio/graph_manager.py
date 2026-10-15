import logging

log = logging.getLogger(__name__)

# Template marker for parameters without a default value.
REQUIRED = "REQUIRED"


def is_list_ref(attr: str) -> bool:
    """True for reference attributes that hold a *list* of object names."""
    return attr == "layer_list" or attr.endswith(("_list_ref", "_dict_ref"))


def add_ref_value(values: dict, attr: str, name: str) -> None:
    """Record that reference *attr* points to object *name*."""
    if is_list_ref(attr):
        current = values.get(attr)
        if not isinstance(current, list):
            current = [] if current in (None, "") else [current]
        if name not in current:
            current.append(name)
        values[attr] = current
    else:
        values[attr] = name


def remove_ref_value(values: dict, attr: str, name: str) -> None:
    """Undo :func:`add_ref_value`; drops the key when nothing is left."""
    current = values.get(attr)
    if isinstance(current, list):
        if name in current:
            current.remove(name)
        if not current:
            values.pop(attr, None)
    elif current == name:
        values.pop(attr, None)


# SPECULA rejects these in object names (simul.validate_section_names); '-'
# separates an input_list alias, '.' the output key and ':' the delay.
_FORBIDDEN_NAME_CHARS = ".-:"


def validate_node_name(name: str, existing_names=()) -> str | None:
    """Return an error message if *name* is not a valid object name, else None."""
    if not name or not name.strip():
        return "Name cannot be empty"
    if any(c.isspace() for c in name):
        return "Name cannot contain whitespace"
    bad = sorted({c for c in name if c in _FORBIDDEN_NAME_CHARS})
    if bad:
        return f"Name cannot contain {' '.join(repr(c) for c in bad)}"
    if name.startswith("_"):
        return "Names starting with '_' are reserved"
    if name in existing_names:
        return f"An object named '{name}' already exists"
    return None


class GraphManager:
    def __init__(self, templates):
        self.templates = templates
        self.nodes = {}  # {node_id: node_data}
        self.connections = []  # List of tuples (output_node, output_attr, input_node, input_attr)
        self.connection_properties = {}  # NEW: Store properties for connections

    def add_node(self, node_uuid, node_type):
        # --- ROBUSTNESS FIX ---
        if node_type not in self.templates:
            log.warning(f"Warning: Unknown node type '{node_type}'. Using generic fallback.")
            # Create a dummy template so the editor doesn't crash
            self.templates[node_type] = {
                "inputs": {},      # No inputs known yet
                "outputs": [],     # No outputs known yet
                "parameters": {},  # No parameters known yet
                "name": node_type  # Default name
            }
        # ----------------------

        template = self.templates[node_type]

        # Deep copy the template structure for the new instance
        self.nodes[node_uuid] = {
            "type": node_type,
            "name": template.get("name", node_type),
            "inputs": template.get("inputs", {}).copy(),
            "outputs": list(template.get("outputs", [])),
            "parameters": template.get("parameters", {}).copy(),
            "values": {},  # Stores the actual user-set values
            "filename_map": {}  # Store filenames for DataStore connections
        }

        # Initialize default values.  "REQUIRED" is a template marker meaning
        # "no default" — it must never be stored (or exported) as a value.
        for param, meta in self.nodes[node_uuid]["parameters"].items():
            if "default" in meta and meta["default"] != REQUIRED:
                self.nodes[node_uuid]["values"][param] = meta["default"]

    def node_names(self, exclude_uuid=None) -> set:
        return {
            nd.get("name") for u, nd in self.nodes.items() if u != exclude_uuid
        }

    def find_by_name(self, name: str):
        for u, nd in self.nodes.items():
            if nd.get("name") == name:
                return u
        return None

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

    def remove_node(self, node_id):
        """Delete a node and its connections."""
        if node_id in self.nodes:
            del self.nodes[node_id]
        # Also remove any connection properties for connections involving this node
        connections_to_remove = []
        for conn in self.connections:
            if conn[0] == node_id or conn[2] == node_id:
                connections_to_remove.append(conn)

        for conn in connections_to_remove:
            self.remove_connection(*conn)

    def add_connection(self, output_node, output_attr, input_node, input_attr, properties=None):
        """Create a connection between an output and an input."""
        conn = (output_node, output_attr, input_node, input_attr)
        self.connections.append(conn)

        # Initialize properties if provided
        if properties is not None:
            self.connection_properties[conn] = properties.copy()
        else:
            self.connection_properties[conn] = {"delay": 0}  # Default delay is 0

        return True

    def remove_connection(self, output_node, output_attr, input_node, input_attr):
        """Remove a connection."""
        conn = (output_node, output_attr, input_node, input_attr)
        if conn in self.connections:
            self.connections.remove(conn)
        # Also remove any properties
        if conn in self.connection_properties:
            del self.connection_properties[conn]


    def update_connection_properties(self, output_node, output_attr, input_node, input_attr, properties):
        """Update properties for a specific connection."""
        conn = (output_node, output_attr, input_node, input_attr)
        if conn in self.connections:
            if conn not in self.connection_properties:
                self.connection_properties[conn] = {}
            self.connection_properties[conn].update(properties)
            return True
        return False

    def get_connection_properties(self, output_node, output_attr, input_node, input_attr):
        """Get properties for a specific connection."""
        conn = (output_node, output_attr, input_node, input_attr)
        return self.connection_properties.get(conn, {"delay": 0})
