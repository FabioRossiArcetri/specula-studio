"""
property_panel.py
=================
Renders and manages the right-hand property panel in the Specula node editor.

Responsibilities
----------------
- ``update_node_panel``       – render node parameters, connections, and monitor buttons.
- ``update_connection_panel`` – render connection properties (delay, type).
- Widget callbacks for editing parameter values and node names.
- Reference disconnection via the "X" buttons in the panel.

Dependencies
------------
- ``GraphManager``    – read node data and connection properties.
- ``NodeRegistry``    – link_registry, output_attr_registry for lookups.
- ``MonitorManager``  – active_monitors, open_monitor, close_monitor.
- Two callbacks supplied by NodeManager:
    ``delink_callback(sender, link_id)``
    ``refresh_node_theme(node_uuid)``
"""

import ast

import dearpygui.dearpygui as dpg

from dpg_utils import apply_link_style


# Colour constants (kept local to avoid polluting the global namespace)
_DEFAULT_PARAM_COLOR = [110, 110, 110]
_MODIFIED_PARAM_COLOR = [240, 240, 240]


class PropertyPanel:
    """Renders the inspector panel for selected nodes and connections."""

    def __init__(
        self,
        graph,
        all_templates: dict,
        registry,
        monitor_manager,
        delink_callback,
        refresh_node_theme,
    ):
        """
        Parameters
        ----------
        graph             : GraphManager – node data store.
        all_templates     : dict – parsed class templates.
        registry          : NodeRegistry – shared DPG↔UUID maps.
        monitor_manager   : MonitorManager – for opening/closing monitor windows.
        delink_callback   : callable(sender, link_id) – NodeManager.delink_callback.
        refresh_node_theme: callable(node_uuid)        – NodeManager._refresh_node_theme.
        """
        self.graph = graph
        self.all_templates = all_templates
        self.registry = registry
        self.monitors = monitor_manager
        self._delink_callback = delink_callback
        self._refresh_node_theme = refresh_node_theme

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def update_node_panel(self, node_uuid: str, panel_tag: str):
        """Render the full property inspector for *node_uuid* into *panel_tag*."""
        dpg.delete_item(panel_tag, children_only=True)

        if node_uuid not in self.graph.nodes:
            return

        node_data = self.graph.nodes[node_uuid]
        node_type = node_data["type"]
        node_name = node_data.get("name", node_type)

        template = self.all_templates.get(node_type, {})
        template_params = template.get("parameters", {})
        current_values = node_data.get("values", {})
        suffixes = node_data.get("suffixes", set())

        # --- 1. Editable name field ------------------------------------------
        dpg.add_text("Node Configuration", color=[100, 200, 255], parent=panel_tag)
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Instance Name:", color=[255, 255, 255])
            dpg.add_input_text(
                default_value=node_name,
                width=150,
                callback=self._update_node_name,
                user_data=node_uuid,
            )
        dpg.add_text(f"Class: {node_type}", color=[150, 150, 150], parent=panel_tag)
        dpg.add_separator(parent=panel_tag)

        rendered_params: set = set()

        # --- 2. Parameters section -------------------------------------------
        if template_params:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Parameters", color=[100, 255, 100], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)

            for param_name, meta in template_params.items():
                print(f"\n[PARAM_DEBUG] Processing parameter: {param_name}")
                print(f"[PARAM_DEBUG] Meta type: {type(meta)}, Meta: {meta}")

                is_ref_param = False
                if isinstance(meta, dict):
                    if meta.get("kind") == "reference":
                        is_ref_param = True
                        print(f"[PARAM_DEBUG] Found reference by kind: {param_name}")
                    elif "type" in meta and self.is_data_class_type(meta["type"]):
                        is_ref_param = True
                        print(
                            f"[PARAM_DEBUG] Found reference by type: "
                            f"{param_name}, type: {meta['type']}"
                        )
                elif isinstance(meta, str):
                    if self.is_data_class_type(meta):
                        is_ref_param = True
                    elif "ref" in meta.lower() or "reference" in meta.lower():
                        is_ref_param = True

                print(f"[PARAM_DEBUG] Is reference parameter: {is_ref_param}")

                if is_ref_param:
                    possible_keys = [
                        f"{param_name}_ref",
                        param_name,
                        f"{param_name}Ref",
                        f"{param_name}ref",
                    ]
                    connected_value = None
                    for key in possible_keys:
                        if key in current_values:
                            connected_value = current_values[key]
                            break

                    display_name = f"{param_name}_ref"
                    if connected_value:
                        with dpg.group(horizontal=True, parent=panel_tag):
                            dpg.add_text(f"{display_name}:", color=[150, 255, 150])
                            dpg.add_text(f"{connected_value}", color=[100, 255, 100])
                            dpg.add_button(
                                label="X",
                                callback=self._disconnect_reference,
                                user_data=(node_uuid, display_name, connected_value),
                                width=20,
                                height=20,
                            )
                    else:
                        is_required = False
                        if isinstance(meta, dict):
                            if (
                                meta.get("default") == "REQUIRED"
                                or meta.get("required", False)
                            ):
                                is_required = True
                        if is_required:
                            with dpg.group(horizontal=True, parent=panel_tag):
                                dpg.add_text(
                                    f"{display_name}:", color=[255, 200, 150]
                                )
                                dpg.add_text(
                                    "REQUIRED (connect via link)",
                                    color=[255, 100, 100],
                                )
                        else:
                            with dpg.group(horizontal=True, parent=panel_tag):
                                dpg.add_text(
                                    f"{display_name}:", color=[200, 200, 200]
                                )
                                dpg.add_text("(optional)", color=[150, 150, 150])

                    rendered_params.add(param_name)
                    continue

                # Regular (non-reference) parameter ----------------------------
                val = current_values.get(param_name)
                if val is None and param_name in suffixes:
                    val = current_values.get(f"{param_name}_object")
                default_val = meta.get("default") if isinstance(meta, dict) else None
                if val is None and default_val == "REQUIRED":
                    val = ""
                type_hint = (
                    meta.get("type", "str") if isinstance(meta, dict) else "str"
                )
                if type_hint is None:
                    type_hint = "str"
                default_val = meta.get("default") if isinstance(meta, dict) else None

                self._render_single_widget(
                    panel_tag, node_uuid, param_name, val, type_hint, default_val
                )
                rendered_params.add(param_name)

        # --- 3. Data object parameters (non-reference) -----------------------
        data_object_params = []
        for param_name in suffixes:
            if param_name not in rendered_params:
                val = current_values.get(param_name) or current_values.get(
                    f"{param_name}_object"
                )
                if val is not None:
                    data_object_params.append((param_name, val))

        if data_object_params:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text(
                "Data Object Parameters", color=[150, 200, 255], parent=panel_tag
            )
            dpg.add_separator(parent=panel_tag)
            for param_name, val in data_object_params:
                is_data_class = False
                if param_name in template_params:
                    meta = template_params[param_name]
                    type_hint = (
                        meta.get("type", "str") if isinstance(meta, dict) else meta
                    )
                    is_data_class = self.is_data_class_type(type_hint)

                if is_data_class:
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(f"{param_name}:", color=[150, 200, 255])
                        input_tag = f"{node_uuid}_{param_name}_object"
                        dpg.add_input_text(
                            default_value=str(val),
                            width=200,
                            hint="File path or object identifier",
                            callback=self._update_data_object_param,
                            user_data=(node_uuid, param_name),
                        )
                        dpg.add_button(
                            label="Browse",
                            width=60,
                            callback=self._browse_data_object_file,
                            user_data=(node_uuid, param_name, input_tag),
                        )
                else:
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(f"{param_name}:", color=[200, 200, 200])
                        dpg.add_text(str(val), color=[200, 200, 150])
                    rendered_params.add(param_name)

        # --- 4. Connections section ------------------------------------------
        incoming, outgoing = self.get_connections_for_node(node_uuid)
        regular_inputs = [
            c
            for c in incoming
            if not (
                c["dst_attr"].endswith("_ref") or c["dst_attr"] == "layer_list"
            )
        ]
        reference_inputs = [
            c
            for c in incoming
            if c["dst_attr"].endswith("_ref") or c["dst_attr"] == "layer_list"
        ]

        if regular_inputs:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Input Connections", color=[200, 150, 255], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)
            dpg.add_text("Data Inputs:", color=[255, 200, 100], parent=panel_tag)
            for conn in regular_inputs:
                src_name = conn["src_name"]
                src_attr = conn["src_attr"]
                dst_attr = conn["dst_attr"]
                if dst_attr == "input_list":
                    filename = self.get_connection_filename(
                        node_uuid, conn["src_node"], src_attr
                    )
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(f"  + {dst_attr}: ", color=[200, 200, 200])
                        dpg.add_text(
                            f"{filename}-{src_name}.{src_attr}",
                            color=[150, 255, 150],
                        )
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text("    Filename: ", color=[200, 200, 200])
                        dpg.add_input_text(
                            default_value=filename,
                            width=100,
                            callback=self._update_connection_filename,
                            user_data=(node_uuid, conn["src_node"], src_attr),
                        )
                else:
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(f"  + {dst_attr}: ", color=[200, 200, 200])
                        dpg.add_text(
                            f"{src_name}.{src_attr}", color=[150, 255, 150]
                        )

        if reference_inputs:
            if not regular_inputs:
                dpg.add_spacer(height=10, parent=panel_tag)
                dpg.add_text(
                    "Connections", color=[200, 150, 255], parent=panel_tag
                )
                dpg.add_separator(parent=panel_tag)
            dpg.add_text(
                "Reference Connections:", color=[255, 200, 100], parent=panel_tag
            )
            for conn in reference_inputs:
                src_name = conn["src_name"]
                src_attr = conn["src_attr"]
                dst_attr = conn["dst_attr"]
                with dpg.group(horizontal=True, parent=panel_tag):
                    dpg.add_text(f"  + {dst_attr}: ", color=[200, 200, 200])
                    if src_attr == "ref":
                        dpg.add_text(f"{src_name}", color=[100, 255, 100])
                    else:
                        dpg.add_text(
                            f"{src_name}.{src_attr}", color=[100, 255, 100]
                        )

        if outgoing:
            if not regular_inputs and not reference_inputs:
                dpg.add_spacer(height=10, parent=panel_tag)
                dpg.add_text(
                    "Connections", color=[200, 150, 255], parent=panel_tag
                )
                dpg.add_separator(parent=panel_tag)
            dpg.add_text("Outputs:", color=[255, 200, 100], parent=panel_tag)
            for conn in outgoing:
                dst_name = conn["dst_name"]
                src_attr = conn["src_attr"]
                dst_attr = conn["dst_attr"]
                with dpg.group(horizontal=True, parent=panel_tag):
                    dpg.add_text(f"  + {src_attr} -> ", color=[200, 200, 200])
                    dpg.add_text(
                        f"{dst_name}.{dst_attr}", color=[150, 255, 150]
                    )

        if not incoming and not outgoing:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Connections", color=[200, 150, 255], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)
            dpg.add_text("No connections", color=[150, 150, 150], parent=panel_tag)

        dpg.add_spacer(height=10, parent=panel_tag)

        # --- 5. Output monitors section --------------------------------------
        all_outputs: list = []
        for out in template.get("outputs", []):
            if isinstance(out, str) and out not in all_outputs:
                all_outputs.append(out)
        for out in node_data.get("outputs_extra", []):
            if isinstance(out, str) and out not in all_outputs:
                all_outputs.append(out)
        for attr_id, (uid, name) in self.registry.output_attr_registry.items():
            if uid == node_uuid and name not in all_outputs:
                all_outputs.append(name)

        if all_outputs:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Output Monitors", color=[255, 150, 100], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)

            for output_name in sorted(all_outputs):
                is_open = any(
                    info.get("node_uuid") == node_uuid
                    and info.get("output_name") == output_name
                    for info in self.monitors.active_monitors.values()
                )

                with dpg.group(horizontal=True, parent=panel_tag):
                    dpg.add_text(f"  + {output_name}: ", color=[200, 200, 200])
                    if not is_open:
                        dpg.add_button(
                            label="Open Monitor",
                            callback=self.monitors.open_monitor,
                            user_data=(node_uuid, output_name),
                            width=120,
                        )
                        dpg.add_text("- Inactive", color=[150, 150, 150])
                    else:
                        monitor_id = next(
                            (
                                mid
                                for mid, info in self.monitors.active_monitors.items()
                                if info.get("node_uuid") == node_uuid
                                and info.get("output_name") == output_name
                            ),
                            None,
                        )
                        if monitor_id:
                            def _close_wrapper(sender, app_data, user_data):
                                self.monitors.close_monitor(
                                    user_data, from_window_close=False
                                )

                            dpg.add_button(
                                label="Close Monitor",
                                callback=_close_wrapper,
                                user_data=monitor_id,
                                width=120,
                            )
                            dpg.add_text("+ Active", color=[0, 255, 0])

            dpg.add_spacer(height=5, parent=panel_tag)

        dpg.add_spacer(height=10, parent=panel_tag)

    def update_connection_panel(self, link_id, panel_tag: str):
        """Render connection properties (delay, type) for *link_id*."""
        dpg.delete_item(panel_tag, children_only=True)

        if link_id not in self.registry.link_registry:
            print(f"[PANEL] Link {link_id} not in registry")
            return

        src_uuid, src_attr, dst_uuid, dst_attr = self.registry.link_registry[link_id]
        src_node = self.graph.nodes.get(src_uuid, {})
        dst_node = self.graph.nodes.get(dst_uuid, {})
        src_name = src_node.get("name", "Unknown")
        dst_name = dst_node.get("name", "Unknown")

        conn_props = self.graph.get_connection_properties(
            src_uuid, src_attr, dst_uuid, dst_attr
        )
        current_delay = conn_props.get("delay", 0)

        dpg.add_text(
            "Connection Properties", color=[100, 200, 255], parent=panel_tag
        )
        dpg.add_separator(parent=panel_tag)

        dpg.add_text("Source (Output):", color=[255, 255, 255], parent=panel_tag)
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Node:", color=[200, 200, 200])
            dpg.add_text(f"{src_name}", color=[150, 255, 150])
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Attribute:", color=[200, 200, 200])
            dpg.add_text(src_attr, color=[150, 255, 150])

        dpg.add_spacer(height=10, parent=panel_tag)

        dpg.add_text("Destination (Input):", color=[255, 255, 255], parent=panel_tag)
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Node:", color=[200, 200, 200])
            dpg.add_text(f"{dst_name}", color=[150, 255, 150])
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Attribute:", color=[200, 200, 200])
            dpg.add_text(dst_attr, color=[150, 255, 150])

        dpg.add_separator(parent=panel_tag)
        dpg.add_spacer(height=10, parent=panel_tag)
        dpg.add_text("Delay/Index:", color=[255, 200, 100], parent=panel_tag)

        def update_delay_callback(sender, app_data, user_data):
            conn_data = user_data
            new_delay = int(app_data)
            if new_delay not in [0, -1]:
                dpg.set_value(sender, 0)
                new_delay = 0
            self.graph.update_connection_properties(
                conn_data[0], conn_data[1], conn_data[2], conn_data[3],
                {"delay": new_delay},
            )
            self._update_connection_display(
                conn_data[0], conn_data[1], conn_data[2], conn_data[3], new_delay
            )

        dpg.add_input_int(
            default_value=current_delay,
            min_value=-1,
            max_value=0,
            min_clamped=True,
            max_clamped=True,
            width=100,
            callback=update_delay_callback,
            user_data=(src_uuid, src_attr, dst_uuid, dst_attr),
            parent=panel_tag,
        )
        dpg.add_text(
            "0 = normal connection, -1 = feedback (previous timestep)",
            color=[150, 150, 150],
            parent=panel_tag,
        )
        dpg.add_spacer(height=10, parent=panel_tag)

        conn_type = "Feedback" if current_delay == -1 else "Normal"
        dpg.add_text(f"Type: {conn_type}", color=[200, 200, 255], parent=panel_tag)
        if conn_type == "Feedback":
            dpg.add_text(
                "This connection uses data from previous timestep",
                color=[255, 150, 100],
                parent=panel_tag,
            )

        dpg.add_spacer(height=10, parent=panel_tag)
        dpg.add_separator(parent=panel_tag)
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Link ID:", color=[150, 150, 150])
            dpg.add_text(link_id, color=[200, 200, 200])

    # ------------------------------------------------------------------
    # Widget callbacks (all private)
    # ------------------------------------------------------------------

    def _update_node_name(self, sender, app_data, user_data):
        """Update instance name in graph and refresh DPG node label."""
        node_uuid = user_data
        new_name = app_data
        if node_uuid in self.graph.nodes:
            self.graph.nodes[node_uuid]["name"] = new_name
            dpg_id = self.registry.uuid_to_dpg.get(node_uuid)
            if dpg_id:
                dpg.set_item_label(
                    dpg_id,
                    f"{new_name} ({self.graph.nodes[node_uuid]['type']})",
                )
            print(f"Renamed node {node_uuid} to '{new_name}'")

    def _update_param(self, sender, app_data, user_data):
        """Save a UI widget change back to the graph, enforcing types."""
        node_uuid, param_name, target_type = user_data
        if node_uuid not in self.graph.nodes:
            return
        values_dict = self.graph.nodes[node_uuid]["values"]
        final_val = app_data
        try:
            if target_type == "list" or (
                isinstance(app_data, str) and app_data.startswith("[")
            ):
                try:
                    final_val = ast.literal_eval(app_data)
                except (ValueError, SyntaxError):
                    print(f"Warning: Invalid list syntax for {param_name}")
                    return
            elif target_type in ["int", "integer"]:
                final_val = int(app_data)
            elif target_type in ["float", "double", "number"]:
                final_val = float(app_data)
            elif target_type in ["bool", "boolean"]:
                final_val = bool(app_data)
            values_dict[param_name] = final_val
            self._refresh_node_theme(node_uuid)
            print(
                f"Updated {node_uuid} [{param_name}] -> "
                f"{final_val} ({type(final_val).__name__})"
            )
        except Exception as e:
            print(f"Error updating parameter {param_name}: {e}")

    def _update_data_object_param(self, sender, app_data, user_data):
        """Update a data-object parameter value."""
        node_uuid, param_name = user_data
        if node_uuid in self.graph.nodes:
            node_data = self.graph.nodes[node_uuid]
            node_data.setdefault("values", {})
            node_data["values"][param_name] = app_data
            node_data["values"][f"{param_name}_object"] = app_data
            print(f"Updated data object parameter {param_name} = {app_data}")
            self._refresh_node_theme(node_uuid)

    def _browse_data_object_file(self, sender, app_data, user_data):
        """Placeholder: open file browser for a data-object parameter."""
        node_uuid, param_name, input_tag = user_data
        print(f"Browse button clicked for {param_name} on node {node_uuid}")

    def _disconnect_reference(self, sender, app_data, user_data):
        """Remove the link that provides a reference parameter."""
        node_uuid, param_name, connected_node_name = user_data
        link_to_remove = None
        for link_id, (src_uuid, src_attr, dst_uuid, dst_attr) in (
            self.registry.link_registry.items()
        ):
            if dst_uuid == node_uuid and dst_attr == param_name:
                src_node = self.graph.nodes.get(src_uuid, {})
                if src_node.get("name", "") == connected_node_name:
                    link_to_remove = link_id
                    break
        if link_to_remove:
            self._delink_callback(None, link_to_remove)
            self._refresh_node_theme(node_uuid)

    def _update_connection_display(
        self, src_uuid, src_attr, dst_uuid, dst_attr, delay: int
    ):
        """Update the visual style of a connection when its delay changes."""
        link_id = None
        for lid, conn_data in self.registry.link_registry.items():
            if (
                conn_data[0] == src_uuid
                and conn_data[1] == src_attr
                and conn_data[2] == dst_uuid
                and conn_data[3] == dst_attr
            ):
                link_id = lid
                break

        if not link_id or not dpg.does_item_exist(link_id):
            return

        if delay == -1:
            apply_link_style(link_id, color=[255, 0, 0, 255])
            self._update_feedback_attribute(src_uuid, src_attr, delay)
        elif delay == 0:
            if dst_attr.endswith("_ref") or "params" in dst_attr.lower():
                apply_link_style(link_id, color=[200, 200, 200, 60])
            else:
                dpg.configure_item(link_id, color=[255, 255, 255, 255])

    def _update_feedback_attribute(self, node_uuid, attr_name, delay: int):
        """Update the text on a feedback output pin."""
        attr_id = None
        for aid, (uid, name) in self.registry.output_attr_registry.items():
            if uid == node_uuid and name == attr_name:
                attr_id = aid
                break
        if not attr_id or not dpg.does_item_exist(attr_id):
            return
        children = dpg.get_item_children(attr_id, slot=1)
        for child in children:
            if dpg.get_item_type(child) == "mvAppItemType::mvText":
                current_text = dpg.get_value(child)
                if delay == -1 and ":-1" not in current_text:
                    dpg.set_value(child, f"{attr_name}:-1")
                    dpg.configure_item(child, color=[255, 100, 100])
                elif delay == 0 and ":-1" in current_text:
                    dpg.set_value(child, attr_name.replace(":-1", ""))
                    dpg.configure_item(child, color=[255, 255, 255])
                break

    def _update_connection_filename(self, sender, app_data, user_data):
        """Callback: save a new filename for a DataStore connection."""
        node_uuid, src_uuid, src_attr = user_data
        self.update_connection_filename(node_uuid, src_uuid, src_attr, app_data)
        print(
            f"Updated filename for {src_uuid}.{src_attr} -> {node_uuid}: {app_data}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_single_widget(
        self, parent, node_uuid, param_name, val, type_hint, default_val=None
    ):
        """Render one parameter row in the property panel."""

        def _values_equal(a, b):
            try:
                return a == b
            except Exception:
                return False

        node_data = self.graph.nodes.get(node_uuid, {})
        template = self.all_templates.get(node_data.get("type", ""), {})
        param_meta = template.get("parameters", {}).get(param_name, {})
        param_kind = param_meta.get("kind", "value") if isinstance(param_meta, dict) else "value"

        if param_kind == "reference" and val is not None:
            with dpg.group(horizontal=True, parent=parent):
                dpg.add_text(f"{param_name}:", color=[150, 255, 150])
                dpg.add_text(f": {val}", color=[100, 255, 100])
            return

        is_data_object = (
            param_kind == "object"
            or param_name in node_data.get("suffixes", set())
            or self.is_data_class_type(type_hint)
        )
        is_required = default_val == "REQUIRED"
        has_value = val is not None and val not in ("", "REQUIRED")

        if is_required and not has_value:
            label_color = [255, 100, 100]
        elif is_data_object:
            label_color = [150, 200, 255]
        elif param_kind == "reference":
            label_color = [255, 200, 150]
        elif default_val is not None and _values_equal(val, default_val):
            label_color = _DEFAULT_PARAM_COLOR
        else:
            label_color = _MODIFIED_PARAM_COLOR

        user_data = (node_uuid, param_name, type_hint)
        display_val = val

        if is_required and (val is None or val == "REQUIRED"):
            display_val = "" if type_hint in ("str", "string") else 0

        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text(f"{param_name}:", color=label_color)

            if type_hint in ("bool", "boolean"):
                if display_val is None:
                    display_val = False
                dpg.add_checkbox(
                    default_value=bool(display_val),
                    callback=self._update_param,
                    user_data=user_data,
                )
            elif type_hint in ("int", "integer"):
                if display_val is None:
                    display_val = 0
                dpg.add_input_int(
                    default_value=int(display_val),
                    width=150,
                    step=1,
                    callback=self._update_param,
                    user_data=user_data,
                )
            elif type_hint in ("float", "double", "number"):
                if display_val is None or display_val in ("inf", "REQUIRED"):
                    display_val = 0.0
                dpg.add_input_float(
                    default_value=float(display_val),
                    width=150,
                    step=0.1,
                    callback=self._update_param,
                    user_data=user_data,
                )
            elif isinstance(display_val, list) or type_hint == "list":
                if display_val is None:
                    display_val = []
                dpg.add_input_text(
                    default_value=str(display_val),
                    width=150,
                    callback=self._update_param,
                    user_data=user_data,
                )
            else:
                if display_val is None:
                    display_val = ""
                dpg.add_input_text(
                    default_value=str(display_val),
                    width=150,
                    callback=self._update_param,
                    user_data=user_data,
                )

    def is_data_class_type(self, type_name: str) -> bool:
        """Return True if *type_name* looks like a Specula data-object type."""
        if not type_name or type_name == "Any":
            return False
        if hasattr(self, "_data_obj_templates") and type_name in self._data_obj_templates:
            return True
        data_keywords = [
            "Matrix", "Vector", "Atmosphere", "Telescope", "Detector", "Field"
        ]
        return any(k in type_name for k in data_keywords)

    def get_connections_for_node(self, node_uuid: str):
        """Return (incoming, outgoing) connection lists for *node_uuid*."""
        incoming = []
        outgoing = []
        for src_u, src_at, dst_u, dst_at in self.graph.connections:
            if dst_u == node_uuid:
                incoming.append(
                    {
                        "src_node": src_u,
                        "src_attr": src_at,
                        "dst_attr": dst_at,
                        "src_name": self.graph.nodes[src_u].get("name", "unknown"),
                        "dst_name": self.graph.nodes[node_uuid].get("name", "unknown"),
                        "type": "input",
                    }
                )
            if src_u == node_uuid:
                outgoing.append(
                    {
                        "dst_node": dst_u,
                        "src_attr": src_at,
                        "dst_attr": dst_at,
                        "src_name": self.graph.nodes[node_uuid].get("name", "unknown"),
                        "dst_name": self.graph.nodes[dst_u].get("name", "unknown"),
                        "type": "output",
                    }
                )
        return incoming, outgoing

    def update_connection_filename(
        self, node_uuid: str, src_uuid: str, src_attr: str, new_filename: str
    ):
        """Persist a filename for a DataStore connection."""
        self.graph.nodes[node_uuid].setdefault("filename_map", {})
        self.graph.nodes[node_uuid]["filename_map"][
            f"{src_uuid}.{src_attr}"
        ] = new_filename

    def get_connection_filename(
        self, node_uuid: str, src_uuid: str, src_attr: str
    ) -> str:
        """Retrieve the stored filename for a DataStore connection."""
        filename_map = self.graph.nodes.get(node_uuid, {}).get("filename_map", {})
        return filename_map.get(f"{src_uuid}.{src_attr}", "data")
