"""
Load / save SPECULA YAML to and from the :class:`~.model.Graph` model.

This module is pure Python (no DearPyGui) so the complete round trip can be
unit-tested; the UI only renders whatever graph these functions produce.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections import OrderedDict

import yaml

from .model import Graph
from .syntax import (
    REF_OUTPUT,
    REQUIRED,
    add_ref_value,
    format_source,
    is_ref_attr,
    parse_source,
)

log = logging.getLogger(__name__)

STUDIO_META_KEY = "_overrides_metadata"

# Inputs that SPECULA always expects as lists, even with a single entry.
_ALWAYS_LIST_INPUTS = ("input_list", "atmo_layer_list", "common_layer_list")

# Keyword heuristic for data-object types not present in the templates.
_DATA_TYPE_KEYWORDS = ("Matrix", "Vector", "Atmosphere", "Telescope", "Detector", "Field")


# ── YAML text helpers ─────────────────────────────────────────────────────────

def ordered_load(stream, Loader=yaml.SafeLoader, object_pairs_hook=OrderedDict):
    """``yaml.safe_load`` preserving mapping order in OrderedDicts."""
    class OrderedLoader(Loader):
        pass

    def construct_mapping(loader, node):
        loader.flatten_mapping(node)
        return object_pairs_hook(loader.construct_pairs(node))

    OrderedLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return yaml.load(stream, OrderedLoader)


class _CleanDumper(yaml.SafeDumper):
    """Safe dumper that writes OrderedDicts as plain mappings."""


_CleanDumper.add_representer(
    OrderedDict,
    lambda dumper, data: dumper.represent_mapping("tag:yaml.org,2002:map", data.items()),
)


_CleanDumper.add_representer(tuple, lambda d, t: d.represent_list(list(t)))


def _represent_numpy(dumper, data):
    return dumper.represent_data(data.item() if hasattr(data, "item") else float(data))


try:  # numpy scalars sneak in via ast.literal_eval of inf, etc.
    import numpy as _np

    for _t in (_np.floating, _np.integer, _np.bool_):
        _CleanDumper.add_multi_representer(_t, _represent_numpy)
    _CleanDumper.add_representer(
        _np.ndarray, lambda d, a: d.represent_list(a.tolist()))
except ImportError:  # pragma: no cover
    pass


def dump_yaml(data) -> str:
    return yaml.dump(data, Dumper=_CleanDumper, sort_keys=False,
                     default_flow_style=False, allow_unicode=True)


def read_yaml_file(path) -> OrderedDict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = ordered_load(f)
    except Exception as exc:
        log.error("Error loading YAML file %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.error("YAML root of %s must be a mapping, got %s", path, type(data).__name__)
        return None
    return data


# ── type helpers ──────────────────────────────────────────────────────────────

def is_data_class_type(type_name, templates: dict | None = None) -> bool:
    """True if *type_name* looks like a SPECULA data-object type."""
    if not type_name or type_name == "Any" or not isinstance(type_name, str):
        return False
    if templates:
        tpl = templates.get(type_name)
        if isinstance(tpl, dict) and tpl.get("is_data_obj"):
            return True
    return any(k in type_name for k in _DATA_TYPE_KEYWORDS)


def new_uuid() -> str:
    return str(uuid.uuid4())[:8]


# ── loading ───────────────────────────────────────────────────────────────────

def _populate_nodes(graph: Graph, yaml_data: dict, templates: dict) -> dict:
    """Pass 1 — create nodes and parameter values.  Returns {name: uuid}."""
    name_to_uuid: dict = {}

    for node_name, content in yaml_data.items():
        if not isinstance(content, dict):
            log.warning("Skipping '%s' — expected a mapping, got %s",
                        node_name, type(content).__name__)
            continue
        if "class" not in content:
            log.warning("Skipping '%s' — missing 'class' key", node_name)
            continue

        node_type = content.get("class")
        u = new_uuid()
        while u in graph.nodes:
            u = new_uuid()
        name_to_uuid[node_name] = u

        graph.add_node(u, node_type)
        node_data = graph.nodes[u]
        node_data["name"] = node_name
        node_data["outputs_extra"] = []
        node_data["suffixes"] = set()
        node_data["values"] = {}
        node_data["param_modes"] = {}
        if "gui_pos" in content:
            node_data["gui_pos"] = content["gui_pos"]
        if isinstance(content.get("outputs"), list):
            node_data["file_outputs"] = list(content["outputs"])

        template_params = templates.get(node_type, {}).get("parameters", {})
        values = node_data["values"]

        for key, value in content.items():
            if key in ("class", "inputs", "outputs", "gui_pos"):
                continue
            if is_ref_attr(key):            # links, created in pass 2
                continue

            if key.endswith("_object") and not key.endswith(("_list_object", "_dict_object")):
                base_key = key[:-7]
                node_data["suffixes"].add(base_key)
                values[base_key] = value
                values[key] = value
                node_data["param_modes"][base_key] = "object"
                continue

            if key.endswith("_data"):
                base_key = key[:-5]
                values[key] = value
                values.setdefault(base_key, None)
                continue

            if key in template_params:
                meta = template_params[key]
                if isinstance(meta, dict) and meta.get("kind", "value") == "object":
                    node_data["suffixes"].add(key)
            values[key] = value

    return name_to_uuid


def _populate_connections(graph: Graph, yaml_data: dict, name_to_uuid: dict,
                          resolver: dict | None = None) -> None:
    """Pass 2 — data inputs and reference links.

    Sources are looked up in *resolver* (defaults to *name_to_uuid*), which
    lets a pasted subset connect to objects already in the graph.
    """
    resolver = resolver if resolver is not None else name_to_uuid
    for node_name, content in yaml_data.items():
        dst_u = name_to_uuid.get(node_name)
        if not dst_u:
            continue
        dst_values = graph.nodes[dst_u]["values"]

        for in_pin, src_raw in (content.get("inputs") or {}).items():
            for text in (src_raw if isinstance(src_raw, list) else [src_raw]):
                src = parse_source(text)
                if src is None or src.node not in resolver:
                    if src is not None:
                        log.warning("%s.%s: unknown source object '%s'",
                                    node_name, in_pin, src.node)
                    continue
                src_u = resolver[src.node]
                graph.add_connection(src_u, src.attr, dst_u, in_pin, {"delay": src.delay})
                if in_pin == "input_list" and src.alias:
                    graph.nodes[dst_u].setdefault("filename_map", {})[
                        f"{src_u}.{src.attr}"] = src.alias

        for key, val in content.items():
            if not is_ref_attr(key):
                continue
            for ref_name in (val if isinstance(val, list) else [val]):
                if not isinstance(ref_name, str):
                    continue
                if ref_name not in resolver:
                    log.warning("%s.%s: unknown referenced object '%s'", node_name, key, ref_name)
                    continue
                graph.add_connection(resolver[ref_name], REF_OUTPUT, dst_u, key, {"delay": 0})
                add_ref_value(dst_values, key, ref_name)


def _populate_dynamic_outputs(graph: Graph) -> None:
    """AtmoPropagation exposes one output per source: out_<source>_ef."""
    for node in graph.nodes.values():
        if node.get("type") != "AtmoPropagation":
            continue
        for src in node["values"].get("source_dict_ref", []) or []:
            out = f"out_{src}_ef"
            if out not in node["outputs_extra"]:
                node["outputs_extra"].append(out)


def load_into(graph: Graph, yaml_data: dict, templates: dict | None = None,
              resolve_existing: bool = False) -> dict:
    """
    Add the objects of *yaml_data* to *graph* (which may already hold nodes).
    Studio-private top-level keys (starting with ``_``) are ignored.
    With *resolve_existing*, inputs/references may also point to objects that
    were already in the graph.  Returns ``{object_name: node_uuid}`` for the
    objects added.
    """
    templates = templates if templates is not None else graph.templates
    data = OrderedDict((k, v) for k, v in yaml_data.items() if not str(k).startswith("_"))
    existing = graph.name_to_uuid() if resolve_existing else {}
    name_to_uuid = _populate_nodes(graph, data, templates)
    _populate_connections(graph, data, name_to_uuid, {**existing, **name_to_uuid})
    _populate_dynamic_outputs(graph)
    return name_to_uuid


def load_graph(yaml_data: dict, templates: dict) -> Graph:
    """Build a new :class:`Graph` from a parsed SPECULA YAML mapping."""
    graph = Graph(templates)
    load_into(graph, yaml_data, templates)
    return graph


# ── exporting ─────────────────────────────────────────────────────────────────

def connection_to_yaml(graph: Graph, conn) -> str:
    src_u, src_attr = conn[0], conn[1]
    delay = graph.connection_properties.get(conn, {}).get("delay", 0)
    src_name = graph.nodes.get(src_u, {}).get("name", "")
    return format_source(src_name, src_attr, delay)


def _node_outputs(graph: Graph, u_id: str, node_data: dict, template: dict) -> list:
    outputs: list = list(node_data.get("file_outputs", []))
    for out in template.get("outputs", []):
        if isinstance(out, str):
            if "name" in out and "+" in out and "'" in out:
                continue
            if ":" in out:
                continue
            if out not in outputs:
                outputs.append(out)
    for out in node_data.get("outputs_extra", []):
        if isinstance(out, str) and out not in outputs:
            outputs.append(out)
    if node_data.get("type") == "AtmoPropagation":
        for (src_u, _, dst_u, dst_at) in graph.connections:
            if dst_u == u_id and dst_at == "source_dict_ref":
                out_name = f"out_{graph.nodes[src_u].get('name', 'unknown')}_ef"
                if out_name not in outputs:
                    outputs.append(out_name)
    return outputs


def export_node(graph: Graph, u_id: str, include_defaults: bool = False) -> OrderedDict:
    node_data = graph.nodes[u_id]
    node_type = node_data["type"]
    template = graph.templates.get(node_type, {})
    template_params = template.get("parameters", {})

    node_dict = OrderedDict({"class": node_type})
    if "gui_pos" in node_data:
        node_dict["gui_pos"] = node_data["gui_pos"]

    outputs = _node_outputs(graph, u_id, node_data, template)
    if outputs:
        node_dict["outputs"] = outputs

    # ── parameters ────────────────────────────────────────────────────────────
    current_values = node_data.get("values", {})
    suffixes = node_data.get("suffixes", set())
    handled: set = set()

    for p_name, p_meta in template_params.items():
        if not isinstance(p_meta, dict):
            continue
        if p_meta.get("kind") == "reference":
            handled.add(p_name)
            continue
        val = current_values.get(p_name)
        kind = p_meta.get("kind", "value")
        # Explicit values are always written — even when equal to the template
        # default (SPECULA reads e.g. main.time_step / root_dir from the YAML
        # itself).  Unset parameters get their default only on request.
        if val is None or val == "":
            default = p_meta.get("default")
            has_data_file = current_values.get(f"{p_name}_data") not in (None, "")
            if include_defaults and default not in (None, "", REQUIRED) and not has_data_file:
                val = copy.deepcopy(default)
            else:
                handled.add(p_name)
                continue

        export_key = p_name
        if p_name in suffixes or kind == "object" or (
                isinstance(val, str) and is_data_class_type(p_meta.get("type"))):
            export_key = f"{p_name}_object"

        if val is not None and val != REQUIRED:
            node_dict[export_key] = val
        handled.add(p_name)

    for key, val in current_values.items():
        if key in handled or key.endswith("_ref"):
            continue
        if key in suffixes:
            node_dict[f"{key}_object"] = val
        else:
            node_dict[key] = val

    # ── connections ───────────────────────────────────────────────────────────
    input_connections: dict = {}
    ref_connections: dict = {}

    for conn in graph.connections:
        src_u, src_at, dst_u, dst_at = conn
        if dst_u != u_id:
            continue
        text = connection_to_yaml(graph, conn)
        pm = template_params.get(dst_at)
        is_ref = (isinstance(pm, dict) and pm.get("kind") == "reference") or is_ref_attr(dst_at)

        if is_ref:
            bucket = ref_connections.setdefault(dst_at, [])
        else:
            if dst_at == "input_list":
                alias = node_data.get("filename_map", {}).get(f"{src_u}.{src_at}", "data")
                text = f"{alias}-{text}"
            bucket = input_connections.setdefault(dst_at, [])
        if text not in bucket:
            bucket.append(text)

    if input_connections:
        node_dict["inputs"] = OrderedDict()
        for dst_at, sources in input_connections.items():
            if dst_at in _ALWAYS_LIST_INPUTS or len(sources) > 1:
                node_dict["inputs"][dst_at] = sources
            else:
                node_dict["inputs"][dst_at] = sources[0]

    for param_name, sources in ref_connections.items():
        if param_name in ("source_dict_ref", "layer_list"):
            node_dict[param_name] = sources
            continue
        export_key = param_name if param_name.endswith("_ref") else f"{param_name}_ref"
        tp = template_params.get(param_name, {})
        is_list = (
            isinstance(tp, dict) and tp.get("kind") == "reference"
            and "list" in str(tp.get("type", "")).lower()
        ) or len(sources) > 1 or param_name.endswith(("_list_ref", "_dict_ref"))
        node_dict[export_key] = sources if is_list else (sources[0] if sources else None)

    return node_dict


def export_graph(graph: Graph, include_defaults: bool = False) -> OrderedDict:
    """Export the whole graph to a SPECULA YAML mapping (insertion order kept)."""
    export_data = OrderedDict()
    for u_id, node_data in graph.nodes.items():
        export_data[node_data.get("name", node_data["type"])] = export_node(
            graph, u_id, include_defaults)
    return export_data


def strip_studio_fields(yaml_data: dict) -> dict:
    """Remove editor-only keys (``_*`` top-level keys, ``gui_pos``) in place."""
    for key in [k for k in yaml_data if str(k).startswith("_")]:
        del yaml_data[key]
    for node in yaml_data.values():
        if isinstance(node, dict):
            node.pop("gui_pos", None)
    return yaml_data


def semantic_diff(old: dict, new: dict, old_label: str = "saved",
                  new_label: str = "current") -> str:
    """
    Unified diff of two simulations as SPECULA sees them (positions and
    editor metadata ignored, both sides normalised by the same dumper).
    Empty string when they are equivalent.
    """
    import difflib

    def norm(data):
        data = strip_studio_fields(copy.deepcopy(OrderedDict(data)))
        return dump_yaml(data).splitlines(keepends=True)

    return "".join(difflib.unified_diff(norm(old), norm(new), old_label, new_label))
