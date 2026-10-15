"""
Load every base fixture through the UI-coupled loader (NodeManager +
FileHandler, driven by tests/fakedpg.py) and print the exports as JSON.

Run as a subprocess so the fake ``dearpygui`` never leaks into other tests:
    python -m tests.helpers.ui_export <fixture> [<fixture> ...]
"""

import os
import tempfile

# Never touch the developer's real settings, even when run by hand.
os.environ.setdefault(
    "SPECULA_STUDIO_SETTINGS",
    os.path.join(tempfile.mkdtemp(prefix="specula_studio_test_"), "settings.json"),
)
os.environ.setdefault("SPECULA_STUDIO_CACHE", tempfile.mkdtemp(prefix="specula_studio_cache_"))

import json
import sys

from tests import fakedpg

fakedpg.install()

from specula_studio import socketio_client  # noqa: E402

socketio_client.SocketIOClient._connect = lambda self: None

from specula_studio.core.templates import load_bundled_templates  # noqa: E402
from specula_studio.file_handler import FileHandler  # noqa: E402
from specula_studio.graph_manager import GraphManager  # noqa: E402
from specula_studio.node_manager import NodeManager  # noqa: E402


def _plain(obj):
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def export(path):
    data_tpl, proc_tpl = load_bundled_templates()
    templates = {**data_tpl, **proc_tpl}
    graph = GraphManager(templates)
    nm = NodeManager(graph, templates, debug=False)
    nm.init_themes()
    fh = FileHandler(nm)
    fh.load_simulation(path)
    result = {}
    for include_defaults, key in ((False, "minimal"), (True, "defaults")):
        data = _plain(fh.export_to_yaml_dict(include_defaults=include_defaults))
        for node in data.values():
            node.pop("gui_pos", None)
        result[key] = data
    return result


if __name__ == "__main__":
    out = {p: export(p) for p in sys.argv[1:]}
    sys.stdout.write("@@JSON@@" + json.dumps(out))
