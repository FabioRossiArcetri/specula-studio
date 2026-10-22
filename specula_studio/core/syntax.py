"""
SPECULA YAML conventions, mirrored from ``specula.connections`` and
``specula.simul`` so that the editor reads and writes exactly what SPECULA
expects.  Nothing in here depends on DearPyGui.

Connection strings
------------------
``[alias-]obj_name.output_key[:delay]`` where *delay* is 0 or -1 (feedback).
A bare ``obj_name`` in an ``inputs`` entry references a data object directly
(the editor models it as the object's ``ref`` output).

Parameter-name suffixes
-----------------------
``_ref``          reference to another object of the same file
``_list_ref``     list of such references
``_dict_ref``     ``{name: obj}`` built from a list of object names
``_object``       data object restored by CalibManager from a tag
``_list_object`` / ``_dict_object``  lists/dicts of restored objects
``_data``         numpy array read by CalibManager from a tag
``tag``           restore the whole object from a tag
"""

from __future__ import annotations

from typing import NamedTuple

# Template marker for parameters without a default value.
REQUIRED = "REQUIRED"

# Pseudo output exposed by every data object so it can be referenced.
REF_OUTPUT = "ref"

# Inputs that the editor treats as list-valued references.
LIST_REF_ATTRS = ("layer_list",)

# SPECULA rejects these in object names (simul.validate_section_names).
FORBIDDEN_NAME_CHARS = ".-:"


class Source(NamedTuple):
    """Parsed connection string."""
    node: str
    attr: str
    delay: int = 0
    alias: str | None = None


def parse_source(text: str) -> Source | None:
    """
    Parse a SPECULA connection string.

    >>> parse_source("dm.out_layer:-1")
    Source(node='dm', attr='out_layer', delay=-1, alias=None)
    >>> parse_source("res_sr-psf.out_sr")
    Source(node='psf', attr='out_sr', delay=0, alias='res_sr')
    >>> parse_source("pupilstop")
    Source(node='pupilstop', attr='ref', delay=0, alias=None)
    """
    if not isinstance(text, str) or not text:
        return None
    body, delay = text, 0
    if ":" in body:
        head, suffix = body.rsplit(":", 1)
        try:
            delay = int(suffix)
            body = head
        except ValueError:
            delay = 0
    alias = None
    if "-" in body:
        alias, body = body.split("-", 1)
    if "." in body:
        node, attr = body.split(".", 1)
    else:
        node, attr = body, REF_OUTPUT
    return Source(node, attr, delay, alias)


def format_source(node: str, attr: str, delay: int = 0, alias: str | None = None) -> str:
    """Inverse of :func:`parse_source`."""
    text = node if attr == REF_OUTPUT else f"{node}.{attr}"
    if delay:
        text = f"{text}:{delay}"
    if alias:
        text = f"{alias}-{text}"
    return text


def output_owner(text: str) -> str | None:
    """Name of the object producing the output referenced by *text*."""
    src = parse_source(text)
    return src.node if src else None


# ── references ────────────────────────────────────────────────────────────────

def is_ref_attr(attr: str) -> bool:
    """True for input attributes that are object references (square pins)."""
    return attr.endswith("_ref") or attr in LIST_REF_ATTRS


def is_list_ref(attr: str) -> bool:
    """True for reference attributes that hold a *list* of object names."""
    return attr in LIST_REF_ATTRS or attr.endswith(("_list_ref", "_dict_ref"))


def add_ref_value(values: dict, attr: str, name: str) -> None:
    """Record that reference *attr* points to object *name*."""
    if is_list_ref(attr):
        current = values.get(attr)
        if not isinstance(current, list):
            current = [] if current in (None, "") else [current]
        if name not in current:
            current.append(name)
        values[attr] = current
    else:
        values[attr] = name


def remove_ref_value(values: dict, attr: str, name: str) -> None:
    """Undo :func:`add_ref_value`; drops the key when nothing is left."""
    current = values.get(attr)
    if isinstance(current, list):
        if name in current:
            current.remove(name)
        if not current:
            values.pop(attr, None)
    elif current == name:
        values.pop(attr, None)


# ── names ─────────────────────────────────────────────────────────────────────

def validate_node_name(name: str, existing_names=()) -> str | None:
    """Return an error message if *name* is not a valid object name, else None."""
    if not name or not name.strip():
        return "Name cannot be empty"
    if any(c.isspace() for c in name):
        return "Name cannot contain whitespace"
    bad = sorted({c for c in name if c in FORBIDDEN_NAME_CHARS})
    if bad:
        return f"Name cannot contain {' '.join(repr(c) for c in bad)}"
    if name.startswith("_"):
        return "Names starting with '_' are reserved"
    if name in existing_names:
        return f"An object named '{name}' already exists"
    return None


def unique_name(base: str, existing) -> str:
    """*base* if free, else ``base_1``, ``base_2`` …"""
    if base not in existing:
        return base
    i = 1
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"
