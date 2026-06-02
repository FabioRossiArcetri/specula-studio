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
(and/or type) is found for that parameter name.

Unit strings (e.g. "nm", "arcsec", "pixels") are parsed from the bracketed
annotation in the docstring type field, e.g.::

    wavelengthInNm : float [nm]
        Wavelength in nanometres.

The unit is stored as a ``"unit"`` key in each parameter dict and is shown
in the property panel beside the input widget.  Dimensionless ``[1]``
annotations are normalised to an empty string.

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
_STANDARD_PARAMS: dict = {
    "target_device_idx": {
        "type":    "int",
        "default": "null",
        "unit":    "",
        "desc": (
            "Target device index for computation.  "
            "Pass -1 for CPU, 0 for the first GPU, 1 for the second GPU, etc.  "
            "None uses the global default device."
        ),
    },
    "precision": {
        "type":    "int",
        "default": "null",
        "unit":    "",
        "desc": (
            "Numerical precision.  "
            "Pass 0 for double precision, 1 for single precision.  "
            "None uses the global precision setting."
        ),
    },
}

# NumPy-style section header
_SECTION_HEADER_RE = re.compile(
    r"^[ \t]*(?P<title>[A-Za-z][A-Za-z0-9 _]*?)\s*\n[ \t]*[-=]{3,}\s*$",
    re.MULTILINE,
)

# One parameter entry inside a Parameters section
_PARAM_ENTRY_RE = re.compile(
    r"^(?P<pname>\w+)"
    r"(?:\s*:\s*(?P<ptype>[^\n]+?))?"
    r"\s*\n"
    r"(?P<pdesc>(?:[ \t]+[^\n]*\n?)*)",
    re.MULTILINE,
)

# Matches the bracketed unit annotation, e.g.  [nm]  [arcsec/pixel]  [1]
_UNIT_RE = re.compile(r"\[([^\]]+)\]")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dedent(text: str) -> str:
    return textwrap.dedent(text or "").strip()


def _extract_unit(raw_type: str) -> tuple[str, str]:
    """
    Split a raw type annotation into (clean_type, unit).

    Examples
    --------
    "float [nm]"          →  ("float", "nm")
    "int [pixels]"        →  ("int", "pixels")
    "float [arcsec/pix]"  →  ("float", "arcsec/pix")
    "int [1]"             →  ("int", "")      ← dimensionless suppressed
    "bool"                →  ("bool", "")
    """
    m = _UNIT_RE.search(raw_type)
    if not m:
        clean = raw_type.replace(", optional", "").strip()
        return clean, ""

    unit = m.group(1).strip()
    # Suppress dimensionless marker
    if unit == "1":
        unit = ""

    clean = _UNIT_RE.sub("", raw_type).replace(", optional", "").strip()
    return clean, unit


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
        title      = m.group("title").strip()
        body_start = m.end()
        body_end   = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out[title] = text[body_start:body_end].strip()

    return out


def _parse_param_section(body: str) -> dict:
    """
    Parse a 'Parameters' section body.

    Returns
    -------
    dict : name → (type_str, desc_str, unit_str)
        *type_str* has the ``[unit]`` bracket removed.
        *unit_str* is the content of the bracket, or ``""`` if absent /
        dimensionless (``[1]``).
    """
    result: dict = {}
    for m in _PARAM_ENTRY_RE.finditer((body or "") + "\n"):
        pname        = m.group("pname").strip()
        raw_type     = (m.group("ptype") or "").strip()
        pdesc        = _dedent(m.group("pdesc") or "")
        clean, unit  = _extract_unit(raw_type)
        result[pname] = (clean, pdesc, unit)
    return result


def _extract_doc_params(klass) -> dict:
    """
    Return {param_name: (type_str, desc_str, unit_str)} parsed from the
    docstrings of *klass* alone.  Does NOT walk the MRO.
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

    if not doc_params and init_doc and init_doc != primary_doc:
        init_secs = _parse_numpy_sections(init_doc)
        for sec_key in ("Parameters", "Args", "Arguments"):
            if sec_key in init_secs:
                doc_params = _parse_param_section(init_secs[sec_key])
                break

    return doc_params


def _type_name(obj) -> str:
    if obj is None or obj is inspect.Parameter.empty:
        return ""
    if hasattr(obj, "__name__"):
        return obj.__name__
    s = str(obj)
    s = re.sub(r"\btyping\.", "", s)
    return s


@lru_cache(maxsize=256)
def _find_class(class_name: str):
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
    Walk ``klass.__mro__`` to fill empty ``desc``, ``type``, and ``unit``
    fields in *params*.  Mutates *params* in-place.
    """
    needs_desc = {p for p, m in params.items() if not m.get("desc", "").strip()}
    needs_type = {p for p, m in params.items() if not m.get("type", "").strip()}
    needs_unit = {p for p, m in params.items() if not m.get("unit", "").strip()}

    if not needs_desc and not needs_type and not needs_unit:
        return

    for ancestor in klass.__mro__[1:]:
        if ancestor is object:
            continue
        if not needs_desc and not needs_type and not needs_unit:
            break

        anc_doc_params = _extract_doc_params(ancestor)

        # Ancestor signature for type fallback
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

        filled_desc = set()
        filled_type = set()
        filled_unit = set()

        for pname in list(needs_desc):
            if pname in anc_doc_params:
                anc_type, anc_desc, anc_unit = anc_doc_params[pname]
                if anc_desc.strip():
                    params[pname]["desc"] = anc_desc
                    filled_desc.add(pname)
                if pname in needs_type and anc_type.strip():
                    params[pname]["type"] = anc_type
                    filled_type.add(pname)
                if pname in needs_unit and anc_unit.strip():
                    params[pname]["unit"] = anc_unit
                    filled_unit.add(pname)

        for pname in list(needs_type):
            if pname in filled_type:
                continue
            if pname in anc_sig_types and anc_sig_types[pname]:
                params[pname]["type"] = anc_sig_types[pname]
                filled_type.add(pname)
            elif pname in anc_doc_params and anc_doc_params[pname][0].strip():
                params[pname]["type"] = anc_doc_params[pname][0]
                filled_type.add(pname)

        for pname in list(needs_unit):
            if pname in filled_unit:
                continue
            if pname in anc_doc_params and anc_doc_params[pname][2].strip():
                params[pname]["unit"] = anc_doc_params[pname][2]
                filled_unit.add(pname)

        needs_desc -= filled_desc
        needs_type -= filled_type
        needs_unit -= filled_unit


def _apply_standard_params(params: dict) -> None:
    """
    Unconditionally ensure ``target_device_idx`` and ``precision`` appear in
    *params* with their canonical descriptions.  The ``default`` actually read
    from the signature is preserved; only ``desc``, ``type``, and ``unit``
    are overwritten.
    """
    for pname, standard in _STANDARD_PARAMS.items():
        if pname in params:
            params[pname]["desc"] = standard["desc"]
            params[pname]["type"] = standard["type"]
            params[pname]["unit"] = standard["unit"]
        else:
            params[pname] = dict(standard)


# ── Public API ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def get_class_help(class_name: str) -> dict:
    """
    Return a structured help dictionary for *class_name*.

    Keys
    ----
    class_name : str
    summary    : str
    full_doc   : str
    category   : str
    parameters : dict  name → {"type": str, "default": str, "unit": str, "desc": str}
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

    # Category
    mod = klass.__module__ or ""
    if "processing_objects" in mod:
        result["category"] = "processing_objects"
    elif "data_objects" in mod:
        result["category"] = "data_objects"

    # Docstrings
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

    doc_params: dict = _extract_doc_params(klass)

    # __init__ signature → parameters
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

            # _extract_doc_params now returns 3-tuples
            doc_type, doc_desc, doc_unit = doc_params.get(pname, ("", "", ""))
            result["parameters"][pname] = {
                "type":    doc_type or ann_type,
                "default": default_str,
                "unit":    doc_unit,
                "desc":    doc_desc,
            }

    # MRO walk
    if result["parameters"]:
        _fill_from_mro(klass, result["parameters"])

    # Standard params (target_device_idx, precision)
    _apply_standard_params(result["parameters"])

    # input_names()
    if hasattr(klass, "input_names") and callable(klass.input_names):
        try:
            for iname, idesc in (klass.input_names() or {}).items():
                result["inputs"][iname] = {
                    "type": _type_name(getattr(idesc, "type", None)),
                    "desc": getattr(idesc, "desc", "") or "",
                }
        except Exception:
            pass

    # output_names()
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


# ── Tooltip helpers ───────────────────────────────────────────────────────────

def get_param_tooltip(class_name: str, param_name: str) -> str:
    """Short tooltip text for *param_name*.  Includes unit when available."""
    info  = get_class_help(class_name)
    if info.get("error"):
        return ""
    param = info["parameters"].get(param_name)
    if not param:
        return ""
    parts = []
    if param["type"]:
        type_str = param["type"]
        if param.get("unit"):
            type_str += f" [{param['unit']}]"
        parts.append(f"type: {type_str}")
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


def get_param_unit(class_name: str, param_name: str) -> str:
    """
    Return the unit string for *param_name*, e.g. ``"nm"``, ``"arcsec"``.
    Returns ``""`` when no unit is available or it is dimensionless.
    """
    info = get_class_help(class_name)
    if info.get("error"):
        return ""
    param = info["parameters"].get(param_name)
    if not param:
        return ""
    return param.get("unit", "")


@lru_cache(maxsize=512)
def is_input_optional(class_name: str, input_name: str) -> bool:
    """
    Return True if *input_name* is optional for *class_name*.

    Strategy (in order of reliability):
    1. Instantiate the class with all-None kwargs and read .optional directly.
    2. Parse desc string from input_names() for the word "optional".
    3. Default False (conservative).
    """
    klass = _find_class(class_name)
    if klass is None:
        return False

    # Strategy 1
    try:
        sig    = inspect.signature(klass.__init__)
        kwargs = {p: None for p in sig.parameters if p != "self"}
        obj    = klass(**kwargs)
        inputs = getattr(obj, "inputs", {})
        if input_name in inputs:
            return bool(getattr(inputs[input_name], "optional", False))
    except Exception:
        pass

    # Strategy 2
    if hasattr(klass, "input_names") and callable(klass.input_names):
        try:
            inames = klass.input_names() or {}
            if input_name in inames:
                desc = getattr(inames[input_name], "desc", "") or ""
                return "optional" in desc.lower()
        except Exception:
            pass

    return False
