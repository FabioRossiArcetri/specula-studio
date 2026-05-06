import yaml
import os
import dearpygui.dearpygui as dpg
from dpg_utils import auto_layout_nodes
import uuid
import traceback

class FileHandler:
    def __init__(self, node_manager):
        self.nm = node_manager

    # Refresh all node themes after import
    def refresh_all_themes(self):
        for node_uuid in self.nm.graph.nodes:
            self.nm._refresh_node_theme(node_uuid)

    # Schedule UI update for imported values
    def update_ui_values(self):
        for u_id, node_data in self.nm.graph.nodes.items():
            if u_id in self.nm.uuid_to_dpg and 'values' in node_data:
                # Update property panel if this node is selected
                if self.nm._last_selected_uuid == u_id:
                    self.nm.update_property_panel(u_id, "property_panel")

    def _load_yaml_file(self, file_path):
        """Load and validate YAML file. Returns parsed data or None."""
        try:
            with open(file_path, "r") as f:
                data = yaml.safe_load(f)
            
            if not isinstance(data, dict):
                print(f"[FILE_HANDLER] Error: YAML root must be a mapping, got {type(data)}")
                return None
            
            return data
        except Exception as e:
            print(f"[FILE_HANDLER] Error loading YAML file: {e}")
            return None

    def _populate_graph_from_yaml(self, yaml_data):
        """
        Populate graph model from YAML data (Pass 1).
        
        Creates nodes in the graph model and loads their parameter values,
        but does not create UI elements or connections yet.
        
        Args:
            yaml_data (dict): Parsed YAML data
            
        Returns:
            dict: Mapping of node names to UUIDs
        """
        name_to_uuid = {}

        for node_name, content in yaml_data.items():
            if not isinstance(content, dict):
                print(f"[FILE_HANDLER] Warning: Skipping '{node_name}' — expected dict, got {type(content)}")
                continue
            if 'class' not in content:
                print(f"[FILE_HANDLER] Warning: Skipping '{node_name}' — missing 'class' key")
                continue

            node_type = content.get('class')
            u = str(uuid.uuid4())[:8]
            name_to_uuid[node_name] = u
            
            self.nm.graph.add_node(u, node_type)
            node_data = self.nm.graph.nodes[u]
            node_data['name'] = node_name
            node_data['outputs_extra'] = [] 
            node_data['suffixes'] = set()
            node_data['values'] = {}
            
            # Store position if available
            if 'gui_pos' in content:
                node_data['gui_pos'] = content['gui_pos']
            
            # IMPORTANT: Import ALL parameter values from YAML
            template = self.nm.all_templates.get(node_type, {})
            template_params = template.get('parameters', {})
            
            # Process all key-value pairs in the content
            for key, value in content.items():
                # Skip reserved fields
                if key in ['class', 'inputs', 'outputs', 'gui_pos']:
                    continue
                
                # Skip reference connections (handled later)
                if key.endswith('_ref') or key == 'layer_list':
                    continue
                
                # Check if this is an object parameter with _object suffix
                if key.endswith('_object'):
                    base_key = key[:-7]  # Remove '_object' suffix
                    
                    # Check if the base parameter is in the template
                    if base_key in template_params:
                        param_meta = template_params[base_key]
                        param_kind = param_meta.get('kind', 'value')
                        
                        # For object parameters
                        if param_kind == 'object' or key.endswith('_object'):
                            node_data['suffixes'].add(base_key)
                            node_data['values'][base_key] = value
                            print(f"[FILE_HANDLER] Imported object parameter: {base_key} = {value}")
                        else:
                            # Base key exists but not as an object - still import it
                            node_data['values'][base_key] = value
                            print(f"[FILE_HANDLER] Imported parameter: {base_key} = {value}")
                    else:
                        # Base key not in template, store under base name anyway
                        node_data['suffixes'].add(base_key)
                        node_data['values'][base_key] = value
                        print(f"[FILE_HANDLER] Imported unknown object parameter: {base_key} = {value}")
                    
                # Check if this is a template parameter (non-object version)
                elif key in template_params:
                    param_meta = template_params[key]
                    param_kind = param_meta.get('kind', 'value')
                    
                    # For object parameters with suffix
                    if param_kind == 'object':
                        node_data['suffixes'].add(key)
                        node_data['values'][key] = value
                    else:
                        # Regular parameter - store directly
                        node_data['values'][key] = value
                else:
                    # Not in template, but exists in YAML - store it anyway
                    node_data['values'][key] = value

        return name_to_uuid

    def _create_ui_nodes(self, yaml_data, name_to_uuid):
        """
        Create UI nodes from graph model (Pass 2).
        
        Creates the actual DPG node elements with positions.
        
        Args:
            yaml_data (dict): Original YAML data (for positions)
            name_to_uuid (dict): Mapping of node names to UUIDs
        """
        for node_name, content in yaml_data.items():
            u = name_to_uuid[node_name]
            pos = content.get('gui_pos', [100, 100])
            self.nm.create_node(content['class'], pos=pos, existing_uuid=u, name_override=node_name)
        
        # Let DPG process the node creations
        dpg.split_frame()
        dpg.split_frame()

    def _create_connections(self, yaml_data, name_to_uuid):
        """
        Create connections between nodes (Pass 3).
        
        Processes inputs and reference links from YAML and creates connections
        in both the graph model and the UI.
        
        Args:
            yaml_data (dict): Original YAML data (for connections)
            name_to_uuid (dict): Mapping of node names to UUIDs
        """
        connections_to_create = []
        
        for node_name, content in yaml_data.items():
            dst_u = name_to_uuid.get(node_name)
            if not dst_u: 
                continue

            # Standard Inputs
            if "inputs" in content:
                for in_pin, src_raw in content["inputs"].items():
                    sources = src_raw if isinstance(src_raw, list) else [src_raw]
                    
                    for s in sources:
                        if not isinstance(s, str): 
                            continue
                        
                        # DataStore input_list with filename
                        if in_pin == "input_list" and "-" in s:
                            filename, node_and_attr = s.split("-", 1)
                            src_node_name, src_attr, delay = self._parse_source_info(node_and_attr)
                            
                            if src_node_name in name_to_uuid:
                                connections_to_create.append((
                                    name_to_uuid[src_node_name], src_attr, dst_u, in_pin, delay, filename
                                ))
                            continue
                        
                        # Regular connection
                        actual_source = s
                        src_node_name, src_attr, delay = self._parse_source_info(actual_source)
                        
                        if src_node_name in name_to_uuid:
                            connections_to_create.append((
                                name_to_uuid[src_node_name], src_attr, dst_u, in_pin, delay, None
                            ))

            # Reference Links
            for key, val in content.items():
                if key.endswith("_ref") or key == "layer_list":
                    refs = val if isinstance(val, list) else [val]
                    connection_key = key
                    
                    for r_name in refs:
                        if r_name in name_to_uuid:
                            connections_to_create.append((
                                name_to_uuid[r_name], "ref", dst_u, connection_key, 0, None
                            ))
                            
                            # Store in values for display
                            dst_node_data = self.nm.graph.nodes[dst_u]
                            if 'values' not in dst_node_data:
                                dst_node_data['values'] = {}
                            
                            if key in ['source_dict_ref', 'layer_list']:
                                if key not in dst_node_data['values']:
                                    dst_node_data['values'][key] = []
                                if r_name not in dst_node_data['values'][key]:
                                    dst_node_data['values'][key].append(r_name)
                            else:
                                dst_node_data['values'][key] = r_name
        
        # Create all connections
        for src_u, src_a, dst_u, dst_a, delay, filename in connections_to_create:
            self.nm.manual_link(src_u, src_a, dst_u, dst_a, delay=delay)
            
            if filename and dst_a == "input_list":
                if 'filename_map' not in self.nm.graph.nodes[dst_u]:
                    self.nm.graph.nodes[dst_u]['filename_map'] = {}
                conn_key = f"{src_u}.{src_a}"
                self.nm.graph.nodes[dst_u]['filename_map'][conn_key] = filename

    def _finalize_load(self, perform_auto_layout=True, operation_name="LOAD"):
        """
        Finalize a load operation with theme refresh and optional auto-layout.
        
        Args:
            perform_auto_layout (bool): Whether to perform auto-layout on nodes
            operation_name (str): Name for logging (e.g., "IMPORT" or "LOAD")
        """
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 3, self.refresh_all_themes)
        
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 3, self.update_ui_values)
        
        def verify_nodes(attempt=1, max_attempts=5):
            """Verify all nodes are loaded and perform layout if needed."""
            missing_nodes = []
            for node_id in self.nm.graph.nodes:
                if node_id not in self.nm.uuid_to_dpg:
                    missing_nodes.append(node_id)
            
            if missing_nodes and attempt < max_attempts:
                print(f"[{operation_name}] Attempt {attempt}: {len(missing_nodes)} nodes missing DPG IDs, retrying...")
                dpg.set_frame_callback(dpg.get_frame_count() + 5, 
                                    lambda: verify_nodes(attempt + 1, max_attempts))
                return
            
            if missing_nodes:
                print(f"[{operation_name}] Failed after {max_attempts} attempts. {len(missing_nodes)} nodes still missing DPG IDs")
                return
            
            if perform_auto_layout:
                print(f"[{operation_name}] All nodes have DPG IDs, performing layout...")
                try:                
                    auto_layout_nodes(self.nm.graph, self.nm.uuid_to_dpg)
                    print(f"[{operation_name}] Layout completed successfully")
                except Exception as e:
                    print(f"[{operation_name}] Layout error: {e}")                
                    traceback.print_exc()
            else:
                print(f"[{operation_name}] All {len(self.nm.graph.nodes)} nodes loaded successfully with saved positions")
        
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 10, lambda: verify_nodes(1, 5))

    def load_simulation(self, file_path,include_defaults=False):
        """
        Load a saved simulation from YAML.
        
        Loads a complete simulation including all nodes, connections, and positions
        as they were when saved. This respects saved node positions and does NOT 
        perform auto-layout.
        
        Args:
            file_path (str): Path to the simulation YAML file
            include_defaults (bool): Whether to include default values when loading
        """
        yaml_data = self._load_yaml_file(file_path)
        if yaml_data is None:
            return
        
        # Clear existing graph
        self.nm.clear_all()
        self.nm.graph.nodes.clear()
        self.nm.graph.connections.clear()
        self.nm.graph.connection_properties.clear()
        
        # Load in three passes
        name_to_uuid = self._populate_graph_from_yaml(yaml_data)
        self._create_ui_nodes(yaml_data, name_to_uuid)
        self._create_connections(yaml_data, name_to_uuid)
        
        # Finalize WITHOUT auto-layout (preserve saved positions)
        self._finalize_load(perform_auto_layout=False, operation_name="LOAD")
        
        print(f"[LOAD] Simulation loaded from {file_path}")

    def save_simulation(self, file_path):
        """
        Save the current simulation layout to YAML.
        
        Exports the entire simulation with node positions preserved.
        Captures the current position of each node from the DPG node editor.
        
        Args:
            file_path (str): Path to save the simulation file
        """
        # First, capture current positions from DPG
        for node_uuid, dpg_id in self.nm.uuid_to_dpg.items():
            if node_uuid in self.nm.graph.nodes:
                node_data = self.nm.graph.nodes[node_uuid]
                # Get current position from the DPG node
                if dpg.does_item_exist(dpg_id):
                    current_pos = dpg.get_item_pos(dpg_id)
                    node_data['gui_pos'] = current_pos
        
        # Export the simulation (which now includes positions)
        self.export_simulation(file_path, include_defaults=False)
        print(f"[SAVE] Simulation saved to {file_path}")

    def export_simulation(self, file_path, include_defaults=False):
        """Exports the graph state to YAML."""
        export_data = {}

        for u_id, node_data in self.nm.graph.nodes.items():
            node_type = node_data['type']
            node_name = node_data.get('name', node_type)
            
            template = self.nm.all_templates.get(node_type, {})
            template_params = template.get('parameters', {})

            # 1. Initialize structure
            node_dict = {
                'class': node_type
            }

            # Add position if available
            if 'gui_pos' in node_data:
                node_dict['gui_pos'] = node_data['gui_pos']

            # --- 2. OUTPUTS LOGIC ---
            # Handle outputs for all nodes
            all_outputs = []
            
            # Get template outputs
            standard_outputs = template.get('outputs', [])
            if isinstance(standard_outputs, list):
                for out in standard_outputs:
                    if isinstance(out, str):
                        # Skip template placeholders
                        if "name" in out and "+" in out and "'" in out:
                            continue
                        if ":" in out:  # Skip indexed outputs
                            continue
                        if out not in all_outputs:
                            all_outputs.append(out)
            
            # Get extra outputs
            extra_outputs = node_data.get('outputs_extra', [])
            if isinstance(extra_outputs, list):
                for out in extra_outputs:
                    if isinstance(out, str) and out not in all_outputs:
                        all_outputs.append(out)
            
            # Special handling for AtmoPropagation - ensure we have outputs
            if node_type == "AtmoPropagation":
                # Find connected sources to generate proper output names
                connected_sources = []
                for (src_u, src_at, dst_u, dst_at) in self.nm.graph.connections:
                    if dst_u == u_id and dst_at == "source_dict_ref":
                        src_node_name = self.nm.graph.nodes[src_u].get('name', "unknown")
                        if src_node_name not in connected_sources:
                            connected_sources.append(src_node_name)
                
                # Generate outputs based on connected sources
                for source_name in connected_sources:
                    output_name = f"out_{source_name}_ef"
                    if output_name not in all_outputs:
                        all_outputs.append(output_name)
            
            # Set outputs in node_dict if we have any
            if all_outputs:
                node_dict['outputs'] = all_outputs

            # --- 3. PARAMETERS LOGIC ---
            current_values = node_data.get('values', {})
            suffixes = node_data.get('suffixes', set())

            # Track which parameters have been handled
            handled_params = set()

            for p_name, p_meta in template_params.items():
                # Skip reference parameters entirely - they should ONLY appear as connections
                if p_meta.get('kind') == 'reference':
                    handled_params.add(p_name)
                    continue
                
                val = current_values.get(p_name)
                kind = p_meta.get('kind', 'value')
                
                default_val = p_meta.get('default')

                # FIXED: Better default value comparison
                # Skip if we're not including defaults AND the value matches the default
                if not include_defaults:
                    # Handle special cases for default comparison
                    should_skip = False
                    
                    # Case 1: Both None
                    if val is None and default_val is None:
                        should_skip = True
                    # Case 2: Both are strings and equal (case-insensitive for certain defaults)
                    elif isinstance(val, str) and isinstance(default_val, str):
                        # For "REQUIRED", never skip
                        if default_val == "REQUIRED":
                            should_skip = False
                        else:
                            should_skip = (val.lower() == default_val.lower())
                    # Case 3: Both are numbers/booleans/lists with same value
                    elif val == default_val:
                        should_skip = True
                    # Case 4: Special handling for empty strings vs None
                    elif val == "" and default_val is None:
                        should_skip = True
                    elif val is None and default_val == "":
                        should_skip = True
                    
                    if should_skip:
                        handled_params.add(p_name)
                        continue
                
                # Determine the correct export key
                export_key = p_name
                
                # Condition A: It was explicitly imported with a suffix
                if p_name in suffixes:
                    export_key = f"{p_name}_object"
                
                # Condition B: It's an 'object' kind parameter
                elif kind == 'object':
                    export_key = f"{p_name}_object"
                
                # Condition C: Heuristic for new nodes created in Editor
                elif isinstance(val, str) and self.nm.is_data_class_type(p_meta.get('type')):
                    export_key = f"{p_name}_object"

                if val is not None:
                    node_dict[export_key] = val
                handled_params.add(p_name)

            # Also handle any values that aren't in template but exist in current_values
            for key, val in current_values.items():
                if key not in handled_params:
                    # Check if this is a reference parameter (ends with _ref)
                    if key.endswith("_ref"):
                        # This should be handled as a connection, not a parameter
                        continue
                    # Check if it's an object parameter with suffix
                    elif key in suffixes or f"{key}_object" in current_values:
                        export_key = f"{key}_object" if key in suffixes else key
                        node_dict[export_key] = val
                    else:
                        # Regular parameter
                        node_dict[key] = val

            # --- 4. CONNECTIONS (Inputs & References) ---
            # First, collect all connections for this node
            input_connections = {}
            ref_connections = {}
            

            # In the connection processing loop:
            for (src_u, src_at, dst_u, dst_at) in self.nm.graph.connections:
                if dst_u == u_id:
                    # Use the new method to format the connection
                    connection_str = self.nm.get_connection_for_yaml(src_u, src_at, dst_u, dst_at)
                    
                    # Check if this is a reference connection
                    is_ref_connection = False
                                        
                    # Check template for reference parameters
                    if dst_at in template_params:
                        param_meta = template_params[dst_at]
                        if isinstance(param_meta, dict) and param_meta.get("kind") == "reference":
                            is_ref_connection = True
                    
                    # Also check attribute name patterns
                    if not is_ref_connection and (dst_at.endswith("_ref") or dst_at == "layer_list"):
                        is_ref_connection = True
                    

                    if is_ref_connection:
                        # For reference connections
                        param_name = dst_at
                        if param_name not in ref_connections:
                            ref_connections[param_name] = []
                        if connection_str not in ref_connections[param_name]:
                            ref_connections[param_name].append(connection_str)
                    else:
                        # For regular data inputs
                        # For DataStore input_list, add filename prefix
                        if dst_at == "input_list":
                            # Get filename for this connection
                            filename = "data"  # default
                            if 'filename_map' in node_data:
                                conn_key = f"{src_u}.{src_at}"
                                filename = node_data['filename_map'].get(conn_key, "data")
                            
                            connection_str = f"{filename}-{connection_str}"
                        
                        if dst_at not in input_connections:
                            input_connections[dst_at] = []
                        if connection_str not in input_connections[dst_at]:
                            input_connections[dst_at].append(connection_str)
            
            # Process regular inputs (data connections)
            if input_connections:
                node_dict['inputs'] = {}
                for dst_at, sources in input_connections.items():
                    # Check if this is a DataStore input_list
                    if dst_at == "input_list":
                        node_dict['inputs'][dst_at] = sources
                    else:
                        # For other inputs, decide whether to output as list or single string
                        # Based on the original YAML patterns
                        if dst_at in ['atmo_layer_list', 'common_layer_list']:
                            # These are always lists in the original YAML
                            node_dict['inputs'][dst_at] = sources
                        elif len(sources) > 1:
                            node_dict['inputs'][dst_at] = sources
                        else:
                            node_dict['inputs'][dst_at] = sources[0]
            
            # Process reference connections
            for param_name, sources in ref_connections.items():
                # source_dict_ref and layer_list are ALWAYS lists
                if param_name in ['source_dict_ref', 'layer_list']:
                    node_dict[param_name] = sources
                # For other reference parameters
                else:
                    # Add _ref suffix if not already present
                    if not param_name.endswith("_ref"):
                        export_param_name = f"{param_name}_ref"
                    else:
                        export_param_name = param_name
                    
                    # Single reference parameters: use string if only one source
                    template_param = template_params.get(param_name, {})
                    if isinstance(template_param, dict) and template_param.get('kind') == 'reference':
                        # Check if the template indicates it's a list
                        type_hint = template_param.get('type', '')
                        if 'list' in str(type_hint).lower() or len(sources) > 1:
                            node_dict[export_param_name] = sources
                        else:
                            node_dict[export_param_name] = sources[0] if sources else None
                    else:
                        # Default: string if single, list if multiple
                        if len(sources) > 1:
                            node_dict[export_param_name] = sources
                        else:
                            node_dict[export_param_name] = sources[0] if sources else None

            export_data[node_name] = node_dict

        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(export_data, f, sort_keys=False, default_flow_style=False)
            
        print(f"Exported simulation to {file_path}")

    def _parse_source_info(self, source_val):
        """Handles strings, lists, and indexed attributes from Specula YAML.
        Returns (node_name, attr_name, delay)
        """
        delay = 0  # Default no delay

        # Handle cases where it's a list
        if isinstance(source_val, list):
            # Caller should iterate; parse only one at a time
            # Raise or return list of results instead of silently dropping
            if not source_val:
                return None, None, 0
            source_val = source_val[0]  # document this is intentional if kept

        if isinstance(source_val, str):
            # Check for delay suffix (e.g., "out_layer:-1")
            if ":-" in source_val:
                # Split into base and delay
                base_part, delay_part = source_val.rsplit(":-", 1)
                try:
                    delay = -int(delay_part)  # Note: in YAML it's :-1, so delay = -1
                except ValueError:
                    delay = 0
                    base_part = source_val
            else:
                base_part = source_val
            
            # Now parse the base part (node.attr)
            if "." in base_part:
                parts = base_part.split(".")
                node_name = parts[0]
                attr_name = ".".join(parts[1:])
            else:
                # If no dot (e.g., just a node name for reference), default to 'ref' pin
                node_name = base_part
                attr_name = "ref"
            
            return node_name, attr_name, delay
        
        return None, None, 0