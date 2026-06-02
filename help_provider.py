"""
help_provider.py
================
Extract and format documentation from the live SPECULA package.

Sources (in priority order):
1. Class-level __doc__  (NumPy / Google-style, both understood)
2. __init__ __doc__
3. input_names() / output_names() classmethods  →  InputDesc/OutputDesc .desc
4. inspect.signature  →  type annotations and defaults

Results are cached per class name so repeated calls are free.
"""

import inspect
import importlib
import pkgutil
import re
import textwrap
from functools import lru_cache


# Packages to search for SPECULA classes
_SPECULA_PACKAGES = [
    "specula.processing_objects",
    "specula.data_objects",
]

# NumPy-style section header: "Parameters\n----------" or "Parameters\n=========="
_SECTION_HEADER_RE = re.compile(
    r"^[ \t]*(?P<title>[A-Za-z][A-Za-z0-9 _]*?)\s*\n[ \t]*[-=]{3,}\s*$",
    re.MULTILINE,
)

# One parameter entry inside a Parameters section:
#   name : type [unit], optional
#       Description text, possibly multiple lines.
_PARAM_ENTRY_RE = re.compile(
    r"^(?P<pname>\w+)"
    r"(?:\s*:\s*(?P<ptype>[^\n]+?))?"
    r"\s*\n"
    r"(?P<pdesc>(?:[ \t]+[^\n]*\n?)*)",
    re.MULTILINE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dedent(text: str) -> str:
    return textwrap.dedent(text or "").strip()


def _parse_numpy_sections(docstring: str) -> dict[str, str]:
    """Split a NumPy/Google docstring into {section_title: body_text}."""
    if not docstring:
        return {}
    text = _dedent(docstring)
    hits = list(_SECTION_HEADER_RE.finditer(text))
    if not hits:
        return {"Summary": text}

    out: dict[str, str] = {}
    preamble = text[: hits[0].start()].strip()
    if preamble:
        out["Summary"] = preamble

    for i, m in enumerate(hits):
        title = m.group("title").strip()
        body_start = m.end()
        body_end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out[title] = text[body_start:body_end].strip()

    return out


def _parse_param_section(body: str) -> dict[str, tuple[str, str]]:
    """Parse a 'Parameters' section body → {name: (type_str, desc_str)}."""
    result: dict[str, tuple[str, str]] = {}
    for m in _PARAM_ENTRY_RE.finditer((body or "") + "\n"):
        pname = m.group("pname").strip()
        ptype = (m.group("ptype") or "").strip()
        # Strip [unit] and 'optional' annotations from type string for tooltip brevity
        ptype_clean = re.sub(r"\s*\[.*?\]", "", ptype).replace(", optional", "").strip()
        pdesc = _dedent(m.group("pdesc") or "")
        result[pname] = (ptype_clean, pdesc)
    return result


def _type_name(obj) -> str:
    """Return a short readable type name from a class or annotation."""
    if obj is None or obj is inspect.Parameter.empty:
        return ""
    if hasattr(obj, "__name__"):
        return obj.__name__
    s = str(obj)
    # Strip typing.Optional[X] → Optional[X], typing.List[X] → List[X], …
    s = re.sub(r"\btyping\.", "", s)
    return s


@lru_cache(maxsize=256)
def _find_class(class_name: str):
    """Locate and return the class object in the SPECULA packages, or None."""
    for pkg_name in _SPECULA_PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        for _, mod_name, _ in pkgutil.iter_modules(pkg.__path__):
            full = f"{pkg_name}.{mod_name}"
            try:
                mod = importlib.import_module(full)
            except Exception:
                continue
            klass = getattr(mod, class_name, None)
            if klass is not None and inspect.isclass(klass):
                return klass
    return None


# ── Public API ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def get_class_help(class_name: str) -> dict:
    """
    Return a structured help dictionary for *class_name*.

    Keys
    ----
    class_name : str
    summary    : str   – first line of the class docstring
    full_doc   : str   – complete cleaned docstring
    category   : str   – "processing_objects" | "data_objects" | "unknown"
    parameters : dict  name → {"type": str, "default": str, "desc": str}
    inputs     : dict  name → {"type": str, "desc": str}
    outputs    : dict  name → {"type": str, "desc": str}
    error      : str | None
    """
    result: dict = {
        "class_name": class_name,
        "summary":    "",
        "full_doc":   "",
        "category":   "unknown",
        "parameters": {},
        "inputs":     {},
        "outputs":    {},
        "error":      None,
    }

    klass = _find_class(class_name)
    if klass is None:
        result["error"] = (
            f"Class '{class_name}' not found in the installed SPECULA package.\n"
            "Make sure SPECULA is installed in the same environment."
        )
        return result

    # ── Category from module path ─────────────────────────────────────────────
    mod = klass.__module__ or ""
    if "processing_objects" in mod:
        result["category"] = "processing_objects"
    elif "data_objects" in mod:
        result["category"] = "data_objects"

    # ── Docstrings ────────────────────────────────────────────────────────────
    class_doc = inspect.getdoc(klass) or ""
    init_doc  = inspect.getdoc(klass.__init__) or ""

    # Prefer class-level doc; fall back to __init__ doc
    primary_doc = class_doc or init_doc
    sections    = _parse_numpy_sections(primary_doc)

    result["full_doc"] = primary_doc
    result["summary"]  = (sections.get("Summary", "") or "").split("\n")[0].strip()

    # Parameter descriptions extracted from docstring
    doc_params: dict[str, tuple[str, str]] = {}
    for sec_key in ("Parameters", "Args", "Arguments"):
        if sec_key in sections:
            doc_params = _parse_param_section(sections[sec_key])
            break

    # If class doc had no Parameters section, try __init__ doc separately
    if not doc_params and init_doc and init_doc != primary_doc:
        init_secs = _parse_numpy_sections(init_doc)
        for sec_key in ("Parameters", "Args", "Arguments"):
            if sec_key in init_secs:
                doc_params = _parse_param_section(init_secs[sec_key])
                break

    # ── __init__ signature → parameters ──────────────────────────────────────
    try:
        sig = inspect.signature(klass.__init__)
    except (ValueError, TypeError):
        sig = None

    if sig:
        for pname, param in sig.parameters.items():
            if pname in ("self", "target_device_idx", "precision"):
                continue

            ann_type = _type_name(
                param.annotation
                if param.annotation is not inspect.Parameter.empty
                else None
            )

            if param.default is inspect.Parameter.empty:
                default_str = "REQUIRED"
            elif param.default is None:
                default_str = "null"
            else:
                default_str = str(param.default)

            doc_type, doc_desc = doc_params.get(pname, ("", ""))
            result["parameters"][pname] = {
                "type":    doc_type or ann_type,
                "default": default_str,
                "desc":    doc_desc,
            }

    # ── input_names() → inputs ────────────────────────────────────────────────
    if hasattr(klass, "input_names") and callable(klass.input_names):
        try:
            for iname, idesc in (klass.input_names() or {}).items():
                result["inputs"][iname] = {
                    "type": _type_name(getattr(idesc, "type", None)),
                    "desc": getattr(idesc, "desc", "") or "",
                }
        except Exception:
            pass

    # ── output_names() → outputs ──────────────────────────────────────────────
    if hasattr(klass, "output_names") and callable(klass.output_names):
        try:
            for oname, odesc in (klass.output_names() or {}).items():
                result["outputs"][oname] = {
                    "type": _type_name(getattr(odesc, "type", None)),
                    "desc": getattr(odesc, "desc", "") or "",
                }
        except Exception:
            pass

    return result


# ── Tooltip helpers (one-liners for use in the property panel) ────────────────

def get_param_tooltip(class_name: str, param_name: str) -> str:
    """Short tooltip text for *param_name*.  Empty string if nothing found."""
    info  = get_class_help(class_name)
    if info.get("error"):
        return ""
    param = info["parameters"].get(param_name)
    if not param:
        return ""
    parts = []
    if param["type"]:
        parts.append(f"type: {param['type']}")
    if param["default"] not in ("", "REQUIRED"):
        parts.append(f"default: {param['default']}")
    if param["desc"]:
        parts.append(param["desc"].split("\n")[0])
    return "  |  ".join(parts)


def get_input_tooltip(class_name: str, input_name: str) -> str:
    info = get_class_help(class_name)
    if info.get("error"):
        return ""
    inp = info["inputs"].get(input_name)
    if not inp:
        return ""
    parts = []
    if inp["type"]:
        parts.append(f"type: {inp['type']}")
    if inp["desc"]:
        parts.append(inp["desc"].split("\n")[0])
    return "  |  ".join(parts)


def get_output_tooltip(class_name: str, output_name: str) -> str:
    info = get_class_help(class_name)
    if info.get("error"):
        return ""
    out = info["outputs"].get(output_name)
    if not out:
        return ""
    parts = []
    if out["type"]:
        parts.append(f"type: {out['type']}")
    if out["desc"]:
        parts.append(out["desc"].split("\n")[0])
    return "  |  ".join(parts)


def get_class_tooltip(class_name: str) -> str:
    """Short tooltip for the class name itself."""
    info = get_class_help(class_name)
    if info.get("error"):
        return f"{class_name} (SPECULA class — no doc available)"
    summary = info.get("summary", "")
    cat     = info.get("category", "")
    parts   = [class_name]
    if cat and cat != "unknown":
        parts.append(cat.replace("_", " "))
    if summary:
        parts.append(summary)
    return "  |  ".join(parts)