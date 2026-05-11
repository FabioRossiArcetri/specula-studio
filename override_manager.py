"""
OverrideManager — specula-studio

Manages SPECULA override YAML files.  Each override is an additional YAML
file that is composed on top of the base simulation description using the
same rule-set as specula.simul.Simul.combine_params():

  * A top-level key ``name_override`` (suffix _override) → dict-update the
    existing object called ``name``.
  * A top-level key ``remove`` → list of object names to delete.
  * Any other top-level key → add the object to the simulation dict.

Snapshot logic
--------------
The first time any override is enabled the manager asks the editor to take
an in-memory snapshot of the current simulation (a plain Python dict via
FileHandler.export_to_yaml_dict).  The snapshot is kept frozen until all
overrides are disabled, at which point it is cleared.

Every toggle (enable or disable) causes a full re-derivation:
  base_snapshot  →  apply_all_enabled_overrides  →  reload graph
"""

import copy
import yaml
from collections import OrderedDict
from pathlib import Path


# ── SPECULA combine_params logic ──────────────────────────────────────────────

def _combine_params(base: dict, additional: dict) -> dict:
    """
    Merge *additional* onto *base* following SPECULA's combine_params rules:

    - ``name_override`` keys   → update the existing ``name`` object (shallow).
    - ``remove`` key           → list of object names to remove from base.
    - Any other key            → add the object (overwrite if already present,
                                 so studio is more permissive than the CLI).
    """
    result = OrderedDict(base)

    for name, values in additional.items():
        if name == 'remove':
            remove_list = values if isinstance(values, list) else [values]
            for objname in remove_list:
                result.pop(objname, None)

        elif name.endswith('_override'):
            objname = name[:-9]          # strip trailing '_override'
            if objname in result and isinstance(result[objname], dict) \
                    and isinstance(values, dict):
                merged = OrderedDict(result[objname])
                merged.update(values)
                result[objname] = merged
            else:
                # Object doesn't exist yet — treat as an add
                result[objname] = values

        else:
            # Add or overwrite object
            result[name] = values

    return result


# ── OverrideManager ───────────────────────────────────────────────────────────

class OverrideManager:
    """
    Lifecycle of overrides
    ----------------------
    load_overrides(paths)      → parse files, add to ordered registry (enabled=True)
    remove_override(path)      → remove from registry
    toggle_override(path)      → flip enabled flag; caller must call apply()
    apply_overrides(base)      → return merged dict for all enabled overrides
    """

    def __init__(self):
        # OrderedDict: abs-path-str → {'enabled': bool, 'data': dict}
        self._overrides: OrderedDict = OrderedDict()
        # Frozen snapshot of the simulation BEFORE any override was applied.
        # None until the first override becomes enabled.
        self._base_snapshot: dict | None = None

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def set_base_snapshot(self, snapshot: dict):
        """Store a deep-copy of the pre-override simulation dict."""
        self._base_snapshot = copy.deepcopy(snapshot)
        print("[OVERRIDE_MGR] Base snapshot stored "
              f"({len(snapshot)} top-level keys)")

    def get_base_snapshot(self) -> dict | None:
        """Return a deep-copy of the stored snapshot, or None."""
        if self._base_snapshot is None:
            return None
        return copy.deepcopy(self._base_snapshot)

    def clear_base_snapshot(self):
        self._base_snapshot = None
        print("[OVERRIDE_MGR] Base snapshot cleared")

    def has_base_snapshot(self) -> bool:
        return self._base_snapshot is not None

    # ── File management ───────────────────────────────────────────────────────

    def load_overrides(self, paths) -> int:
        """
        Parse and register one or more override YAML files.
        Skips paths that are already registered.
        Returns the number of newly loaded files.
        """
        loaded = 0
        for path in paths:
            path = str(Path(path).resolve())
            if path in self._overrides:
                print(f"[OVERRIDE_MGR] Already loaded: {Path(path).name}")
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    print(f"[OVERRIDE_MGR] Skipping {path}: "
                          f"root is not a mapping")
                    continue
                self._overrides[path] = {'enabled': True, 'data': data}
                print(f"[OVERRIDE_MGR] Loaded: {Path(path).name}")
                loaded += 1
            except Exception as e:
                print(f"[OVERRIDE_MGR] Error loading {path}: {e}")
        return loaded

    def remove_override(self, path: str):
        """Remove an override from the registry (regardless of enabled state)."""
        path = str(Path(path).resolve())
        if path in self._overrides:
            del self._overrides[path]
            print(f"[OVERRIDE_MGR] Removed: {Path(path).name}")

    def toggle_override(self, path: str) -> bool:
        """
        Flip the enabled flag of the given override.
        Returns the *new* enabled state, or False if path not found.
        """
        path = str(Path(path).resolve())
        if path not in self._overrides:
            return False
        new_state = not self._overrides[path]['enabled']
        self._overrides[path]['enabled'] = new_state
        verb = "Enabled" if new_state else "Disabled"
        print(f"[OVERRIDE_MGR] {verb}: {Path(path).name}")
        return new_state

    def enable_override(self, path: str):
        path = str(Path(path).resolve())
        if path in self._overrides:
            self._overrides[path]['enabled'] = True

    def disable_override(self, path: str):
        path = str(Path(path).resolve())
        if path in self._overrides:
            self._overrides[path]['enabled'] = False

    def is_enabled(self, path: str) -> bool:
        path = str(Path(path).resolve())
        return self._overrides.get(path, {}).get('enabled', False)

    def get_all_overrides(self) -> list:
        """All registered override paths in load order."""
        return list(self._overrides.keys())

    def get_enabled_overrides(self) -> list:
        """Enabled override paths in load order."""
        return [p for p, v in self._overrides.items() if v['enabled']]

    def any_enabled(self) -> bool:
        return any(v['enabled'] for v in self._overrides.values())

    # ── Application ───────────────────────────────────────────────────────────

    def apply_overrides(self, base: dict) -> dict:
        """
        Deep-merge all *enabled* overrides (in load order) onto *base*.
        *base* is not modified; a new OrderedDict is returned.
        """
        result = copy.deepcopy(base)
        for path, entry in self._overrides.items():
            if entry['enabled']:
                result = _combine_params(result, entry['data'])
                print(f"[OVERRIDE_MGR] Applied: {Path(path).name}")
        return result

    # ── CLI helper ────────────────────────────────────────────────────────────

    def get_override_string(self) -> str:
        """Space-separated list of enabled paths for the SPECULA CLI."""
        return " ".join(self.get_enabled_overrides())

    # ── Persistence ───────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise state for embedding in a saved simulation YAML."""
        return {
            'overrides': [
                {'path': path, 'enabled': entry['enabled']}
                for path, entry in self._overrides.items()
            ]
        }

    def from_dict(self, data: dict):
        """Restore state from a loaded simulation YAML."""
        for item in data.get('overrides', []):
            path = item.get('path')
            enabled = item.get('enabled', True)
            if not path:
                continue
            path = str(Path(path).resolve())
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    yaml_data = yaml.safe_load(f)
                if isinstance(yaml_data, dict):
                    self._overrides[path] = {
                        'enabled': enabled,
                        'data': yaml_data,
                    }
                    print(f"[OVERRIDE_MGR] Restored: {Path(path).name} "
                          f"({'on' if enabled else 'off'})")
            except Exception as e:
                print(f"[OVERRIDE_MGR] Could not restore {path}: {e}")