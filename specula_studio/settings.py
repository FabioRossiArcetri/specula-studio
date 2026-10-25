"""
Persistent user settings for SPECULA Studio.

A single JSON file in the platform's user-config directory replaces the two
legacy locations (``~/.specula_studio_settings.json`` and ``user_prefs.json``
inside the package directory).  Values from the legacy files are migrated the
first time the new file is created.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile

from platformdirs import user_config_dir

log = logging.getLogger(__name__)


APP_NAME = "specula-studio"
MAX_RECENT_FILES = 10

LEGACY_SETTINGS_PATH = pathlib.Path.home() / ".specula_studio_settings.json"
# Older versions wrote the theme next to their own source file: the checkout
# root before the package layout, the package directory afterwards.
_PKG_DIR = pathlib.Path(__file__).resolve().parent
LEGACY_THEME_PATHS = [_PKG_DIR / "user_prefs.json", _PKG_DIR.parent / "user_prefs.json"]

DEFAULTS: dict = {
    "auto_simul_params": True,
    "include_defaults": False,
    "render_size": "MEDIUM",
    "layout_spacing": 60,       # pixels between nodes in the automatic layouts
    "font_path": None,          # None → matplotlib's DejaVuSerif
    "recent_files": [],
    "theme": "dark",
    "log_level": "INFO",
    "class_source": "installed",  # "installed" (SPECULA catalog) | "bundled"
    "remote_hosts": [],         # [{"host": ..., "user": ...}], most recent first
    "hpc": {},                  # HPC batch profile (hosts, paths, SLURM options)
    "hpc_jobs": [],             # submitted batch jobs (see hpc.HpcJob)
}


def default_settings_path() -> pathlib.Path:
    override = os.environ.get("SPECULA_STUDIO_SETTINGS")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(user_config_dir(APP_NAME)) / "settings.json"


class Settings:
    """Dictionary-like settings store backed by one JSON file."""

    def __init__(self, path: pathlib.Path | None = None, migrate: bool = True):
        self.path = pathlib.Path(path) if path else default_settings_path()
        self.data: dict = dict(DEFAULTS)
        self.data["recent_files"] = []
        self.data["remote_hosts"] = []
        self.data["hpc"] = {}
        self.data["hpc_jobs"] = []
        self.load(migrate=migrate)

    # ── mapping protocol ──────────────────────────────────────────────────────
    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __contains__(self, key):
        return key in self.data

    def get(self, key, default=None):
        return self.data.get(key, default)

    # ── persistence ───────────────────────────────────────────────────────────
    def load(self, migrate: bool = True) -> None:
        if self.path.exists():
            self._merge_file(self.path)
        elif migrate:
            migrated = False
            if LEGACY_SETTINGS_PATH.exists():
                migrated |= self._merge_file(LEGACY_SETTINGS_PATH)
            for theme_path in LEGACY_THEME_PATHS:
                if theme_path.exists():
                    migrated |= self._merge_file(theme_path, keys=("theme",))
            if migrated:
                log.info("Migrated legacy settings to %s", self.path)
                self.save()

    def _merge_file(self, path: pathlib.Path, keys=None) -> bool:
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
        except Exception as exc:
            log.warning("Could not read settings file %s: %s", path, exc)
            return False
        if not isinstance(saved, dict):
            return False
        for key, value in saved.items():
            if keys is not None and key not in keys:
                continue
            if key in DEFAULTS:
                self.data[key] = value
        return True

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            log.warning("Could not save settings to %s: %s", self.path, exc)

    # ── helpers ───────────────────────────────────────────────────────────────
    def add_recent_file(self, file_path: str) -> list[str]:
        abs_path = str(pathlib.Path(file_path).resolve())
        recent = [p for p in self.data.get("recent_files", []) if p != abs_path]
        recent.insert(0, abs_path)
        self.data["recent_files"] = recent[:MAX_RECENT_FILES]
        self.save()
        return self.data["recent_files"]

    def remove_recent_file(self, file_path: str) -> None:
        self.data["recent_files"] = [
            p for p in self.data.get("recent_files", []) if p != file_path
        ]
        self.save()

    def add_remote_host(self, host: str, user: str = "") -> None:
        if not host:
            return
        entry = {"host": host, "user": user or ""}
        hosts = [h for h in self.data.get("remote_hosts", []) if h != entry]
        hosts.insert(0, entry)
        self.data["remote_hosts"] = hosts[:10]
        self.save()


_store: Settings | None = None


def store() -> Settings:
    """Process-wide settings singleton (created on first use)."""
    global _store
    if _store is None:
        _store = Settings()
    return _store


def set_store(settings: Settings) -> None:
    """Replace the singleton (used by tests)."""
    global _store
    _store = settings
