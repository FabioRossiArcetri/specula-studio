"""Graphviz (DOT) export of a simulation graph."""

from __future__ import annotations

from .model import Graph
from .syntax import is_ref_attr


def _esc(text) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _q(text) -> str:
    return f'"{_esc(text)}"'


def to_dot(graph: Graph, title: str = "") -> str:
    """
    Processing objects are boxes, data objects ellipses.  Data connections are
    solid (labelled output → input), feedback (delay -1) connections red and
    dashed, references grey and dotted.
    """
    lines = ["digraph specula {", "  rankdir=LR;",
             '  node [fontname="Helvetica", fontsize=10];',
             '  edge [fontname="Helvetica", fontsize=8];']
    if title:
        lines.append(f"  label={_q(title)}; labelloc=t;")
    for u, node in graph.nodes.items():
        tpl = graph.templates.get(node.get("type"), {})
        shape = "ellipse" if tpl.get("is_data_obj") else "box"
        label = f'"{_esc(node.get("name"))}\\n({_esc(node.get("type"))})"'   # DOT newline
        lines.append(f"  {_q(u)} [label={label}, shape={shape}];")
    for conn in graph.connections:
        src, src_attr, dst, dst_attr = conn
        if src not in graph.nodes or dst not in graph.nodes:
            continue
        delay = graph.connection_properties.get(conn, {}).get("delay", 0)
        if is_ref_attr(dst_attr):
            attrs = f'style=dotted, color=gray50, label={_q(dst_attr)}'
        elif delay == -1:
            attrs = f'style=dashed, color=red, label={_q(f"{src_attr} -> {dst_attr} (t-1)")}'
        else:
            attrs = f'label={_q(f"{src_attr} -> {dst_attr}")}'
        lines.append(f"  {_q(src)} -> {_q(dst)} [{attrs}];")
    lines.append("}")
    return "\n".join(lines) + "\n"
