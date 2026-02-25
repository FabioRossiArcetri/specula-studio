class GraphManager:
    def __init__(self, templates):
        self.templates = templates
        self.nodes = {}  # {node_id: node_data}
        self.connections = []  # List of tuples (output_node, output_attr, input_node, input_attr)
        self.connection_properties = {}  # NEW: Store properties for connections
    
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
            "filename_map": {}  # Store filenames for DataStore connections
        }        

        # Initialize default values
        for param, meta in self.nodes[node_uuid]["parameters"].items():
            if "default" in meta:
                self.nodes[node_uuid]["values"][param] = meta["default"]

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
    