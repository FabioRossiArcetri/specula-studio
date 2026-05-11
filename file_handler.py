import yaml
import os
import dearpygui.dearpygui as dpg
from dpg_utils import auto_layout_nodes
import uuid
import traceback
from collections import OrderedDict


# ── Custom YAML dumper ────────────────────────────────────────────────────────
# Serialises OrderedDict as a plain YAML mapping so that the output file
# contains no !!python/… tags and is readable by any standard YAML parser
# (including SPECULA's yaml.safe_load).

class _CleanDumper(yaml.Dumper):
    pass

_CleanDumper.add_representer(
    OrderedDict,
    lambda dumper, data: dumper.represent_mapping(
        'tag:yaml.org,2002:map', data.items()
    )
)


# ── FileHandler ───────────────────────────────────────────────────────────────

class FileHandler:
    def __init__(self, node_manager):
        self.nm = node_manager
        self.editor = None  # Set by SpeculaEditor after construction

    # ── Override metadata helpers ─────────────────────────────────────────────

    def _add_overrides_metadata(self, yaml_data: dict):
        """Embed override manager state in export data."""
        if hasattr(self, 'editor') and self.editor is not None and \
                hasattr(self.editor, 'override_manager'):
            meta = self.editor.override_manager.to_dict()
            if meta.get('overrides'):
                yaml_data['_overrides_metadata'] = meta
        return yaml_data

    def _load_overrides_metadata(self, yaml_data: dict):
        """
        Extract and restore override manager state from loaded data.
        Pops the key so it is not treated as a simulation node.
        """
        if '_overrides_metadata' in yaml_data and \
                hasattr(self, 'editor') and self.editor is not None:
            meta = yaml_data.pop('_overrides_metadata')
            self.editor.override_manager.from_dict(meta)

    # ── YAML helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def ordered_load(stream, Loader=yaml.SafeLoader,
                     object_pairs_hook=OrderedDict):
        """Load YAML preserving insertion order."""
        class OrderedLoader(Loader):
            pass

        def construct_mapping(loader, node):
            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))

        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping)
        return yaml.load(stream, OrderedLoader)

    # ── Theme / UI helpers ────────────────────────────────────────────────────

    def refresh_all_themes(self):
        for node_uuid in self.nm.graph.nodes:
            self.nm._refresh_node_theme(node_uuid)

    def update_ui_values(self):
        for u_id, node_data in self.nm.graph.nodes.items():
            if u_id in self.nm.uuid_to_dpg and 'values' in node_data:
                if self.nm._last_selected_uuid == u_id:
                    self.nm.update_property_panel(u_id, "property_panel")

    # ── YAML loading helpers ──────────────────────────────────────────────────

    def _load_yaml_file(self, file_path):
        """Load and validate YAML file. Returns OrderedDict or None."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = self.ordered_load(f)
            if not isinstance(data, dict):
                print(f"[FILE_HANDLER] Error: YAML root must be a mapping, "
                      f"got {type(data)}")
                return None
            return data
        except Exception as e:
            print(f"[FILE_HANDLER] Error loading YAML file: {e}")
            return None

    # ── Pass 1: populate graph model ──────────────────────────────────────────

    def _populate_graph_from_yaml(self, yaml_data):
        """
        Pass 1 — create graph nodes from YAML data.
        Returns dict mapping node names to UUIDs.
        """
        name_to_uuid = {}

        for node_name, content in yaml_data.items():
            if not isinstance(content, dict):
                print(f"[FILE_HANDLER] Warning: Skipping '{node_name}' — "
                      f"expected dict, got {type(content)}")
                continue
            if 'class' not in content:
                print(f"[FILE_HANDLER] Warning: Skipping '{node_name}' — "
                      f"missing 'class' key")
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

            if 'gui_pos' in content:
                node_data['gui_pos'] = content['gui_pos']

            template = self.nm.all_templates.get(node_type, {})
            template_params = template.get('parameters', {})

            for key, value in content.items():
                if key in ('class', 'inputs', 'outputs', 'gui_pos'):
                    continue
                if key.endswith('_ref') or key == 'layer_list':
                    continue

                if key.endswith('_object'):
                    base_key = key[:-7]
                    if base_key in template_params:
                        param_meta = template_params[base_key]
                        if param_meta.get('kind', 'value') == 'object' \
                                or key.endswith('_object'):
                            node_data['suffixes'].add(base_key)
                            node_data['values'][base_key] = value
                        else:
                            node_data['values'][base_key] = value
                    else:
                        node_data['suffixes'].add(base_key)
                        node_data['values'][base_key] = value

                elif key in template_params:
                    param_meta = template_params[key]
                    if param_meta.get('kind', 'value') == 'object':
                        node_data['suffixes'].add(key)
                        node_data['values'][key] = value
                    else:
                        node_data['values'][key] = value
                else:
                    node_data['values'][key] = value

        return name_to_uuid

    # ── Pass 2: create UI nodes ───────────────────────────────────────────────

    def _create_ui_nodes(self, yaml_data, name_to_uuid):
        """
        Pass 2 — create DPG node elements.
        Ends with two split_frame() so attribute IDs are ready for Pass 3.
        """
        for node_name, content in yaml_data.items():
            if node_name not in name_to_uuid:
                continue
            u = name_to_uuid[node_name]
            pos = content.get('gui_pos', [100, 100])
            self.nm.create_node(content['class'], pos=pos,
                                existing_uuid=u, name_override=node_name)
        dpg.split_frame()
        dpg.split_frame()

    # ── Pass 3: create connections ────────────────────────────────────────────

    def _create_connections(self, yaml_data, name_to_uuid):
        """
        Pass 3 — wire up all connections from inputs and *_ref / layer_list keys.
        """
        connections_to_create = []

        for node_name, content in yaml_data.items():
            dst_u = name_to_uuid.get(node_name)
            if not dst_u:
                continue

            # Standard data inputs
            if "inputs" in content:
                for in_pin, src_raw in content["inputs"].items():
                    sources = src_raw if isinstance(src_raw, list) else [src_raw]
                    for s in sources:
                        if not isinstance(s, str):
                            continue
                        if in_pin == "input_list" and "-" in s:
                            filename, node_and_attr = s.split("-", 1)
                            src_node_name, src_attr, delay = \
                                self._parse_source_info(node_and_attr)
                            if src_node_name in name_to_uuid:
                                connections_to_create.append((
                                    name_to_uuid[src_node_name], src_attr,
                                    dst_u, in_pin, delay, filename
                                ))
                            continue
                        src_node_name, src_attr, delay = \
                            self._parse_source_info(s)
                        if src_node_name in name_to_uuid:
                            connections_to_create.append((
                                name_to_uuid[src_node_name], src_attr,
                                dst_u, in_pin, delay, None
                            ))

            # Reference links (*_ref, layer_list)
            for key, val in content.items():
                if not (key.endswith("_ref") or key == "layer_list"):
                    continue
                refs = val if isinstance(val, list) else [val]
                for r_name in refs:
                    if not isinstance(r_name, str):
                        continue
                    if r_name in name_to_uuid:
                        connections_to_create.append((
                            name_to_uuid[r_name], "ref",
                            dst_u, key, 0, None
                        ))
                        dst_node_data = self.nm.graph.nodes[dst_u]
                        dst_node_data.setdefault('values', {})
                        if key in ('source_dict_ref', 'layer_list'):
                            dst_node_data['values'].setdefault(key, [])
                            if r_name not in dst_node_data['values'][key]:
                                dst_node_data['values'][key].append(r_name)
                        else:
                            dst_node_data['values'][key] = r_name

        for src_u, src_a, dst_u, dst_a, delay, filename in connections_to_create:
            self.nm.manual_link(src_u, src_a, dst_u, dst_a, delay=delay)
            if filename and dst_a == "input_list":
                self.nm.graph.nodes[dst_u].setdefault('filename_map', {})
                conn_key = f"{src_u}.{src_a}"
                self.nm.graph.nodes[dst_u]['filename_map'][conn_key] = filename

    # ── Finalize ──────────────────────────────────────────────────────────────

    def _finalize_load(self, perform_auto_layout=True, operation_name="LOAD"):
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 3, self.refresh_all_themes)
        dpg.set_frame_callback(current_frame + 3, self.update_ui_values)

        def verify_nodes(attempt=1, max_attempts=5):
            missing = [nid for nid in self.nm.graph.nodes
                       if nid not in self.nm.uuid_to_dpg]
            if missing and attempt < max_attempts:
                print(f"[{operation_name}] Attempt {attempt}: "
                      f"{len(missing)} nodes missing DPG IDs, retrying…")
                dpg.set_frame_callback(
                    dpg.get_frame_count() + 5,
                    lambda: verify_nodes(attempt + 1, max_attempts))
                return
            if missing:
                print(f"[{operation_name}] Failed after {max_attempts} attempts")
                return
            if perform_auto_layout:
                try:
                    auto_layout_nodes(self.nm.graph, self.nm.uuid_to_dpg)
                    print(f"[{operation_name}] Auto-layout done")
                except Exception as e:
                    print(f"[{operation_name}] Layout error: {e}")
                    traceback.print_exc()
            else:
                print(f"[{operation_name}] {len(self.nm.graph.nodes)} nodes "
                      f"loaded with saved positions")

        dpg.set_frame_callback(dpg.get_frame_count() + 10,
                               lambda: verify_nodes(1, 5))

    # ── Internal load from dict ───────────────────────────────────────────────

    def _load_from_yaml_data(self, yaml_data: dict,
                             perform_auto_layout: bool = False,
                             operation_name: str = "LOAD"):
        """
        Core load implementation that works from an already-parsed dict.
        Used by both load_simulation() and load_from_yaml_dict().
        """
        self.nm.clear_all()
        self.nm.graph.nodes.clear()
        self.nm.graph.connections.clear()
        self.nm.graph.connection_properties.clear()

        name_to_uuid = self._populate_graph_from_yaml(yaml_data)
        self._create_ui_nodes(yaml_data, name_to_uuid)
        self._create_connections(yaml_data, name_to_uuid)
        self._finalize_load(perform_auto_layout=perform_auto_layout,
                            operation_name=operation_name)

    # ── Public API ────────────────────────────────────────────────────────────

    def load_simulation(self, file_path, include_defaults=False):
        """Load a simulation from a YAML file (preserves saved positions)."""
        yaml_data = self._load_yaml_file(file_path)
        if yaml_data is None:
            return
        self._load_overrides_metadata(yaml_data)
        self._load_from_yaml_data(yaml_data, perform_auto_layout=False,
                                  operation_name="LOAD")
        print(f"[LOAD] Simulation loaded from {file_path}")

    def load_from_yaml_dict(self, yaml_data: dict):
        """
        Load a simulation from an in-memory dict (no file I/O).
        Used by the override mechanism.
        Override metadata is intentionally NOT extracted here.
        """
        self._load_from_yaml_data(
            dict(yaml_data),
            perform_auto_layout=False,
            operation_name="OVERRIDE-RELOAD"
        )
        print("[FILE_HANDLER] Simulation reloaded from in-memory dict")

    def save_simulation(self, file_path, include_defaults=False):
        """Capture current DPG positions then export to YAML file."""
        for node_uuid, dpg_id in self.nm.uuid_to_dpg.items():
            if node_uuid in self.nm.graph.nodes:
                node_data = self.nm.graph.nodes[node_uuid]
                if dpg.does_item_exist(dpg_id):
                    node_data['gui_pos'] = dpg.get_item_pos(dpg_id)
        self.export_simulation(file_path, include_defaults=include_defaults)
        print(f"[SAVE] Simulation saved to {file_path}")

    def export_simulation(self, file_path, include_defaults=False):
        """Export the graph state to a SPECULA-compatible YAML file."""
        export_data = self.export_to_yaml_dict(
            include_defaults=include_defaults,
            include_override_metadata=True)
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(export_data, f,
                      Dumper=_CleanDumper,       # ← no !!python/… tags
                      sort_keys=False,
                      default_flow_style=False,
                      allow_unicode=True)
        print(f"[FILE_HANDLER] Exported simulation to {file_path}")

    def export_to_yaml_dict(self, include_defaults=False,
                             include_override_metadata=False) -> dict:
        """
        Export the current graph to an in-memory OrderedDict.
        Shared by export_simulation() and the override snapshot mechanism.
        """
        export_data = OrderedDict()

        for u_id, node_data in self.nm.graph.nodes.items():
            node_type = node_data['type']
            node_name = node_data.get('name', node_type)

            template = self.nm.all_templates.get(node_type, {})
            template_params = template.get('parameters', {})

            node_dict = OrderedDict({'class': node_type})

            # Position
            if 'gui_pos' in node_data:
                node_dict['gui_pos'] = node_data['gui_pos']

            # Outputs
            all_outputs = []
            for out in template.get('outputs', []):
                if isinstance(out, str):
                    if "name" in out and "+" in out and "'" in out:
                        continue
                    if ":" in out:
                        continue
                    if out not in all_outputs:
                        all_outputs.append(out)
            for out in node_data.get('outputs_extra', []):
                if isinstance(out, str) and out not in all_outputs:
                    all_outputs.append(out)
            if node_type == "AtmoPropagation":
                for (src_u, src_at, dst_u, dst_at) in self.nm.graph.connections:
                    if dst_u == u_id and dst_at == "source_dict_ref":
                        src_name = self.nm.graph.nodes[src_u].get('name', "unknown")
                        out_name = f"out_{src_name}_ef"
                        if out_name not in all_outputs:
                            all_outputs.append(out_name)
            if all_outputs:
                node_dict['outputs'] = all_outputs

            # Parameters
            current_values = node_data.get('values', {})
            suffixes = node_data.get('suffixes', set())
            handled_params = set()

            for p_name, p_meta in template_params.items():
                if p_meta.get('kind') == 'reference':
                    handled_params.add(p_name)
                    continue
                val = current_values.get(p_name)
                kind = p_meta.get('kind', 'value')
                default_val = p_meta.get('default')

                if not include_defaults:
                    skip = False
                    if val is None and default_val is None:
                        skip = True
                    elif isinstance(val, str) and isinstance(default_val, str):
                        if default_val != "REQUIRED":
                            skip = (val.lower() == default_val.lower())
                    elif val == default_val:
                        skip = True
                    elif val == "" and default_val is None:
                        skip = True
                    elif val is None and default_val == "":
                        skip = True
                    if skip:
                        handled_params.add(p_name)
                        continue

                export_key = p_name
                if p_name in suffixes:
                    export_key = f"{p_name}_object"
                elif kind == 'object':
                    export_key = f"{p_name}_object"
                elif (isinstance(val, str) and
                      self.nm.is_data_class_type(p_meta.get('type'))):
                    export_key = f"{p_name}_object"

                if val is not None:
                    node_dict[export_key] = val
                handled_params.add(p_name)

            for key, val in current_values.items():
                if key not in handled_params:
                    if key.endswith("_ref"):
                        continue
                    elif key in suffixes or f"{key}_object" in current_values:
                        export_key = f"{key}_object" if key in suffixes else key
                        node_dict[export_key] = val
                    else:
                        node_dict[key] = val

            # Connections
            input_connections = {}
            ref_connections = {}

            for (src_u, src_at, dst_u, dst_at) in self.nm.graph.connections:
                if dst_u != u_id:
                    continue
                connection_str = self.nm.get_connection_for_yaml(
                    src_u, src_at, dst_u, dst_at)

                is_ref = False
                if dst_at in template_params:
                    pm = template_params[dst_at]
                    if isinstance(pm, dict) and pm.get("kind") == "reference":
                        is_ref = True
                if not is_ref and (dst_at.endswith("_ref") or
                                   dst_at == "layer_list"):
                    is_ref = True

                if is_ref:
                    ref_connections.setdefault(dst_at, [])
                    if connection_str not in ref_connections[dst_at]:
                        ref_connections[dst_at].append(connection_str)
                else:
                    if dst_at == "input_list":
                        filename = "data"
                        if 'filename_map' in node_data:
                            conn_key = f"{src_u}.{src_at}"
                            filename = node_data['filename_map'].get(
                                conn_key, "data")
                        connection_str = f"{filename}-{connection_str}"
                    input_connections.setdefault(dst_at, [])
                    if connection_str not in input_connections[dst_at]:
                        input_connections[dst_at].append(connection_str)

            if input_connections:
                node_dict['inputs'] = OrderedDict()
                for dst_at, sources in input_connections.items():
                    if dst_at == "input_list":
                        node_dict['inputs'][dst_at] = sources
                    elif dst_at in ('atmo_layer_list', 'common_layer_list') \
                            or len(sources) > 1:
                        node_dict['inputs'][dst_at] = sources
                    else:
                        node_dict['inputs'][dst_at] = sources[0]

            for param_name, sources in ref_connections.items():
                if param_name in ('source_dict_ref', 'layer_list'):
                    node_dict[param_name] = sources
                else:
                    export_key = (param_name if param_name.endswith("_ref")
                                  else f"{param_name}_ref")
                    tp = template_params.get(param_name, {})
                    if (isinstance(tp, dict) and
                            tp.get('kind') == 'reference' and
                            ('list' in str(tp.get('type', '')).lower() or
                             len(sources) > 1)):
                        node_dict[export_key] = sources
                    elif len(sources) > 1:
                        node_dict[export_key] = sources
                    else:
                        node_dict[export_key] = (sources[0] if sources
                                                 else None)

            export_data[node_name] = node_dict

        if include_override_metadata:
            export_data = self._add_overrides_metadata(export_data)

        return export_data

    # ── Connection string parser ──────────────────────────────────────────────

    def _parse_source_info(self, source_val):
        """
        Parse a SPECULA connection string.
          "node.attr"       → (node, attr, 0)
          "node.attr:-1"    → (node, attr, -1)
          "node"            → (node, "ref", 0)
        """
        delay = 0
        if isinstance(source_val, list):
            if not source_val:
                return None, None, 0
            source_val = source_val[0]

        if isinstance(source_val, str):
            if ":-" in source_val:
                base_part, delay_part = source_val.rsplit(":-", 1)
                try:
                    delay = -int(delay_part)
                except ValueError:
                    delay = 0
                    base_part = source_val
            else:
                base_part = source_val

            if "." in base_part:
                parts = base_part.split(".")
                return parts[0], ".".join(parts[1:]), delay
            else:
                return base_part, "ref", delay

        return None, None, 0

    # ── Node template utilities ───────────────────────────────────────────────

    def get_node_template(self, node_type: str) -> dict:
        return self.nm.all_templates.get(node_type, {})

    def get_node_defaults(self, node_type: str) -> dict:
        template = self.get_node_template(node_type)
        return {p: m['default']
                for p, m in template.get('parameters', {}).items()
                if 'default' in m}