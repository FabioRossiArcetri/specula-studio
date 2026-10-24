"""Bundled class templates (``specula_yaml_docs``) — loading only, no UI."""

from __future__ import annotations

import os
import pathlib
from collections import OrderedDict

from .yaml_io import ordered_load

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "specula_yaml_docs"


def load_template_dir(folder) -> OrderedDict:
    """Merge every ``*.yml`` in *folder* (sorted for determinism)."""
    templates = OrderedDict()
    if os.path.isdir(folder):
        for file in sorted(os.listdir(folder)):
            if file.endswith(".yml"):
                with open(os.path.join(folder, file), encoding="utf-8") as f:
                    data = ordered_load(f)
                if data:
                    templates.update(data)
    return templates


def load_bundled_templates(root=TEMPLATES_DIR) -> tuple[OrderedDict, OrderedDict]:
    """Return ``(data_object_templates, processing_object_templates)``."""
    root = pathlib.Path(root)
    return (load_template_dir(root / "data_objects"),
            load_template_dir(root / "processing_objects"))
