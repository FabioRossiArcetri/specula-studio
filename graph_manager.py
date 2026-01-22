class GraphManager:
    def __init__(self, templates):
        self.templates = templates
        self.nodes = {}  # {node_id: node_data}
        self.connections = []

    def add_node(self, node_uuid, node_type):
        # --- ROBUSTNESS FIX ---
        if node_type not in self.templates:
            print(f"Warning: Unknown node type '{node_type}'. Using generic fallback.")
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
            "filename_map": {}  # NEW: Store filenames for DataStore connections
        }        

        # Initialize default values
        for param, meta in self.nodes[node_uuid]["parameters"].items():
            if "default" in meta:
                self.nodes[node_uuid]["values"][param] = meta["default"]

    def remove_node(self, node_id):
        """Delete a node and its connections."""
        if node_id in self.nodes:
            del self.nodes[node_id]
        self.connections = [c for c in self.connections if c[0] != node_id and c[2] != node_id]

    def add_connection(self, output_node, output_attr, input_node, input_attr):
        """Create a connection between an output and an input."""
        self.connections.append((output_node, output_attr, input_node, input_attr))
        return True

    def remove_connection(self, output_node, output_attr, input_node, input_attr):
        """Remove a connection."""
        self.connections.remove((output_node, output_attr, input_node, input_attr))

