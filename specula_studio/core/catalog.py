"""
Class catalog: the metadata (parameters, inputs, outputs, docs) of every
SPECULA class the editor can place.

Sources, in order of preference:

1. **Installed SPECULA** — generated from the installed package's sources
   (AST) and classes (``input_names()`` / ``output_names()``, docstrings), and
   cached per installation (the cache key changes whenever any SPECULA source
   file changes).  Generation imports SPECULA, so it runs in a *subprocess*
   (``python -m specula_studio.core.catalog``) and never initialises a GPU or
   pollutes the editor process.
2. **Bundled templates** — ``specula_yaml_docs/``, shipped with the editor;
   used when SPECULA is not installed, while the catalog is being generated,
   or when the user selects it in the preferences.

The catalog format is the template format the editor always used::

    {ClassName: {"class_name", "bases", "parameters", "inputs", "outputs",
                 "output_names", "is_data_obj", "help"?}}

Each input entry gets an ``optional`` flag (SPECULA marks optional inputs
with "(optional)" in their description).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import logging
import os
import pathlib
import subprocess
import sys
import tempfile
from collections import OrderedDict

from platformdirs import user_cache_dir

from .syntax import REQUIRED
from .templates import TEMPLATES_DIR, load_bundled_templates

log = logging.getLogger(__name__)

CATALOG_FORMAT = 3


# ── locating SPECULA ──────────────────────────────────────────────────────────

def specula_root() -> pathlib.Path | None:
    """Directory of the installed ``specula`` package (without importing it)."""
    try:
        spec = importlib.util.find_spec("specula")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return pathlib.Path(list(spec.submodule_search_locations)[0])


def _source_files(root: pathlib.Path) -> list[pathlib.Path]:
    files = []
    for sub in ("processing_objects", "data_objects", "display"):
        files.extend(sorted((root / sub).rglob("*.py")))
    files.extend(sorted(root.glob("*.py")))
    return files


def catalog_key(root: pathlib.Path) -> str:
    """Fingerprint of an installation: path + size/mtime of every source."""
    h = hashlib.sha1(f"{CATALOG_FORMAT}:{root.resolve()}".encode())
    for f in _source_files(root):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.relative_to(root)}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def cache_dir() -> pathlib.Path:
    override = os.environ.get("SPECULA_STUDIO_CACHE")
    return pathlib.Path(override) if override else pathlib.Path(user_cache_dir("specula-studio"))


def cache_path(root: pathlib.Path) -> pathlib.Path:
    return cache_dir() / f"catalog-{catalog_key(root)}.json"


# ── building ──────────────────────────────────────────────────────────────────

def _input_optional(meta: dict) -> bool:
    return "optional" in str(meta.get("desc", "")).lower()


def annotate_optional_inputs(templates: dict) -> dict:
    """Add ``optional`` to every input entry that lacks it (in place)."""
    for tpl in templates.values():
        for meta in (tpl.get("inputs") or {}).values():
            if isinstance(meta, dict) and "optional" not in meta:
                meta["optional"] = _input_optional(meta)
    return templates


# Constructor arguments that SPECULA's Simul supplies itself (simul.py, "Add
# global and class-specific params"): data_dir is the class's folder under
# root_dir (a value in the YAML still wins), so it is never required; the
# others never come from a parameter file.
SIMUL_SUPPLIED_DEFAULTS = {"data_dir": None}
SIMUL_INTERNAL_ARGS = ("params_dict", "input_ref_getter", "output_ref_getter", "info_getter")


def apply_simul_supplied(templates: dict) -> dict:
    """Constructor arguments filled by Simul: not required, or not shown (in place)."""
    for tpl in templates.values():
        params = tpl.get("parameters") or {}
        for name in SIMUL_INTERNAL_ARGS:
            params.pop(name, None)
        for name, default in SIMUL_SUPPLIED_DEFAULTS.items():
            meta = params.get(name)
            if isinstance(meta, dict) and meta.get("default") == REQUIRED:
                meta["default"] = default
    return templates


def build_catalog(root: pathlib.Path, runtime: bool = True,
                  with_help: bool = True) -> tuple[OrderedDict, OrderedDict]:
    """
    Generate ``(data_object_templates, processing_object_templates)`` from
    the SPECULA package at *root*.  With *runtime* the package is imported
    to read ``input_names()`` / ``output_names()`` (do this in a subprocess).
    """
    from .catalog_parser import SpeculaMetadataParser

    parser = SpeculaMetadataParser()
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("template_"):      # SPECULA's example class
            continue
        try:
            parser.visit(ast.parse(py_file.read_text(encoding="utf-8")))
        except Exception as exc:
            log.debug("Skipping %s: %s", py_file, exc)
    parser.resolve_inheritance()
    if runtime:
        try:
            parser.enrich_from_runtime()
        except Exception as exc:
            log.warning("Runtime enrichment skipped: %s", exc)

    referenced = set()
    for info in parser.found_classes.values():
        for meta in info.get("parameters", {}).values():
            cand = str(meta.get("type", "")).split(".")[-1]
            if cand in parser.found_classes:
                referenced.add(cand)

    data: OrderedDict = OrderedDict()
    proc: OrderedDict = OrderedDict()
    for name in sorted(parser.found_classes):
        info = dict(parser.found_classes[name])
        category = info.pop("category", "other")
        if category not in ("data_objects", "processing_objects"):
            continue
        if name.startswith("Base") and not (info.get("inputs") or info.get("outputs")) \
                and name not in referenced:
            continue
        info["is_data_obj"] = category == "data_objects"
        (data if info["is_data_obj"] else proc)[name] = info

    if with_help and runtime:
        try:
            from .. import help_provider
            for name, tpl in list(data.items()) + list(proc.items()):
                try:
                    tpl["help"] = help_provider.get_class_help(name)
                except Exception as exc:
                    log.debug("No help for %s: %s", name, exc)
        except Exception as exc:
            log.debug("Help extraction skipped: %s", exc)

    annotate_optional_inputs(data)
    annotate_optional_inputs(proc)
    return data, proc


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and obj != obj:      # NaN
        return None
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def write_cache(path: pathlib.Path, data: dict, proc: dict, root: pathlib.Path) -> None:
    payload = {"format": CATALOG_FORMAT, "specula_root": str(root),
               "data_objects": _jsonable(data), "processing_objects": _jsonable(proc)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, allow_nan=True)
    os.replace(tmp, path)


def read_cache(path: pathlib.Path) -> tuple[OrderedDict, OrderedDict] | None:
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f, object_pairs_hook=OrderedDict)
    except (OSError, ValueError):
        return None
    if payload.get("format") != CATALOG_FORMAT:
        return None
    return payload["data_objects"], payload["processing_objects"]


# ── public API used by the app ────────────────────────────────────────────────

class CatalogInfo:
    """Where the active catalog came from (shown in the Help menu)."""

    def __init__(self, source: str, detail: str = ""):
        self.source = source      # "installed" | "bundled"
        self.detail = detail

    def __str__(self):
        return f"{self.source} ({self.detail})" if self.detail else self.source


def load_catalog(prefer_installed: bool = True):
    """
    Return ``(data_templates, proc_templates, info, pending_root)``.

    *pending_root* is the SPECULA root whose catalog still needs generating
    (call :func:`start_build`), or None.
    """
    root = specula_root() if prefer_installed else None
    if root is not None:
        cached = read_cache(cache_path(root))
        if cached is not None:
            data, proc = cached
            apply_simul_supplied(data)
            apply_simul_supplied(proc)
            return data, proc, CatalogInfo("installed", str(root)), None
    data, proc = load_bundled_templates(TEMPLATES_DIR)
    for templates in (data, proc):
        annotate_optional_inputs(templates)
        apply_simul_supplied(templates)
    return data, proc, CatalogInfo("bundled", str(TEMPLATES_DIR)), root


def start_build(root: pathlib.Path) -> subprocess.Popen:
    """Generate the catalog for *root* in a background subprocess."""
    pkg_parent = str(pathlib.Path(__file__).resolve().parents[2])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_parent, env.get("PYTHONPATH")) if p)
    log.info("Generating the class catalog of the installed SPECULA (%s)…", root)
    return subprocess.Popen(
        [sys.executable, "-m", "specula_studio.core.catalog", "--build", str(root)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the SPECULA class catalog cache")
    parser.add_argument("--build", metavar="SPECULA_ROOT", help="package directory")
    parser.add_argument("--no-runtime", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    root = pathlib.Path(args.build) if args.build else specula_root()
    if root is None:
        print("SPECULA is not installed", file=sys.stderr)
        return 1
    data, proc = build_catalog(root, runtime=not args.no_runtime)
    write_cache(cache_path(root), data, proc, root)
    print(cache_path(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
