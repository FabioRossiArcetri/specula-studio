"""
Copy / paste / duplicate of a set of objects.

The clipboard holds the selected objects in SPECULA YAML form.  On paste the
objects get fresh unique names; references *between* pasted objects follow
the renaming, while references to objects outside the selection are kept if
such an object exists in the target graph (so duplicating a PSF keeps its
input from the propagation object).
"""

from __future__ import annotations

import copy
from collections import OrderedDict

from . import yaml_io
from .model import Graph
from .syntax import format_source, is_ref_attr, parse_source, unique_name

PASTE_OFFSET = (40, 40)


def copy_nodes(graph: Graph, node_uuids) -> OrderedDict:
    """Snapshot *node_uuids* as ``{name: yaml_block}`` (insertion order kept)."""
    clip = OrderedDict()
    for u in graph.nodes:                      # graph order, not selection order
        if u in node_uuids:
            name = graph.nodes[u]["name"]
            clip[name] = copy.deepcopy(yaml_io.export_node(graph, u))
    return clip


def _rename_source(text, mapping):
    src = parse_source(text)
    if src is None or src.node not in mapping:
        return text
    return format_source(mapping[src.node], src.attr, src.delay, src.alias)


def _rename_in_block(block: dict, mapping: dict) -> dict:
    out = OrderedDict()
    for key, val in block.items():
        if key == "inputs" and isinstance(val, dict):
            val = OrderedDict(
                (k, [_rename_source(s, mapping) for s in v] if isinstance(v, list)
                 else _rename_source(v, mapping))
                for k, v in val.items()
            )
        elif is_ref_attr(key):
            if isinstance(val, list):
                val = [mapping.get(v, v) for v in val]
            elif isinstance(val, str):
                val = mapping.get(val, val)
        elif key == "outputs" and isinstance(val, list):
            # AtmoPropagation: out_<source>_ef follows a renamed source
            for old, new in mapping.items():
                val = [f"out_{new}_ef" if o == f"out_{old}_ef" else o for o in val]
        out[key] = val
    return out


def paste_nodes(graph: Graph, clip: dict, offset=PASTE_OFFSET) -> list:
    """Add *clip* to *graph* with unique names.  Returns the new node uuids."""
    if not clip:
        return []
    existing = set(graph.node_names())
    mapping = {}
    for name in clip:
        new_name = unique_name(name, existing)
        existing.add(new_name)
        mapping[name] = new_name

    data = OrderedDict()
    for name, block in clip.items():
        block = _rename_in_block(copy.deepcopy(block), mapping)
        if "gui_pos" in block and isinstance(block["gui_pos"], (list, tuple)):
            x, y = block["gui_pos"][:2]
            block["gui_pos"] = [x + offset[0], y + offset[1]]
        data[mapping[name]] = block

    name_to_uuid = yaml_io.load_into(graph, data, resolve_existing=True)
    return list(name_to_uuid.values())
