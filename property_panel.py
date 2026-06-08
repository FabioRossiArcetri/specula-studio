"""
property_panel.py
=================
Renders and manages the right-hand property panel in the Specula node editor.

Parameter-mode handling
-----------------------
Every parameter whose template kind is "reference" *or* "object" (legacy)
exposes a _ref ↔ _object toggle.

  _ref (link)    – default: the square input pin is visible; wire it to any
                   data-object node's "ref" output.

  _object (file) – the ref pin is hidden; the user types or browses for the
                   CalibManager FITS tag.  Clearing the text reverts to _ref.

Array parameters (kind: "data") show a dual widget: inline values OR a
separate _data filename field with a browse button.

Parameters named "tag" or ending with "_tag" get a "…" browse button.

Default value display
---------------------
For every ordinary (value / data / tag) parameter the widget starts **empty**
when the value is not set in the loaded YAML.  The template default is always
shown as grey text immediately to the right of the input widget so the user
knows what the code will use.  "null" defaults are shown as "(default: null)".
Required parameters show "(REQUIRED)" in red.

Folder resolution for file dialogs
------------------------------------
The browse dialog opens in  <root_dir>/<folder_name>  where root_dir comes
from the linked SimulParams node and folder_name equals the parameter name
with one hard-coded exception:  recmat → rec  (CalibManager uses 'rec' as the
on-disk sub-directory for reconstruction matrices).  All other parameter names
map to themselves, after stripping any trailing _tag / _data / _object suffix.

Tooltips
--------
When the mouse hovers over a parameter name, input name, output name, or the
class name line, a tooltip is shown with type, default and description text
extracted live from the installed SPECULA package via help_provider.py.
Tooltips degrade silently to nothing if SPECULA or help_provider is absent.
"""

import ast
import os

import numpy as np
import dearpygui.dearpygui as dpg

from dpg_utils import apply_link_style
from theme_manager import ThemeManager

# ── Documentation helpers (gracefully absent if SPECULA not installed) ─────────
try:
    from help_provider import (
        get_param_tooltip,
        get_input_tooltip,
        get_output_tooltip,
        get_class_tooltip,
        get_param_unit,
    )
    _HELP_AVAILABLE = True
except ImportError:
    _HELP_AVAILABLE = False
    def get_param_tooltip(*_):  return ""
    def get_input_tooltip(*_):  return ""
    def get_output_tooltip(*_): return ""
    def get_class_tooltip(*_):  return ""
    def get_param_unit(*_):     return ""

# Type hints that indicate a list/array value
_ARRAY_TYPE_HINTS = frozenset(
    ["list", "Any", "ndarray", "array", "tuple", "sequence"]
)

# The ONE known folder-name exception: parameter 'recmat' lives under 'rec/'
# in the CalibManager directory tree.  Add more pairs here if more exceptions
# are discovered in future SPECULA releases.
_PARAM_FOLDER_OVERRIDES: dict = {
    "recmat": "rec",
}


def _folder_for_param(param_name: str) -> str:
    """
    Return the CalibManager sub-directory name for *param_name*.

    The rule is: folder = param_name, with the single exception recmat → rec.
    Trailing suffixes (_tag, _data, _object) that appear in widget tag strings
    are stripped first so the folder name is always the bare parameter name.
    """
    bare = param_name
    for sfx in ("_tag", "_data", "_object"):
        if bare.endswith(sfx):
            bare = bare[: -len(sfx)]
            break
    return _PARAM_FOLDER_OVERRIDES.get(bare, bare)


def _add_tooltip(parent_id, text: str):
    """Attach a tooltip to *parent_id* if *text* is non-empty."""
    if text:
        tm = ThemeManager()
        with dpg.tooltip(parent=parent_id):
            dpg.add_text(text, color=tm.get_color("tooltip_text"), wrap=400)


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
        self.graph              = graph
        self.all_templates      = all_templates
        self.registry           = registry
        self.monitors           = monitor_manager
        self._delink_callback   = delink_callback
        self._refresh_node_theme = refresh_node_theme
        self.theme_manager = ThemeManager()

    # =========================================================================
    # Public entry points
    # =========================================================================

    def update_node_panel(self, node_uuid: str, panel_tag: str):
        """Render the full property inspector for *node_uuid* into *panel_tag*."""
        dpg.delete_item(panel_tag, children_only=True)

        if node_uuid not in self.graph.nodes:
            return

        node_data       = self.graph.nodes[node_uuid]
        node_type       = node_data["type"]
        node_name       = node_data.get("name", node_type)
        template        = self.all_templates.get(node_type, {})
        template_params = template.get("parameters", {})
        current_values  = node_data.get("values", {})
        suffixes        = node_data.get("suffixes", set())
        tm = self.theme_manager

        # ── 1. Editable name ──────────────────────────────────────────────────
        dpg.add_text(
            "Node Configuration",
            color=tm.get_color("panel_section_header"),
            parent=panel_tag,
        )
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text(
                "Instance Name:",
                color=tm.get_color("panel_label_normal"),
            )
            dpg.add_input_text(
                default_value=node_name,
                width=150,
                callback=self._update_node_name,
                user_data=node_uuid,
            )

        # Class name with tooltip showing summary
        cls_lbl = dpg.add_text(
            f"Class: {node_type}",
            color=tm.get_color("panel_text_muted"),
            parent=panel_tag,
        )
        _add_tooltip(cls_lbl, get_class_tooltip(node_type))

        dpg.add_separator(parent=panel_tag)

        rendered_params: set = set()

        # ── 2. Parameters ─────────────────────────────────────────────────────
        if template_params:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text(
                "Parameters",
                color=tm.get_color("panel_section_header"),
                parent=panel_tag,
            )
            dpg.add_separator(parent=panel_tag)

            for param_name, meta in template_params.items():
                # Determine if this is a ref/object parameter.
                # Both kind:"reference" (new templates) and kind:"object"
                # (legacy templates not yet regenerated) are treated the same.
                is_ref_param = False
                if isinstance(meta, dict):
                    kind = meta.get("kind", "value")
                    if kind in ("reference", "object"):
                        is_ref_param = True
                    elif "type" in meta and self.is_data_class_type(meta["type"]):
                        is_ref_param = True
                elif isinstance(meta, str):
                    if self.is_data_class_type(meta):
                        is_ref_param = True
                    elif "ref" in meta.lower() or "reference" in meta.lower():
                        is_ref_param = True

                if is_ref_param:
                    self._render_ref_or_object_param(
                        panel_tag, node_uuid, node_data, param_name,
                        meta, current_values,
                    )
                    rendered_params.add(param_name)
                    continue

                # Regular (non-reference) parameter
                val = current_values.get(param_name)
                if val is None and param_name in suffixes:
                    val = current_values.get(f"{param_name}_object")
                default_val = meta.get("default") if isinstance(meta, dict) else None
                type_hint = (
                    meta.get("type", "str") if isinstance(meta, dict) else "str"
                ) or "str"

                self._render_single_widget(
                    panel_tag, node_uuid, param_name, val, type_hint, default_val
                )
                rendered_params.add(param_name)

        # ── 3. Suffix / data-object params not yet rendered ───────────────────
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
                "Data Object Parameters",
                color=tm.get_color("panel_label_data_obj"),
                parent=panel_tag,
            )
            dpg.add_separator(parent=panel_tag)
            for param_name, val in data_object_params:
                is_data_class = False
                if param_name in template_params:
                    meta      = template_params[param_name]
                    type_hint = (
                        meta.get("type", "str") if isinstance(meta, dict) else meta
                    )
                    is_data_class = self.is_data_class_type(type_hint)

                if is_data_class:
                    with dpg.group(horizontal=True, parent=panel_tag):
                        lbl = dpg.add_text(
                            f"{param_name}:",
                            color=tm.get_color("panel_label_data_obj"),
                        )
                        _add_tooltip(lbl, get_param_tooltip(node_type, param_name))
                        input_tag = f"{node_uuid}_{param_name}_object"
                        dpg.add_input_text(
                            tag=input_tag,
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
                        lbl = dpg.add_text(
                            f"{param_name}:",
                            color=tm.get_color("panel_text_secondary"),
                        )
                        _add_tooltip(lbl, get_param_tooltip(node_type, param_name))
                        dpg.add_text(
                            str(val),
                            color=tm.get_color("panel_label_normal"),
                        )
                    rendered_params.add(param_name)

        # ── 4. Connections ────────────────────────────────────────────────────
        incoming, outgoing = self.get_connections_for_node(node_uuid)
        regular_inputs = [
            c for c in incoming
            if not (c["dst_attr"].endswith("_ref") or c["dst_attr"] == "layer_list")
        ]
        reference_inputs = [
            c for c in incoming
            if c["dst_attr"].endswith("_ref") or c["dst_attr"] == "layer_list"
        ]

        if regular_inputs:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text(
                "Input Connections",
                color=tm.get_color("panel_section_header"),
                parent=panel_tag,
            )
            dpg.add_separator(parent=panel_tag)
            dpg.add_text(
                "Data Inputs:",
                color=tm.get_color("panel_label_ref"),
                parent=panel_tag,
            )
            for conn in regular_inputs:
                src_name = conn["src_name"]
                src_attr = conn["src_attr"]
                dst_attr = conn["dst_attr"]
                if dst_attr == "input_list":
                    filename = self.get_connection_filename(
                        node_uuid, conn["src_node"], src_attr
                    )
                    with dpg.group(horizontal=True, parent=panel_tag):
                        lbl = dpg.add_text(
                            f"  + {dst_attr}: ",
                            color=tm.get_color("panel_text_secondary"),
                        )
                        _add_tooltip(lbl, get_input_tooltip(node_type, dst_attr))
                        dpg.add_text(
                            f"{filename}-{src_name}.{src_attr}",
                            color=tm.get_color("panel_conn_src_name"),
                        )
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(
                            "    Filename: ",
                            color=tm.get_color("panel_text_secondary"),
                        )
                        dpg.add_input_text(
                            default_value=filename,
                            width=100,
                            callback=self._update_connection_filename,
                            user_data=(node_uuid, conn["src_node"], src_attr),
                        )
                else:
                    with dpg.group(horizontal=True, parent=panel_tag):
                        lbl = dpg.add_text(
                            f"  + {dst_attr}: ",
                            color=tm.get_color("panel_text_secondary"),
                        )
                        _add_tooltip(lbl, get_input_tooltip(node_type, dst_attr))
                        dpg.add_text(
                            f"{src_name}.{src_attr}",
                            color=tm.get_color("panel_conn_src_name"),
                        )

        if reference_inputs:
            if not regular_inputs:
                dpg.add_spacer(height=10, parent=panel_tag)
                dpg.add_text(
                    "Connections",
                    color=tm.get_color("panel_section_header"),
                    parent=panel_tag,
                )
                dpg.add_separator(parent=panel_tag)
            dpg.add_text(
                "Reference Connections:",
                color=tm.get_color("panel_label_ref"),
                parent=panel_tag,
            )
            for conn in reference_inputs:
                src_name = conn["src_name"]
                src_attr = conn["src_attr"]
                dst_attr = conn["dst_attr"]
                with dpg.group(horizontal=True, parent=panel_tag):
                    lbl = dpg.add_text(
                        f"  + {dst_attr}: ",
                        color=tm.get_color("panel_text_secondary"),
                    )
                    _add_tooltip(lbl, get_input_tooltip(node_type, dst_attr))
                    if src_attr == "ref":
                        dpg.add_text(
                            f"{src_name}",
                            color=tm.get_color("panel_conn_src_name"),
                        )
                    else:
                        dpg.add_text(
                            f"{src_name}.{src_attr}",
                            color=tm.get_color("panel_conn_src_name"),
                        )

        if outgoing:
            if not regular_inputs and not reference_inputs:
                dpg.add_spacer(height=10, parent=panel_tag)
                dpg.add_text(
                    "Connections",
                    color=tm.get_color("panel_section_header"),
                    parent=panel_tag,
                )
                dpg.add_separator(parent=panel_tag)
            dpg.add_text(
                "Outputs:",
                color=tm.get_color("panel_label_ref"),
                parent=panel_tag,
            )
            for conn in outgoing:
                dst_name = conn["dst_name"]
                src_attr = conn["src_attr"]
                dst_attr = conn["dst_attr"]
                with dpg.group(horizontal=True, parent=panel_tag):
                    lbl = dpg.add_text(
                        f"  + {src_attr} -> ",
                        color=tm.get_color("panel_text_secondary"),
                    )
                    _add_tooltip(lbl, get_output_tooltip(node_type, src_attr))
                    dpg.add_text(
                        f"{dst_name}.{dst_attr}",
                        color=tm.get_color("panel_conn_dst_name"),
                    )

        if not incoming and not outgoing:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text(
                "Connections",
                color=tm.get_color("panel_section_header"),
                parent=panel_tag,
            )
            dpg.add_separator(parent=panel_tag)
            dpg.add_text(
                "No connections",
                color=tm.get_color("panel_text_muted"),
                parent=panel_tag,
            )

        dpg.add_spacer(height=10, parent=panel_tag)

        # ── 5. Output monitors ────────────────────────────────────────────────
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
            dpg.add_text(
                "Output Monitors",
                color=tm.get_color("panel_section_header"),
                parent=panel_tag,
            )
            dpg.add_separator(parent=panel_tag)

            for output_name in sorted(all_outputs):
                is_open = self.monitors.is_monitor_open(node_uuid, output_name)
                with dpg.group(horizontal=True, parent=panel_tag):
                    lbl = dpg.add_text(
                        f"  + {output_name}: ",
                        color=tm.get_color("panel_text_secondary"),
                    )
                    _add_tooltip(lbl, get_output_tooltip(node_type, output_name))
                    if not is_open:
                        dpg.add_button(
                            label="Open Monitor",
                            callback=self.monitors.open_monitor,
                            user_data=(node_uuid, output_name),
                            width=120,
                        )
                        dpg.add_text(
                            "- Inactive",
                            color=tm.get_color("panel_text_muted"),
                        )
                    else:
                        monitor_id = self.monitors.find_monitor_id(node_uuid, output_name)
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
                            dpg.add_text(
                                "+ Active",
                                color=tm.get_color("panel_monitor_active"),
                            )

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

        conn_props    = self.graph.get_connection_properties(
            src_uuid, src_attr, dst_uuid, dst_attr
        )
        current_delay = conn_props.get("delay", 0)
        tm = self.theme_manager

        dpg.add_text(
            "Connection Properties",
            color=tm.get_color("panel_section_header"),
            parent=panel_tag,
        )
        dpg.add_separator(parent=panel_tag)
        dpg.add_text(
            "Source (Output):",
            color=tm.get_color("panel_label_normal"),
            parent=panel_tag,
        )
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Node:", color=tm.get_color("panel_text_secondary"))
            dpg.add_text(
                f"{src_name}",
                color=tm.get_color("panel_conn_src_name"),
            )
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Attribute:", color=tm.get_color("panel_text_secondary"))
            dpg.add_text(
                src_attr,
                color=tm.get_color("panel_conn_src_name"),
            )
        dpg.add_spacer(height=10, parent=panel_tag)
        dpg.add_text(
            "Destination (Input):",
            color=tm.get_color("panel_label_normal"),
            parent=panel_tag,
        )
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Node:", color=tm.get_color("panel_text_secondary"))
            dpg.add_text(
                f"{dst_name}",
                color=tm.get_color("panel_conn_dst_name"),
            )
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Attribute:", color=tm.get_color("panel_text_secondary"))
            dpg.add_text(
                dst_attr,
                color=tm.get_color("panel_conn_dst_name"),
            )
        dpg.add_separator(parent=panel_tag)
        dpg.add_spacer(height=10, parent=panel_tag)
        dpg.add_text(
            "Delay/Index:",
            color=tm.get_color("panel_label_ref"),
            parent=panel_tag,
        )

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
            min_value=-1, max_value=0,
            min_clamped=True, max_clamped=True,
            width=100,
            callback=update_delay_callback,
            user_data=(src_uuid, src_attr, dst_uuid, dst_attr),
            parent=panel_tag,
        )
        dpg.add_text(
            "0 = normal connection, -1 = feedback (previous timestep)",
            color=tm.get_color("panel_text_hint"),
            parent=panel_tag,
        )
        dpg.add_spacer(height=10, parent=panel_tag)
        conn_type = "Feedback" if current_delay == -1 else "Normal"
        dpg.add_text(
            f"Type: {conn_type}",
            color=tm.get_color("panel_label_normal"),
            parent=panel_tag,
        )
        if conn_type == "Feedback":
            dpg.add_text(
                "This connection uses data from previous timestep",
                color=tm.get_color("panel_text_hint"),
                parent=panel_tag,
            )
        dpg.add_spacer(height=10, parent=panel_tag)
        dpg.add_separator(parent=panel_tag)
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Link ID:", color=tm.get_color("panel_text_muted"))
            dpg.add_text(link_id, color=tm.get_color("panel_text_secondary"))

    # =========================================================================
    # Ref-or-object parameter rendering
    # =========================================================================

    def _render_ref_or_object_param(
        self,
        panel_tag: str,
        node_uuid: str,
        node_data: dict,
        param_name: str,
        meta,
        current_values: dict,
    ):
        """
        Render a single ref-or-object parameter row (combo + sub-row).

        Handles both kind:"reference" (new templates) and kind:"object"
        (legacy templates).  The logic and layout are identical for both.
        """
        node_type   = node_data.get("type", "")
        param_modes = node_data.setdefault("param_modes", {})
        tm = self.theme_manager

        # Infer _object mode from YAML data already loaded
        obj_val = current_values.get(f"{param_name}_object", "")
        if obj_val and param_name not in param_modes:
            param_modes[param_name] = "object"

        current_mode     = param_modes.get(param_name, "ref")
        display_ref_name = f"{param_name}_ref"

        # ── mode selector combo ───────────────────────────────────────────────
        dpg.add_spacer(height=3, parent=panel_tag)
        mode_items         = ["_ref (link)", "_object (file)"]
        current_mode_label = (
            "_object (file)" if current_mode == "object" else "_ref (link)"
        )
        with dpg.group(horizontal=True, parent=panel_tag):
            lbl = dpg.add_text(
                f"{param_name}:",
                color=tm.get_color("panel_label_data_obj"),
            )
            _add_tooltip(lbl, get_param_tooltip(node_type, param_name))
            dpg.add_combo(
                items=mode_items,
                default_value=current_mode_label,
                width=150,
                callback=self._on_ref_obj_mode_changed,
                user_data=(node_uuid, param_name),
            )

        # ── ref mode ─────────────────────────────────────────────────────────
        if current_mode == "ref":
            possible_keys   = [f"{param_name}_ref", param_name,
                               f"{param_name}Ref", f"{param_name}ref"]
            connected_value = None
            for key in possible_keys:
                if key in current_values:
                    connected_value = current_values[key]
                    break

            if connected_value:
                with dpg.group(horizontal=True, parent=panel_tag):
                    dpg.add_text("  →", color=tm.get_color("panel_text_muted"))
                    dpg.add_text(
                        f"{connected_value}",
                        color=tm.get_color("panel_conn_src_name"),
                    )
                    dpg.add_button(
                        label="X",
                        callback=self._disconnect_reference,
                        user_data=(node_uuid, display_ref_name, connected_value),
                        width=20, height=20,
                    )
            else:
                is_required = (
                    isinstance(meta, dict)
                    and (
                        meta.get("default") == "REQUIRED"
                        or meta.get("required", False)
                    )
                )
                if is_required:
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(
                            f"  {display_ref_name}:",
                            color=tm.get_color("panel_label_required"),
                        )
                        dpg.add_text(
                            "REQUIRED – wire a link or switch to _object",
                            color=tm.get_color("panel_label_required"),
                        )
                else:
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(
                            f"  {display_ref_name}:",
                            color=tm.get_color("panel_text_secondary"),
                        )
                        dpg.add_text(
                            "(optional link)",
                            color=tm.get_color("panel_text_muted"),
                        )

        # ── object (file) mode ────────────────────────────────────────────────
        else:
            obj_input_tag = f"{node_uuid}_{param_name}_obj_input"
            with dpg.group(horizontal=True, parent=panel_tag):
                dpg.add_text(
                    "  file:",
                    color=tm.get_color("panel_label_data_obj"),
                )
                dpg.add_input_text(
                    tag=obj_input_tag,
                    default_value=str(obj_val) if obj_val else "",
                    width=150,
                    hint="tag / filename (no extension)…",
                    callback=self._update_object_filename_param,
                    user_data=(node_uuid, param_name),
                )
                dpg.add_button(
                    label="…",
                    width=30,
                    callback=self._browse_data_object_file,
                    user_data=(node_uuid, param_name, obj_input_tag),
                )
            dpg.add_text(
                "  (ref pin hidden while file is set)",
                color=tm.get_color("panel_text_muted"),
                parent=panel_tag,
            )

    # =========================================================================
    # Widget callbacks
    # =========================================================================

    def _on_ref_obj_mode_changed(self, sender, app_data, user_data):
        node_uuid, param_name = user_data
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            return

        new_mode    = "object" if "object" in app_data else "ref"
        param_modes = node_data.setdefault("param_modes", {})
        old_mode    = param_modes.get(param_name, "ref")

        if new_mode == old_mode:
            return

        param_modes[param_name] = new_mode
        attr_name = f"{param_name}_ref"

        if new_mode == "object":
            self._disconnect_ref_links(node_uuid, attr_name)
            self._set_ref_pin_visible(node_uuid, attr_name, False)
        else:
            node_data.get("values", {}).pop(f"{param_name}_object", None)
            node_data.get("suffixes", set()).discard(param_name)
            self._set_ref_pin_visible(node_uuid, attr_name, True)

        self._refresh_node_theme(node_uuid)
        self.update_node_panel(node_uuid, "property_panel")

    def _update_node_name(self, sender, app_data, user_data):
        node_uuid = user_data
        new_name  = app_data
        if node_uuid in self.graph.nodes:
            self.graph.nodes[node_uuid]["name"] = new_name
            dpg_id = self.registry.uuid_to_dpg.get(node_uuid)
            if dpg_id:
                dpg.set_item_label(
                    dpg_id,
                    f"{new_name} ({self.graph.nodes[node_uuid]['type']})",
                )

    def _parse_value(self, value):
        if not isinstance(value, str):
            return value
        val_stripped = value.strip()
        if not val_stripped:
            return ""
        try:
            return ast.literal_eval(val_stripped)
        except (ValueError, SyntaxError):
            return val_stripped

    def _update_param(self, sender, app_data, user_data):
        node_uuid, param_name, target_type = user_data
        node_data   = self.graph.nodes[node_uuid]
        values_dict = node_data.setdefault("values", {})

        try:
            raw_input   = app_data.strip() if isinstance(app_data, str) else str(app_data)
            lower_input = raw_input.lower()
            final_val   = raw_input

            if lower_input in ["inf", "infinity", ".inf"]:
                final_val = np.inf
            elif lower_input in ["-inf", "-infinity", "-.inf"]:
                final_val = -np.inf
            elif target_type == "list" or raw_input.startswith("["):
                try:
                    import re as _re
                    yaml_ready = _re.sub(
                        r'(?<![.\w])-?inf(?![\w])',
                        lambda m: str(np.inf) if not m.group().startswith('-') else str(-np.inf),
                        raw_input, flags=_re.IGNORECASE,
                    )
                    final_val = ast.literal_eval(yaml_ready)
                except Exception:
                    print(f"Warning: Invalid list syntax for {param_name}")
                    return
            elif target_type in ["float", "double", "number"]:
                if not isinstance(final_val, float):
                    final_val = float(raw_input)
            elif target_type in ["int", "integer"]:
                final_val = int(raw_input)
            elif target_type in ["bool", "boolean"]:
                final_val = lower_input in ["true", "1", "yes", "on"]

            # Allow clearing a value back to "unset"
            if raw_input == "":
                values_dict.pop(param_name, None)
            else:
                values_dict[param_name] = final_val

            self._refresh_node_theme(node_uuid)

        except Exception as e:
            print(f"Error updating parameter {param_name}: {e}")

    def _update_data_object_param(self, sender, app_data, user_data):
        node_uuid, param_name = user_data
        if node_uuid in self.graph.nodes:
            node_data = self.graph.nodes[node_uuid]
            node_data.setdefault("suffixes", set()).add(param_name)
            node_data.setdefault("values", {})
            node_data["values"][param_name]             = app_data
            node_data["values"][f"{param_name}_object"] = app_data
            self._refresh_node_theme(node_uuid)

    def _update_object_filename_param(self, sender, app_data, user_data):
        node_uuid, param_name = user_data
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            return

        filename = app_data.strip()
        values   = node_data.setdefault("values", {})

        if filename:
            node_data.setdefault("suffixes", set()).add(param_name)
            values[param_name]             = filename
            values[f"{param_name}_object"] = filename
        else:
            # Empty → revert to ref mode
            values.pop(f"{param_name}_object", None)
            values.pop(param_name, None)
            node_data.get("suffixes", set()).discard(param_name)
            node_data.setdefault("param_modes", {})[param_name] = "ref"
            self._set_ref_pin_visible(node_uuid, f"{param_name}_ref", True)
            self.update_node_panel(node_uuid, "property_panel")
            return

        self._refresh_node_theme(node_uuid)

    def _update_data_param(self, sender, app_data, user_data):
        """Callback for the inline-value field of a _data (array) parameter."""
        node_uuid, param_name = user_data
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            return
        try:
            import re as _re
            yaml_ready = _re.sub(
                r'(?<![.\w])-?inf(?![\w])',
                lambda m: str(np.inf) if not m.group().startswith('-') else str(-np.inf),
                app_data, flags=_re.IGNORECASE,
            )
            parsed = ast.literal_eval(yaml_ready)
        except Exception:
            parsed = app_data if app_data.strip() else None

        values = node_data.setdefault("values", {})
        if parsed is None:
            values.pop(param_name, None)
        else:
            values[param_name] = parsed
        # Clear _data file when user types inline values
        values.pop(f"{param_name}_data", None)
        self._refresh_node_theme(node_uuid)

    def _update_data_file_param(self, sender, app_data, user_data):
        """Callback for the _data filename field of an array parameter."""
        node_uuid, param_name = user_data
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            return
        filename = app_data.strip()
        values   = node_data.setdefault("values", {})
        if filename:
            values[f"{param_name}_data"] = filename
            values.pop(param_name, None)
        else:
            values.pop(f"{param_name}_data", None)
        self._refresh_node_theme(node_uuid)

    def _browse_data_object_file(self, sender, app_data, user_data):
        """
        Open a file-browser for a data-object / _data / _tag / _object parameter.

        Browse dialog opens in  <root_dir>/<folder_name>  where:
        - root_dir   comes from the SimulParams node (values['root_dir'])
        - folder_name = _folder_for_param(param_name)

        The only hard-coded folder exception is  recmat → rec.
        All other parameters map to their own name.
        """
        if len(user_data) == 3:
            node_uuid, param_name, input_tag = user_data
        else:
            node_uuid, param_name, input_tag, *_ = user_data

        param_folder = _folder_for_param(param_name)
        dialog_tag   = f"file_dialog_{node_uuid}_{param_folder}"

        if dpg.does_item_exist(dialog_tag):
            dpg.show_item(dialog_tag)
            dpg.focus_item(dialog_tag)
            return

        def _file_selected(dialog_sender, dialog_app_data, dialog_user_data):
            file_path = None
            if isinstance(dialog_app_data, dict):
                file_path = (
                    dialog_app_data.get("file_path_name")
                    or dialog_app_data.get("file_path")
                )
                if not file_path:
                    selections = (
                        dialog_app_data.get("selections")
                        or dialog_app_data.get("file_names")
                        or dialog_app_data.get("file_name")
                    )
                    if isinstance(selections, (list, tuple)) and selections:
                        file_path = selections[0]
                    elif isinstance(selections, str):
                        file_path = selections
                if file_path and file_path.endswith(".*"):
                    fname   = dialog_app_data.get("file_name") or (
                        dialog_app_data.get("selections") or [None]
                    )[0]
                    dirpart = file_path[:-2] if file_path.endswith("/.*") else file_path
                    if fname:
                        if not os.path.isabs(fname) and os.path.isdir(dirpart):
                            file_path = os.path.join(dirpart, fname)
                        else:
                            file_path = fname
            elif isinstance(dialog_app_data, (list, tuple)) and dialog_app_data:
                file_path = dialog_app_data[0]

            if isinstance(file_path, str) and file_path.endswith(".*"):
                file_path = file_path[:-2]

            if file_path:
                filename_only = os.path.basename(file_path)
                # Strip extension — CalibManager uses bare tag names
                bare_name = filename_only
                for ext in (".fits", ".fit", ".npy", ".npz"):
                    if bare_name.lower().endswith(ext):
                        bare_name = bare_name[: -len(ext)]
                        break

                try:
                    if dpg.does_item_exist(input_tag):
                        dpg.set_value(input_tag, bare_name)
                except Exception as exc:
                    print(f"[BROWSE] Could not set input tag {input_tag}: {exc}")

                # Route to the right callback depending on widget-tag suffix
                try:
                    if input_tag.endswith("_data_file_input"):
                        self._update_data_file_param(
                            None, bare_name, (node_uuid, param_name)
                        )
                    elif input_tag.endswith("_obj_input"):
                        self._update_object_filename_param(
                            None, bare_name, (node_uuid, param_name)
                        )
                    else:
                        self._update_data_object_param(
                            None, bare_name, (node_uuid, param_folder)
                        )
                except Exception as exc:
                    print(f"[BROWSE] Error updating node value: {exc}")

            try:
                dpg.hide_item(dialog_tag)
                dpg.delete_item(dialog_tag)
            except Exception:
                pass

        # Determine initial directory from SimulParams.root_dir
        initial_dir = None
        for nd in self.graph.nodes.values():
            if nd.get("type") == "SimulParams":
                root_dir = nd.get("values", {}).get("root_dir")
                if root_dir:
                    initial_dir = os.path.join(str(root_dir), param_folder)
                    print(f"[BROWSE] root_dir='{root_dir}', "
                          f"param='{param_name}', folder='{param_folder}', "
                          f"initial_dir='{initial_dir}'")
                    break

        dialog_kwargs = {
            "directory_selector": False,
            "show": True,
            "callback": _file_selected,
            "tag": dialog_tag,
            "width": 700,
            "height": 400,
        }
        if initial_dir:
            dialog_kwargs["default_path"] = initial_dir

        with dpg.file_dialog(**dialog_kwargs):
            dpg.add_file_extension(".fits", color=(150, 255, 150), custom_text="FITS")
            dpg.add_file_extension(".fit",  color=(150, 200, 150), custom_text="FIT")
            dpg.add_file_extension(".npy",  color=(150, 150, 255), custom_text="NumPy")
            dpg.add_file_extension(".npz",  color=(100, 100, 255), custom_text="NumPy-Z")
            dpg.add_file_extension(".*",    color=(150, 150, 150), custom_text="All files")

    def _disconnect_reference(self, sender, app_data, user_data):
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
        link_id = None
        for lid, conn_data in self.registry.link_registry.items():
            if (
                conn_data[0] == src_uuid and conn_data[1] == src_attr
                and conn_data[2] == dst_uuid and conn_data[3] == dst_attr
            ):
                link_id = lid
                break
        if not link_id or not dpg.does_item_exist(link_id):
            return
        tm = self.theme_manager
        if delay == -1:
            apply_link_style(link_id, color=tm.get_color("node_feedback_link_color"))
            self._update_feedback_attribute(src_uuid, src_attr, delay)
        elif delay == 0:
            if dst_attr.endswith("_ref") or "params" in dst_attr.lower():
                apply_link_style(link_id, color=tm.get_color("node_ref_link_color"))
            else:
                dpg.configure_item(link_id, color=tm.get_color("link_default"))

    def _update_feedback_attribute(self, node_uuid, attr_name, delay: int):
        attr_id = None
        for aid, (uid, name) in self.registry.output_attr_registry.items():
            if uid == node_uuid and name == attr_name:
                attr_id = aid
                break
        if not attr_id or not dpg.does_item_exist(attr_id):
            return
        tm = self.theme_manager
        children = dpg.get_item_children(attr_id, slot=1)
        for child in children:
            if dpg.get_item_type(child) == "mvAppItemType::mvText":
                current_text = dpg.get_value(child)
                if delay == -1 and ":-1" not in current_text:
                    dpg.set_value(child, f"{attr_name}:-1")
                    dpg.configure_item(child, color=tm.get_color("text_error"))
                elif delay == 0 and ":-1" in current_text:
                    dpg.set_value(child, attr_name.replace(":-1", ""))
                    dpg.configure_item(child, color=tm.get_color("node_output_label"))
                break

    def _update_connection_filename(self, sender, app_data, user_data):
        node_uuid, src_uuid, src_attr = user_data
        self.update_connection_filename(node_uuid, src_uuid, src_attr, app_data)

    # =========================================================================
    # Pin-visibility helpers
    # =========================================================================

    def _set_ref_pin_visible(self, node_uuid: str, attr_name: str, visible: bool):
        for attr_id, (uid, name) in self.registry.input_attr_registry.items():
            if uid == node_uuid and name == attr_name:
                if dpg.does_item_exist(attr_id):
                    try:
                        dpg.configure_item(attr_id, show=visible)
                    except Exception as exc:
                        print(f"[PANEL] _set_ref_pin_visible: {exc}")
                break

    def _disconnect_ref_links(self, node_uuid: str, attr_name: str):
        links_to_remove = [
            lid
            for lid, (su, sa, du, da) in list(self.registry.link_registry.items())
            if du == node_uuid and da == attr_name
        ]
        for lid in links_to_remove:
            self._delink_callback(None, lid)

    def restore_param_mode_pins(self, node_uuid: str):
        """Re-hide ref pins that were in _object mode after a full UI rebuild."""
        node_data = self.graph.nodes.get(node_uuid, {})
        for param_name, mode in node_data.get("param_modes", {}).items():
            if mode == "object":
                self._set_ref_pin_visible(node_uuid, f"{param_name}_ref", False)

    # =========================================================================
    # Single-widget renderer (non-reference parameters)
    # =========================================================================

    @staticmethod
    def _float_to_display(val) -> str:
        try:
            if val == np.inf or val == float("inf"):
                return "inf"
            if val == -np.inf or val == float("-inf"):
                return "-inf"
        except Exception:
            pass
        return str(val)

    @staticmethod
    def _default_hint_text(default_val) -> str:
        """Return the grey hint string shown beside every value widget."""
        if default_val == "REQUIRED":
            return "(REQUIRED)"
        if default_val is None:
            return "(default: null)"
        return f"(default: {default_val})"

    def _render_single_widget(
        self, parent, node_uuid, param_name, val, type_hint, default_val=None
    ):
        """
        Render one parameter row.

        Layout
        ------
        param_name:  [  input widget  ]  (default: X)

        The input widget is always initialised with the current stored value
        when one exists, or left empty when the parameter is not set in the
        YAML.  It is never pre-filled with the template default — that value is
        shown as grey hint text to the right so the user knows what the code
        will fall back to.
        """
        node_data  = self.graph.nodes.get(node_uuid, {})
        node_type  = node_data.get("type", "")
        template   = self.all_templates.get(node_type, {})
        param_meta = template.get("parameters", {}).get(param_name, {})
        param_kind = (
            param_meta.get("kind", "value") if isinstance(param_meta, dict) else "value"
        )
        tm = self.theme_manager

        # Legacy safety: reference/object params should not reach here, but guard
        if param_kind in ("reference", "object") and val is not None:
            with dpg.group(horizontal=True, parent=parent):
                lbl = dpg.add_text(
                    f"{param_name}:",
                    color=tm.get_color("panel_label_data_obj"),
                )
                _add_tooltip(lbl, get_param_tooltip(node_type, param_name))
                dpg.add_text(
                    f"{val}",
                    color=tm.get_color("panel_conn_src_name"),
                )
            return

        is_data_object = (
            param_kind in ("object",)
            or param_name in node_data.get("suffixes", set())
            or self.is_data_class_type(type_hint)
        )
        is_required = default_val == "REQUIRED"
        has_value   = val is not None and val not in ("", "REQUIRED")

        # ── label colour ──────────────────────────────────────────────────────
        if is_required and not has_value:
            label_color = tm.get_color("panel_label_required")
        elif is_data_object:
            label_color = tm.get_color("panel_label_data_obj")
        elif param_kind == "reference":
            label_color = tm.get_color("panel_label_ref")
        elif default_val is not None and val == default_val:
            label_color = tm.get_color("param_default")
        elif has_value:
            label_color = tm.get_color("param_modified")
        else:
            label_color = tm.get_color("param_default")

        # ── current display value (empty string when not set) ─────────────────
        if val is None:
            display_val = ""
        else:
            display_val = self._float_to_display(val)

        hint_text  = self._default_hint_text(default_val)
        user_data  = (node_uuid, param_name, type_hint)
        input_tag  = f"{node_uuid}_{param_name}_object"
        is_tag_param = (param_name == "tag" or param_name.endswith("_tag"))

        # Tooltip text for this parameter (empty string → no tooltip)
        param_tip = get_param_tooltip(node_type, param_name)

        with dpg.group(horizontal=True, parent=parent):
            lbl = dpg.add_text(f"{param_name}:", color=label_color)
            _add_tooltip(lbl, param_tip)

            if type_hint in ("bool", "boolean"):
                bool_val = bool(val) if val is not None else False
                dpg.add_checkbox(
                    default_value=bool_val,
                    callback=self._update_param,
                    user_data=user_data,
                )

            elif type_hint in ("int", "integer"):
                dpg.add_input_text(
                    default_value=display_val,
                    width=100,
                    hint="integer",
                    callback=self._update_param,
                    user_data=user_data,
                )

            elif type_hint in ("float", "double", "number"):
                dpg.add_input_text(
                    default_value=display_val,
                    width=100,
                    hint="number",
                    callback=self._update_param,
                    user_data=user_data,
                )

            elif isinstance(val, list) or type_hint in _ARRAY_TYPE_HINTS or param_kind == "data":
                # ── array / list / data parameter ─────────────────────────────
                existing_data_file = node_data.get("values", {}).get(
                    f"{param_name}_data", ""
                )
                if is_data_object:
                    dpg.add_input_text(
                        tag=input_tag,
                        default_value=display_val,
                        width=140,
                        callback=self._update_data_object_param,
                        user_data=(node_uuid, param_name),
                    )
                else:
                    inline_tag = f"{node_uuid}_{param_name}_inline"
                    dpg.add_input_text(
                        tag=inline_tag,
                        default_value="" if existing_data_file else display_val,
                        width=140,
                        hint="[v0, v1, …]",
                        callback=self._update_data_param,
                        user_data=(node_uuid, param_name),
                    )

            else:
                # ── plain string / unknown type ───────────────────────────────
                if is_data_object:
                    dpg.add_input_text(
                        tag=input_tag,
                        default_value=display_val,
                        width=140,
                        hint="file path / object id",
                        callback=self._update_data_object_param,
                        user_data=(node_uuid, param_name),
                    )
                    dpg.add_button(
                        label="…", width=30,
                        callback=self._browse_data_object_file,
                        user_data=(node_uuid, param_name, input_tag),
                    )
                elif is_tag_param:
                    tag_input_tag = f"{node_uuid}_{param_name}_tag_input"
                    dpg.add_input_text(
                        tag=tag_input_tag,
                        default_value=display_val,
                        width=140,
                        hint="calibration tag / filename…",
                        callback=self._update_param,
                        user_data=user_data,
                    )
                    dpg.add_button(
                        label="…", width=30,
                        callback=self._browse_data_object_file,
                        user_data=(node_uuid, param_name, tag_input_tag),
                    )
                else:
                    dpg.add_input_text(
                        default_value=display_val,
                        width=140,
                        callback=self._update_param,
                        user_data=user_data,
                    )

            # ── unit label + default hint (grey text to the right) ────────────
            if type_hint not in ("bool", "boolean"):
                unit_str = get_param_unit(node_type, param_name) if _HELP_AVAILABLE else ""
                if unit_str:
                    dpg.add_text(
                        f"[{unit_str}]",
                        color=tm.get_color("param_unit"),
                    )

                hint_color = (
                    tm.get_color("panel_label_required") if is_required and not has_value
                    else tm.get_color("param_hint")
                )
                dpg.add_text(hint_text, color=hint_color)

        # ── _data file row (below, for array params that are not objects) ─────
        if (
            (isinstance(val, list) or type_hint in _ARRAY_TYPE_HINTS or param_kind == "data")
            and not is_data_object
        ):
            existing_data_file = node_data.get("values", {}).get(f"{param_name}_data", "")
            data_tag = f"{node_uuid}_{param_name}_data_file_input"
            with dpg.group(horizontal=True, parent=parent):
                dpg.add_text(
                    f"  {param_name}_data:",
                    color=tm.get_color("panel_label_data_obj"),
                )
                dpg.add_input_text(
                    tag=data_tag,
                    default_value=str(existing_data_file) if existing_data_file else "",
                    width=120,
                    hint="filename / tag…",
                    callback=self._update_data_file_param,
                    user_data=(node_uuid, param_name),
                )
                dpg.add_button(
                    label="…", width=30,
                    callback=self._browse_data_object_file,
                    user_data=(node_uuid, param_name, data_tag),
                )

    def is_data_class_type(self, type_name: str) -> bool:
        """Return True if *type_name* looks like a Specula data-object type."""
        if not type_name or type_name == "Any":
            return False
        if hasattr(self, "_data_obj_templates") and type_name in self._data_obj_templates:
            return True
        data_keywords = [
            "Matrix", "Vector", "Atmosphere", "Telescope", "Detector", "Field",
        ]
        return any(k in type_name for k in data_keywords)

    # =========================================================================
    # Helpers
    # =========================================================================

    def get_connections_for_node(self, node_uuid: str):
        incoming = []
        outgoing = []
        for src_u, src_at, dst_u, dst_at in self.graph.connections:
            if dst_u == node_uuid:
                incoming.append({
                    "src_node": src_u, "src_attr": src_at, "dst_attr": dst_at,
                    "src_name": self.graph.nodes[src_u].get("name", "unknown"),
                    "dst_name": self.graph.nodes[node_uuid].get("name", "unknown"),
                    "type": "input",
                })
            if src_u == node_uuid:
                outgoing.append({
                    "dst_node": dst_u, "src_attr": src_at, "dst_attr": dst_at,
                    "src_name": self.graph.nodes[node_uuid].get("name", "unknown"),
                    "dst_name": self.graph.nodes[dst_u].get("name", "unknown"),
                    "type": "output",
                })
        return incoming, outgoing

    def update_connection_filename(
        self, node_uuid: str, src_uuid: str, src_attr: str, new_filename: str
    ):
        self.graph.nodes[node_uuid].setdefault("filename_map", {})
        self.graph.nodes[node_uuid]["filename_map"][f"{src_uuid}.{src_attr}"] = new_filename

    def get_connection_filename(
        self, node_uuid: str, src_uuid: str, src_attr: str
    ) -> str:
        filename_map = self.graph.nodes.get(node_uuid, {}).get("filename_map", {})
        return filename_map.get(f"{src_uuid}.{src_attr}", "data")