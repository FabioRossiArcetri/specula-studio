"""
SPECULA override files ("additional parameter files").

Each override is an extra YAML file composed on top of the base simulation
with the rules of ``specula.simul.Simul.combine_params``:

* ``<name>_override: {...}``  → shallow-update object ``name``;
* ``remove: [names]``         → delete objects (and inputs fed by them);
* any other key               → add a new object;
* a trailing ``_<N>`` on any key (``dm_override_2``, ``remove_3``) applies it
  only to simulation index ``N`` (``--nsimul`` runs).

Preview in the editor
---------------------
The first time an override is enabled, the current simulation is stored as
the *base snapshot*; the graph is then reloaded from
``base + enabled overrides``.  Edits made while overrides are active are
folded back into the base with :func:`rebase_edits` when saving, so the
saved file never contains values that came from an override.

In the saved file
-----------------
The override files of a simulation are listed in comment lines at its end
(:func:`overrides_comment`), so that the file stays a plain SPECULA
parameter file: SPECULA reads every top-level key as an object.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml

from .syntax import output_owner

log = logging.getLogger(__name__)

_SIMUL_IDX_RE = re.compile(r"^(.*)_(\d+)$")


def remove_inputs(params: dict, obj_to_remove: str) -> dict:
    """Drop every input fed by *obj_to_remove* (as SPECULA does on ``remove``)."""
    for obj in params.values():
        if not isinstance(obj, dict) or not isinstance(obj.get("inputs"), dict):
            continue
        new_inputs: OrderedDict[str, Any] = OrderedDict()
        for input_name, source in obj["inputs"].items():
            if isinstance(source, str):
                if output_owner(source) == obj_to_remove:
                    continue
                new_inputs[input_name] = source
            elif isinstance(source, list):
                new_inputs[input_name] = [s for s in source if output_owner(s) != obj_to_remove]
            else:
                new_inputs[input_name] = source
        obj["inputs"] = new_inputs
    return params


def combine_params(base: dict, additional: dict, simul_idx: int = 0,
                   strict: bool = False) -> OrderedDict:
    """
    Return *base* merged with *additional* (neither argument is modified).

    With ``strict=False`` (editor preview) problems that make SPECULA raise
    — removing/overriding a missing object, adding an existing one — are
    logged and the editor stays usable; ``strict=True`` raises like SPECULA.
    """
    result = copy.deepcopy(OrderedDict(base))

    def problem(msg):
        if strict:
            raise ValueError(msg)
        log.warning("Override: %s", msg)

    for name, values in additional.items():
        match = _SIMUL_IDX_RE.match(str(name))
        if match:
            if int(match.group(2)) != simul_idx:
                continue
            name = match.group(1)

        if name == "remove":
            for objname in values if isinstance(values, list) else [values]:
                if objname not in result:
                    problem(f"cannot remove '{objname}': no such object")
                    continue
                del result[objname]
                remove_inputs(result, objname)

        elif name.endswith("_override"):
            objname = name[: -len("_override")]
            if objname not in result or not isinstance(result[objname], dict):
                problem(f"cannot override '{objname}': no such object")
                if not strict and isinstance(values, dict) and "class" in values:
                    result[objname] = copy.deepcopy(values)
                continue
            merged = OrderedDict(result[objname])
            merged.update(copy.deepcopy(values))
            result[objname] = merged

        else:
            if name in result:
                problem(f"object '{name}' already exists (replaced)")
            result[name] = copy.deepcopy(values)

    return result


def rebase_edits(base: dict, merged: dict, current: dict) -> OrderedDict:
    """
    Fold the edits made on an override preview back into the base.

    *merged* is what the editor loaded (base + overrides), *current* is the
    graph after the user's edits.  Every difference between *merged* and
    *current* is applied to *base*:

    * objects added by the user are added;
    * objects deleted by the user are removed;
    * changed / added / removed keys of an object are applied key by key.

    Edits to objects that exist only because an override adds them cannot be
    represented in the base and are dropped (with a warning).
    """
    new_base = copy.deepcopy(OrderedDict(base))

    for name, obj in current.items():
        if name not in merged:
            new_base[name] = copy.deepcopy(obj)

    for name in merged:
        if name not in current:
            new_base.pop(name, None)

    for name, obj in current.items():
        old = merged.get(name)
        if old is None or obj == old:
            continue
        if name not in new_base or not isinstance(new_base[name], dict):
            if _without_position(obj) != _without_position(old):     # not just moved
                log.warning("Edits to '%s' were not saved: the object only exists "
                            "because an override adds it", name)
            continue
        target = new_base[name]
        for key, val in obj.items():
            if key not in old or old[key] != val:
                target[key] = copy.deepcopy(val)
        for key in old:
            if key not in obj:
                target.pop(key, None)

    return new_base


def _without_position(obj) -> Any:
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k != "gui_pos"}
    return obj


# ── the override list in a saved simulation ───────────────────────────────────

_COMMENT_TAG = "#studio-override:"
_COMMENT_RE = re.compile(r"^#studio-override:\s*(on|off)\s+(.+?)\s*$")


def overrides_comment(entries, sim_name: str = "") -> str:
    """Comment lines listing *entries* (``[{path, enabled}]``) at the end of a
    saved simulation; :func:`parse_overrides_comment` reads them back."""
    if not entries:
        return ""
    files = " ".join(e["path"] for e in entries if e["enabled"])
    lines = ["", "# Override files loaded in SPECULA Studio (SPECULA does not read these",
             "# lines: give the active ones after this file, e.g.",
             f"#   specula {sim_name or 'this_file.yml'} {files or '<override files>'})"]
    lines += [f"{_COMMENT_TAG} {'on' if e['enabled'] else 'off'} {e['path']}" for e in entries]
    return "\n".join(lines) + "\n"


def parse_overrides_comment(text: str) -> list[dict]:
    """The ``[{path, enabled}]`` written by :func:`overrides_comment`."""
    found = []
    for line in text.splitlines():
        m = _COMMENT_RE.match(line.strip())
        if m:
            found.append({"path": m.group(2), "enabled": m.group(1) == "on"})
    return found


# ── OverrideManager ───────────────────────────────────────────────────────────

class OverrideManager:
    """
    Registry of override files and the snapshots used for the preview.

    load_overrides(paths)   → parse files, register them (disabled)
    toggle_override(path)   → flip the enabled flag
    apply_overrides(base)   → base merged with every enabled override
    """

    def __init__(self):
        # abs-path → {'enabled': bool, 'data': dict}, in load order
        self._overrides: OrderedDict = OrderedDict()
        # Simulation BEFORE any override was applied (None when inactive)
        self._base_snapshot: dict | None = None
        # What the editor loaded the last time overrides were applied
        self._merged_snapshot: dict | None = None
        # Where the objects that only overrides add were on the canvas (they
        # are not in the base): kept while the simulation is open
        self._added_positions: dict = {}

    # ── snapshots ─────────────────────────────────────────────────────────────

    def set_base_snapshot(self, snapshot: dict):
        self._base_snapshot = copy.deepcopy(snapshot)
        log.info("Override base snapshot stored (%d objects)", len(snapshot))

    def get_base_snapshot(self) -> dict | None:
        return copy.deepcopy(self._base_snapshot) if self._base_snapshot is not None else None

    def clear_base_snapshot(self):
        self._base_snapshot = None
        self._merged_snapshot = None

    def has_base_snapshot(self) -> bool:
        return self._base_snapshot is not None

    def set_merged_snapshot(self, merged: dict):
        self._merged_snapshot = copy.deepcopy(merged)

    def rebased(self, current: dict) -> dict:
        """
        The base updated with the edits made since the overrides were applied
        (``current`` is the editor's export).  Without an active preview the
        current graph *is* the base.
        """
        if self._base_snapshot is None or self._merged_snapshot is None:
            return copy.deepcopy(current)
        return rebase_edits(self._base_snapshot, self._merged_snapshot, current)

    def commit_edits(self, current: dict) -> dict:
        """Fold *current*'s edits into the base snapshot and return it."""
        new_base = self.rebased(current)
        if self._base_snapshot is not None:
            for name, obj in current.items():
                if name not in new_base and isinstance(obj, dict) and "gui_pos" in obj:
                    self._added_positions[name] = list(obj["gui_pos"])
            self._base_snapshot = copy.deepcopy(new_base)
            self._merged_snapshot = copy.deepcopy(current)
        return new_base

    def added_objects(self, merged: dict) -> list[str]:
        """Objects of *merged* that only the overrides add (not in the base)."""
        base = self._base_snapshot or {}
        return [n for n, obj in merged.items() if n not in base and isinstance(obj, dict)]

    def place_added(self, merged: dict) -> list[str]:
        """Give the added objects of *merged* the places they had the last
        time; returns those that have none yet (to be placed on the canvas)."""
        unplaced = []
        for name in self.added_objects(merged):
            pos = self._added_positions.get(name)
            if pos is not None:
                merged[name]["gui_pos"] = list(pos)
            elif "gui_pos" not in merged[name]:
                unplaced.append(name)
        return unplaced

    # ── file management ───────────────────────────────────────────────────────

    def load_overrides(self, paths) -> int:
        """Parse and register override files (disabled).  Returns how many."""
        loaded = 0
        for path in paths:
            path = str(Path(path).resolve())
            if path in self._overrides:
                log.info("Override already loaded: %s", Path(path).name)
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except Exception as exc:
                log.warning("Error loading override %s: %s", path, exc)
                continue
            if not isinstance(data, dict):
                log.warning("Skipping override %s: root is not a mapping", path)
                continue
            self._overrides[path] = {"enabled": False, "data": data}
            log.info("Loaded override (disabled): %s", Path(path).name)
            loaded += 1
        return loaded

    def remove_override(self, path: str):
        self._overrides.pop(str(Path(path).resolve()), None)

    def toggle_override(self, path: str) -> bool:
        path = str(Path(path).resolve())
        if path not in self._overrides:
            return False
        entry = self._overrides[path]
        entry["enabled"] = not entry["enabled"]
        log.info("%s override %s", "Enabled" if entry["enabled"] else "Disabled",
                 Path(path).name)
        return entry["enabled"]

    def is_enabled(self, path: str) -> bool:
        return self._overrides.get(str(Path(path).resolve()), {}).get("enabled", False)

    def get_all_overrides(self) -> list:
        return list(self._overrides.keys())

    def get_enabled_overrides(self) -> list:
        return [p for p, v in self._overrides.items() if v["enabled"]]

    def any_enabled(self) -> bool:
        return any(v["enabled"] for v in self._overrides.values())

    # ── application ───────────────────────────────────────────────────────────

    def apply_overrides(self, base: dict, simul_idx: int = 0) -> OrderedDict:
        """*base* merged with every enabled override, in load order."""
        result = copy.deepcopy(OrderedDict(base))
        for path, entry in self._overrides.items():
            if entry["enabled"]:
                result = combine_params(result, entry["data"], simul_idx=simul_idx)
                log.info("Applied override %s", Path(path).name)
        return result

    # ── persistence ───────────────────────────────────────────────────────────

    def to_dict(self, base_dir: str | None = None) -> dict:
        """The override list, for a simulation saved in *base_dir*: paths
        relative to it when possible (the folder can then be moved)."""
        def shown(path):
            if base_dir:
                try:
                    return os.path.relpath(path, base_dir).replace("\\", "/")
                except ValueError:          # another drive
                    pass
            return path
        return {"overrides": [{"path": shown(p), "enabled": e["enabled"]}
                              for p, e in self._overrides.items()]}

    def from_dict(self, data: dict, base_dir: str | None = None):
        """Restore the override list of a simulation loaded from *base_dir*
        (relative paths are relative to it)."""
        for item in data.get("overrides", []):
            path = item.get("path")
            if not path:
                continue
            if base_dir and not os.path.isabs(path):
                path = os.path.join(base_dir, path)
            path = str(Path(path).resolve())
            try:
                with open(path, encoding="utf-8") as f:
                    yaml_data = yaml.safe_load(f)
            except Exception as exc:
                log.warning("Could not restore override %s: %s", path, exc)
                continue
            if isinstance(yaml_data, dict):
                self._overrides[path] = {"enabled": item.get("enabled", True),
                                         "data": yaml_data}
                log.info("Restored override %s (%s)", Path(path).name,
                         "on" if item.get("enabled", True) else "off")

