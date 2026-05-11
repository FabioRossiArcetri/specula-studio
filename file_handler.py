import os
import pathlib
import json
import yaml
from collections import OrderedDict


def auto_layout_nodes(graph, uuid_to_dpg):
    """
    Simple auto-layout using a hierarchical spring-like algorithm.
    Spreads nodes to avoid overlap.
    """
    import dearpygui.dearpygui as dpg
    
    if not graph.nodes:
        return
    
    # Simple grid layout for now
    cols = max(1, int(len(graph.nodes) ** 0.5) + 1)
    spacing_x = 300
    spacing_y = 200
    
    for idx, (node_uuid, node_data) in enumerate(graph.nodes.items()):
        col = idx % cols
        row = idx // cols
        x = col * spacing_x
        y = row * spacing_y
        node_data['gui_pos'] = [x, y]
        
        dpg_id = uuid_to_dpg.get(node_uuid)
        if dpg_id and dpg.does_item_exist(dpg_id):
            dpg.configure_item(dpg_id, pos=(x, y))


class FileHandler:
    """
    Handles loading and saving of SPECULA simulation files.
    Manages YAML serialization with node graph data and override metadata.
    """
    
    def __init__(self, node_manager):
        self.nm = node_manager
        self.editor = None  # Will be set by main.py after initialization
    
    def _add_overrides_metadata(self, yaml_data: dict):
        """Add override manager metadata to YAML data."""
        if hasattr(self, 'editor') and hasattr(self.editor, 'override_manager'):
            override_meta = self.editor.override_manager.to_dict()
            if override_meta.get('overrides'):
                yaml_data['_overrides_metadata'] = override_meta
        return yaml_data
    
    def _load_overrides_metadata(self, yaml_data: dict):
        """Load override manager metadata from YAML data."""
        if '_overrides_metadata' in yaml_data and hasattr(self, 'editor'):
            meta = yaml_data.pop('_overrides_metadata')
            self.editor.override_manager.from_dict(meta)
    
    # ── YAML helpers ──────────────────────────────────────────────────────────
    
    @staticmethod
    def ordered_load(stream, Loader=yaml.SafeLoader, object_pairs_hook=OrderedDict):
        """Load YAML preserving order."""
        class OrderedLoader(Loader):
            pass
        
        def construct_mapping(loader, node):
            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))
        
        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping)
        return yaml.load(stream, OrderedLoader)
    
    @staticmethod
    def _serialize_value(value):
        """
        Recursively serialize values, handling special types.
        Converts lists to strings (e.g., for outputs), keeps dicts/primitives as-is.
        """
        if isinstance(value, dict):
            return {k: FileHandler._serialize_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            # Lists (like outputs) are serialized as comma-separated strings
            return ", ".join(str(item) for item in value)
        else:
            return value
    
    # ── Export / Save ─────────────────────────────────────────────────────────
    
    def export_simulation(self, file_path: str, include_defaults: bool = False):
        """
        Export the current simulation graph to a YAML file.
        
        The YAML structure mirrors SPECULA's format:
        - Each node becomes a top-level key (object name)
        - Node properties become YAML keys
        - Connections are stored in node 'inputs' sections
        - GUI metadata (gui_pos) is included for editor state
        - Override metadata (_overrides_metadata) is included for persistence
        """
        yaml_data = OrderedDict()
        
        # Export all nodes
        for node_uuid, node_data in self.nm.graph.nodes.items():
            node_name = node_data.get('name', node_uuid)
            node_type = node_data.get('type', 'Unknown')
            
            # Start with the class definition
            node_dict = OrderedDict()
            node_dict['class'] = node_type
            
            # Get the template for this node type
            template = self.nm.all_templates.get(node_type, {})
            template_params = template.get('parameters', {})
            
            # Add parameters from node values
            node_values = node_data.get('values', {})
            for param_name, param_value in node_values.items():
                # Skip internal keys
                if param_name.startswith('_'):
                    continue
                
                # Skip inputs (handled separately below)
                if param_name == 'inputs':
                    continue
                
                # Serialize the value
                serialized_value = self._serialize_value(param_value)
                node_dict[param_name] = serialized_value
            
            # Add inputs section (connections)
            if 'inputs' in node_values and isinstance(node_values['inputs'], dict):
                inputs_dict = OrderedDict()
                for input_name, input_value in node_values['inputs'].items():
                    if input_value is not None:
                        inputs_dict[input_name] = input_value
                
                if inputs_dict or include_defaults:
                    node_dict['inputs'] = inputs_dict if inputs_dict else {}
            elif include_defaults and 'inputs' in template_params:
                node_dict['inputs'] = {}
            
            # Add GUI metadata (for editor state recovery)
            if 'gui_pos' in node_data:
                node_dict['gui_pos'] = node_data['gui_pos']
            
            yaml_data[node_name] = node_dict
        
        # Add overrides metadata
        yaml_data = self._add_overrides_metadata(yaml_data)
        
        # Write to file
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        
        print(f"[FILE_HANDLER] Exported simulation to {file_path}")
    
    def save_simulation(self, file_path: str, include_defaults: bool = False):
        """Save the current simulation to a YAML file."""
        self.export_simulation(file_path, include_defaults=include_defaults)
    
    # ── Import / Load ─────────────────────────────────────────────────────────
    
    def load_simulation(self, file_path: str):
        """
        Load a simulation from a YAML file.
        
        Reconstructs the node graph, connections, and GUI state from the saved YAML.
        """
        file_path = str(file_path)
        
        if not os.path.isfile(file_path):
            print(f"[FILE_HANDLER] File not found: {file_path}")
            return
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                yaml_data = self.ordered_load(f)
            
            if not isinstance(yaml_data, dict):
                print(f"[FILE_HANDLER] Invalid YAML structure in {file_path}")
                return
            
            # Load overrides metadata before clearing the graph
            self._load_overrides_metadata(yaml_data)
            
            # Clear the current graph
            self.nm.clear_all()
            
            # Process each node in the YAML
            for node_name, node_dict in yaml_data.items():
                if not isinstance(node_dict, dict):
                    print(f"[FILE_HANDLER] Skipping non-dict entry: {node_name}")
                    continue
                
                # Extract class and create node
                node_type = node_dict.get('class')
                if not node_type:
                    print(f"[FILE_HANDLER] Node '{node_name}' has no 'class' defined, skipping")
                    continue
                
                if node_type not in self.nm.all_templates:
                    print(f"[FILE_HANDLER] Unknown node type '{node_type}', skipping node '{node_name}'")
                    continue
                
                # Get GUI position if available
                gui_pos = node_dict.get('gui_pos', [100, 100])
                
                # Create the node with the saved name and position
                node_uuid = self.nm.create_node(
                    node_type=node_type,
                    pos=gui_pos,
                    name_override=node_name
                )
                if not node_uuid:
                    print(f"[FILE_HANDLER] Failed to create node '{node_name}' of type '{node_type}'")
                    continue
                
                # Restore node properties
                node_data = self.nm.graph.nodes[node_uuid]
                
                # Restore parameters (everything except 'class', 'inputs', and GUI metadata)
                for key, value in node_dict.items():
                    if key in ('class', 'inputs', 'gui_pos'):
                        continue
                    
                    # Store in node values
                    if 'values' not in node_data:
                        node_data['values'] = {}
                    node_data['values'][key] = value
            
            # Second pass: restore connections
            for node_name, node_dict in yaml_data.items():
                if not isinstance(node_dict, dict):
                    continue
                
                inputs_dict = node_dict.get('inputs', {})
                if not isinstance(inputs_dict, dict):
                    continue
                
                # Find this node
                node_uuid = None
                for uuid, ndata in self.nm.graph.nodes.items():
                    if ndata.get('name') == node_name:
                        node_uuid = uuid
                        break
                
                if not node_uuid:
                    continue
                
                # Process each input connection
                for input_name, input_value in inputs_dict.items():
                    if input_value is None:
                        continue
                    
                    # Find the source node by matching output names
                    source_found = False
                    for source_uuid, source_data in self.nm.graph.nodes.items():
                        source_name = source_data.get('name')
                        if source_name == input_value:
                            # Found a match - create a manual link
                            self.nm.manual_link(source_uuid, 'ref', node_uuid, input_name)
                            source_found = True
                            break
                    
                    if not source_found:
                        print(f"[FILE_HANDLER] Could not find source '{input_value}' for input '{input_name}' in node '{node_name}'")
            
            print(f"[FILE_HANDLER] Loaded simulation from {file_path}")
        
        except Exception as e:
            print(f"[FILE_HANDLER] Error loading simulation: {e}")
            import traceback
            traceback.print_exc()
    
    # ── Node utilities ────────────────────────────────────────────────────────
    
    def get_node_template(self, node_type: str) -> dict:
        """Get the template definition for a node type."""
        return self.nm.all_templates.get(node_type, {})
    
    def get_node_defaults(self, node_type: str) -> dict:
        """Get default parameter values for a node type."""
        template = self.get_node_template(node_type)
        params = template.get('parameters', {})
        
        defaults = {}
        for param_name, param_meta in params.items():
            if 'default' in param_meta:
                defaults[param_name] = param_meta['default']
        
        return defaults