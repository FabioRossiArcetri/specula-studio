import yaml
import os
import dearpygui.dearpygui as dpg
from dpg_utils import auto_layout_nodes
import uuid
import traceback
import ast

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

    def import_simulation(self, file_path):
        with open(file_path, "r") as f:
            sim_data = yaml.safe_load(f)

        if not isinstance(sim_data, dict):
            print(f"[IMPORT] Error: YAML root must be a mapping, got {type(sim_data)}")
            return
        
        # Clear existing graph
        self.nm.clear_all()
        name_to_uuid = {}

        # --- PASS 1: Create UI Nodes first ---
        # This initializes the graph model structure and DPG items.
        for node_name, content in sim_data.items():
            if not isinstance(content, dict) or 'class' not in content:
                continue

            u = str(uuid.uuid4())[:8]
            name_to_uuid[node_name] = u
            pos = content.get('pos', [100, 100])
            
            # This ensures the node is fully registered in DPG and GraphManager
            self.nm.create_node(content['class'], pos=pos, existing_uuid=u, name_override=node_name)
        
        # Allow DPG to process node creations before setting values or links
        dpg.split_frame()
        dpg.split_frame()

        # --- PASS 2: Apply Parameters & Sync UI ---
        for node_name, content in sim_data.items():
            u = name_to_uuid.get(node_name)
            if not u: continue
            
            node_data = self.nm.graph.nodes[u]
            # Ensure metadata fields exist
            node_data['outputs_extra'] = [] 
            node_data['suffixes'] = set()
            
            template = self.nm.all_templates.get(node_data['type'], {})
            template_params = template.get('parameters', {})
            
            for key, value in content.items():
                # Skip reserved YAML keys and references
                if key in ['class', 'inputs', 'outputs', 'pos'] or key.endswith('_ref') or key == 'layer_list':
                    continue
                
                # Handle _object suffix logic
                is_obj = key.endswith('_object')
                base_key = key[:-7] if is_obj else key
                
                if base_key in template_params:
                    node_data['values'][base_key] = self._parse_value(value)
                    if is_obj:
                        node_data['suffixes'].add(base_key)
                    
                    # CRITICAL: Push value to the DPG widget if it exists on the node
                    widget_tag = f"{u}_{base_key}"
                    if dpg.does_item_exist(widget_tag):
                        try:
                            dpg.set_value(widget_tag, self._parse_value(value))
                        except Exception:
                            pass 
                else:
                    # Store non-template values directly
                    node_data['values'][key] = self._parse_value(value)

        # --- PASS 3: Create Links (Connections) ---
        connections_to_create = []
        for node_name, content in sim_data.items():
            dst_u = name_to_uuid.get(node_name)
            if not dst_u or "inputs" not in content: continue

            for in_pin, src_raw in content["inputs"].items():
                sources = src_raw if isinstance(src_raw, list) else [src_raw]
                for s in sources:
                    if not isinstance(s, str): continue
                    
                    # Handle DataStore filenames
                    filename = None
                    if in_pin == "input_list" and "-" in s:
                        filename, s = s.split("-", 1)

                    src_node_name, src_attr, delay = self._parse_source_info(s)
                    if src_node_name in name_to_uuid:
                        connections_to_create.append((
                            name_to_uuid[src_node_name], src_attr, dst_u, in_pin, delay, filename
                        ))

            # Reference Links
            for key, val in content.items():
                if key.endswith("_ref") or key == "layer_list":
                    refs = val if isinstance(val, list) else [val]
                    for r_name in refs:
                        if r_name in name_to_uuid:
                            connections_to_create.append((
                                name_to_uuid[r_name], "ref", dst_u, key, 0, None
                            ))
                            # Sync reference values for the property panel
                            if key in ['source_dict_ref', 'layer_list']:
                                node_data = self.nm.graph.nodes[dst_u]
                                node_data['values'].setdefault(key, []).append(r_name)
                            else:
                                self.nm.graph.nodes[dst_u]['values'][key] = r_name
        
        # Execute links
        for src_u, src_a, dst_u, dst_a, delay, filename in connections_to_create:
            self.nm.manual_link(src_u, src_a, dst_u, dst_a, delay=delay)
            if filename and dst_a == "input_list":
                self.nm.graph.nodes[dst_u].setdefault('filename_map', {})[f"{src_u}.{src_a}"] = filename
                    
        # Final UI refreshes
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 3, self.refresh_all_themes)
        dpg.set_frame_callback(current_frame + 3, self.update_ui_values)
        
        # Trigger layout
        # self.nm.graph.auto_layout_nodes(self.nm.uuid_to_dpg) # Assuming helper is available

        auto_layout_nodes(self.nm.graph, self.nm.uuid_to_dpg)
        print(f"[IMPORT] Completed. {len(self.nm.graph.nodes)} nodes loaded.")



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
            if 'pos' in node_data:
                node_dict['pos'] = node_data['pos']

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

    def _parse_value(self, value):
        """Ensure imported strings are converted to literals if applicable."""
        if not isinstance(value, str):
            return value
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value
        
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

