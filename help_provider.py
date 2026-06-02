"""
help_provider.py
================
Extract and format documentation from the live SPECULA package.

Sources (in priority order):
1. Class-level __doc__  (NumPy / Google-style, both understood)
2. __init__ __doc__
3. input_names() / output_names() classmethods  →  InputDesc/OutputDesc .desc
4. inspect.signature  →  type annotations and defaults

For parameters whose description is empty in the immediate class, the full
MRO (Method Resolution Order) is walked upward until a non-empty description
(and/or type) is found for that parameter name.  This means a parameter
documented only in a base class is automatically surfaced for all subclasses.

Two parameters are special-cased with fixed standard descriptions that are
applied unconditionally to every class:

  target_device_idx – "-1 for CPU, 0/1/… for GPU index; None = use default"
  precision         – "0 = double, 1 = single; None = use global precision"

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

# ── Standard parameters present in every BaseProcessingObj subclass ───────────
# These descriptions are applied as an unconditional fallback after the MRO
# walk so they always appear even when the subclass docstring omits them.
_STANDARD_PARAMS: dict = {
    "target_device_idx": {
        "type":    "int",
        "default": "null",
        "desc": (
            "Target device index for computation.  "
            "Pass -1 for CPU, 0 for the first GPU, 1 for the second GPU, etc.  "
            "None uses the global default device."
        ),
    },
    "precision": {
        "type":    "int",
        "default": "null",
        "desc": (
            "Numerical precision.  "
            "Pass 0 for double precision, 1 for single precision.  "
            "None uses the global precision setting."
        ),
    },
}

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


def _parse_numpy_sections(docstring: str) -> dict:
    """Split a NumPy/Google docstring into {section_title: body_text}."""
    if not docstring:
        return {}
    text = _dedent(docstring)
    hits = list(_SECTION_HEADER_RE.finditer(text))
    if not hits:
        return {"Summary": text}

    out: dict = {}
    preamble = text[: hits[0].start()].strip()
    if preamble:
        out["Summary"] = preamble

    for i, m in enumerate(hits):
        title = m.group("title").strip()
        body_start = m.end()
        body_end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out[title] = text[body_start:body_end].strip()

    return out


def _parse_param_section(body: str) -> dict:
    """Parse a 'Parameters' section body → {name: (type_str, desc_str)}."""
    result: dict = {}
    for m in _PARAM_ENTRY_RE.finditer((body or "") + "\n"):
        pname = m.group("pname").strip()
        ptype = (m.group("ptype") or "").strip()
        # Strip [unit] and 'optional' annotations from type string for brevity
        ptype_clean = re.sub(r"\s*\[.*?\]", "", ptype).replace(", optional", "").strip()
        pdesc = _dedent(m.group("pdesc") or "")
        result[pname] = (ptype_clean, pdesc)
    return result


def _extract_doc_params(klass) -> dict:
    """
    Return {param_name: (type_str, desc_str)} parsed from the docstrings of
    *klass* alone (class-level doc first, then __init__ doc).  Does NOT walk
    the MRO — that is done by the caller.
    """
    class_doc = inspect.getdoc(klass) or ""
    init_doc  = (
        inspect.getdoc(klass.__init__)
        if klass.__init__ is not object.__init__
        else ""
    ) or ""

    primary_doc = class_doc or init_doc
    sections    = _parse_numpy_sections(primary_doc)

    doc_params: dict = {}
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

    return doc_params


def _type_name(obj) -> str:
    """Return a short readable type name from a class or annotation."""
    if obj is None or obj is inspect.Parameter.empty:
        return ""
    if hasattr(obj, "__name__"):
        return obj.__name__
    s = str(obj)
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


def _fill_from_mro(klass, params: dict) -> None:
    """
    For every parameter in *params* whose ``desc`` (and optionally ``type``)
    is empty, walk up ``klass.__mro__`` (skipping the class itself and
    ``object``) and try to fill the gap from an ancestor's docstring.

    *params* is mutated in-place.

    Walk strategy
    -------------
    For each ancestor (in MRO order, so most-specific first):
      1. Parse the ancestor's own docstrings (class + __init__) for a
         Parameters section.
      2. If the ancestor's __init__ signature contains the parameter, also
         pick up the type annotation as a fallback type.
      3. Stop walking for a given parameter as soon as both ``type`` and
         ``desc`` are non-empty.
    """
    needs_desc = {
        pname
        for pname, pmeta in params.items()
        if not pmeta.get("desc", "").strip()
    }
    needs_type = {
        pname
        for pname, pmeta in params.items()
        if not pmeta.get("type", "").strip()
    }

    if not needs_desc and not needs_type:
        return

    # Walk ancestors (skip [0] == klass itself, skip object at the end)
    for ancestor in klass.__mro__[1:]:
        if ancestor is object:
            continue
        if not needs_desc and not needs_type:
            break

        # ── Parse ancestor docstring ──────────────────────────────────────────
        anc_doc_params = _extract_doc_params(ancestor)

        # ── Parse ancestor __init__ signature for type annotations ────────────
        anc_sig_types: dict = {}
        try:
            if ancestor.__init__ is not object.__init__:
                sig = inspect.signature(ancestor.__init__)
                for pname, param in sig.parameters.items():
                    if pname == "self":
                        continue
                    if param.annotation is not inspect.Parameter.empty:
                        anc_sig_types[pname] = _type_name(param.annotation)
        except (ValueError, TypeError):
            pass

        # ── Fill gaps ─────────────────────────────────────────────────────────
        filled_desc = set()
        filled_type = set()

        for pname in list(needs_desc):
            if pname in anc_doc_params:
                anc_type, anc_desc = anc_doc_params[pname]
                if anc_desc.strip():
                    params[pname]["desc"] = anc_desc
                    filled_desc.add(pname)
                # Opportunistically fill type too if empty
                if pname in needs_type and anc_type.strip():
                    params[pname]["type"] = anc_type
                    filled_type.add(pname)

        for pname in list(needs_type):
            if pname in filled_type:
                continue
            if pname in anc_sig_types and anc_sig_types[pname]:
                params[pname]["type"] = anc_sig_types[pname]
                filled_type.add(pname)
            elif pname in anc_doc_params and anc_doc_params[pname][0].strip():
                params[pname]["type"] = anc_doc_params[pname][0]
                filled_type.add(pname)

        needs_desc -= filled_desc
        needs_type -= filled_type


def _apply_standard_params(params: dict) -> None:
    """
    Unconditionally ensure ``target_device_idx`` and ``precision`` appear in
    *params* with their canonical descriptions.

    Rules
    -----
    * If the parameter is already present (picked up from the signature),
      only the ``desc`` and ``type`` fields are overwritten — the ``default``
      value that was read from the actual signature is preserved.
    * If the parameter is absent entirely (e.g. a data-object class that does
      not expose these arguments), it is inserted with the standard default
      of ``"null"``.

    *params* is mutated in-place.
    """
    for pname, standard in _STANDARD_PARAMS.items():
        if pname in params:
            # Keep the signature default; replace desc and type
            params[pname]["desc"] = standard["desc"]
            params[pname]["type"] = standard["type"]
        else:
            # Insert the full entry (class does not declare this param)
            params[pname] = dict(standard)  # shallow copy


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
    class_doc   = inspect.getdoc(klass) or ""
    init_doc    = (
        inspect.getdoc(klass.__init__)
        if klass.__init__ is not object.__init__
        else ""
    ) or ""

    primary_doc = class_doc or init_doc
    sections    = _parse_numpy_sections(primary_doc)

    result["full_doc"] = primary_doc
    result["summary"]  = (sections.get("Summary", "") or "").split("\n")[0].strip()

    # Parameter descriptions extracted from the immediate class's docstring
    doc_params: dict = _extract_doc_params(klass)

    # ── __init__ signature → parameters ──────────────────────────────────────
    # NOTE: target_device_idx and precision are NO LONGER excluded here so
    # that their actual default values are read from the real signature.
    # Their descriptions are overwritten by _apply_standard_params() below.
    try:
        sig = inspect.signature(klass.__init__)
    except (ValueError, TypeError):
        sig = None

    if sig:
        for pname, param in sig.parameters.items():
            if pname == "self":
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

    # ── MRO walk: fill empty desc / type from ancestors ───────────────────────
    if result["parameters"]:
        _fill_from_mro(klass, result["parameters"])

    # ── Standard params: unconditional canonical descriptions ─────────────────
    # Applied AFTER the MRO walk so they always win for these two names.
    _apply_standard_params(result["parameters"])

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

@lru_cache(maxsize=512)
def is_input_optional(class_name: str, input_name: str) -> bool:
    """
    Return True if *input_name* is optional for *class_name*.

    Strategy (in order of reliability):
    1. Instantiate the class with dummy arguments and read the .optional
       attribute directly from the InputValue/InputList object in self.inputs.
       This is the ground truth — it reads the exact flag set in __init__.
    2. If instantiation fails (requires non-trivial args), fall back to
       parsing the desc string returned by input_names(): if it contains
       the word "optional" the input is considered optional.
    3. If input_names() is not defined or the name is absent, default to
       False (treat as required — conservative / safer).
    """
    klass = _find_class(class_name)
    if klass is None:
        return False

    # ── Strategy 1: try a no-arg (or None-arg) instantiation ─────────────────
    # Many SPECULA processing objects accept all-None __init__ args.
    try:
        sig     = inspect.signature(klass.__init__)
        kwargs  = {
            p: None
            for p in sig.parameters
            if p != "self"
        }
        obj = klass(**kwargs)
        inputs = getattr(obj, "inputs", {})
        if input_name in inputs:
            inp = inputs[input_name]
            return bool(getattr(inp, "optional", False))
    except Exception:
        pass  # fall through to strategy 2

    # ── Strategy 2: parse desc string from input_names() ─────────────────────
    if hasattr(klass, "input_names") and callable(klass.input_names):
        try:
            inames = klass.input_names() or {}
            if input_name in inames:
                desc = getattr(inames[input_name], "desc", "") or ""
                # Convention: optional inputs say "(optional)" in the desc
                return "optional" in desc.lower()
        except Exception:
            pass

    # ── Strategy 3: conservative default ─────────────────────────────────────
    return False