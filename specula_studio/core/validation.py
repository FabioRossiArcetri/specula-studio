"""
Graph validation: which nodes are incomplete, and why.

The rules are the ones the editor has always used to draw the red
"incomplete" border, now returned as a list of :class:`Issue` objects so the
same information can drive the node border, the issues panel and the logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .model import Graph
from .syntax import REQUIRED, validate_node_name

ERROR = "error"
WARNING = "warning"

InputOptionalFn = Callable[[str, str], bool]


@dataclass(frozen=True)
class Issue:
    node_uuid: str | None
    message: str
    field: str | None = None
    severity: str = ERROR

    def describe(self, graph: Graph) -> str:
        name = graph.nodes.get(self.node_uuid, {}).get("name") if self.node_uuid else None
        where = f"{name}.{self.field}" if name and self.field else (name or self.field or "")
        return f"{where}: {self.message}" if where else self.message


def _never_optional(class_name: str, input_name: str) -> bool:
    return False


def _is_required(meta: dict) -> bool:
    return meta.get("default") == REQUIRED or bool(meta.get("required", False))


def node_issues(graph: Graph, node_uuid: str,
                is_input_optional: InputOptionalFn = _never_optional) -> list[Issue]:
    """All problems that make *node_uuid* incomplete."""
    node = graph.nodes.get(node_uuid)
    if node is None:
        return []
    node_type = node.get("type", "")
    template = graph.templates.get(node_type, {})
    values = node.get("values", {})
    modes = node.get("param_modes", {})
    issues: list[Issue] = []

    for name, meta in template.get("parameters", {}).items():
        if not isinstance(meta, dict) or not _is_required(meta):
            continue
        kind = meta.get("kind", "value")
        if kind in ("reference", "object"):
            if modes.get(name, "ref") == "object":
                if not values.get(f"{name}_object", ""):
                    issues.append(Issue(node_uuid, "required object file/tag not set", name))
            elif not values.get(f"{name}_ref"):
                issues.append(Issue(node_uuid, "required reference not connected", f"{name}_ref"))
        else:
            val = values.get(name)
            if val is None or val == "" or val == REQUIRED:
                issues.append(Issue(node_uuid, "required value not set", name))

    for input_name in template.get("inputs", {}):
        if is_input_optional(node_type, input_name):
            continue
        if not graph.has_input_connection(node_uuid, input_name):
            issues.append(Issue(node_uuid, "required input not connected", input_name))

    return issues


def graph_issues(graph: Graph,
                 is_input_optional: InputOptionalFn = _never_optional) -> list[Issue]:
    """Node issues plus graph-level problems (names, missing SimulParams)."""
    issues: list[Issue] = []
    seen: dict = {}
    for u, node in graph.nodes.items():
        name = node.get("name", "")
        err = validate_node_name(name)
        if err:
            issues.append(Issue(u, err, "name"))
        if name in seen:
            issues.append(Issue(u, f"duplicate object name '{name}'", "name"))
        seen.setdefault(name, u)
        template = graph.templates.get(node.get("type"))
        if not template or "class_name" not in template:   # generic fallback
            issues.append(Issue(u, f"class '{node.get('type')}' is unknown to the editor",
                                severity=WARNING))
        issues.extend(node_issues(graph, u, is_input_optional))

    if graph.nodes and not any(n.get("type") == "SimulParams" for n in graph.nodes.values()):
        issues.append(Issue(None, "no SimulParams object: SPECULA needs one (usually 'main')",
                            severity=WARNING))
    return issues
