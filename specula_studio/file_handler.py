"""
File I/O glue between the editor UI and the pure :mod:`core.yaml_io` layer.

Loading parses the YAML into the graph model (no UI involved) and then asks
the NodeManager to render that model; saving exports the model.
"""

import logging
import os

import dearpygui.dearpygui as dpg

from .core import overrides, yaml_io
from .dpg_utils import auto_layout_nodes, place_new_nodes

log = logging.getLogger(__name__)


class FileHandler:
    def __init__(self, node_manager):
        self.nm     = node_manager
        self.editor = None  # Set by SpeculaEditor after construction

    # ── The override files of a simulation ───────────────────────────────────
    #
    # Listed in comment lines at the end of the saved file (SPECULA reads every
    # top-level key as an object), with paths relative to the file.  Files
    # saved by older versions have them under the top-level key
    # yaml_io.STUDIO_META_KEY instead: still read.

    def _overrides_comment(self, file_path) -> str:
        if self.editor is None or not hasattr(self.editor, 'override_manager'):
            return ""
        folder = os.path.dirname(os.path.abspath(file_path))
        entries = self.editor.override_manager.to_dict(folder)["overrides"]
        return overrides.overrides_comment(entries, os.path.basename(file_path))

    def _load_overrides_metadata(self, yaml_data: dict, file_path) -> None:
        folder = os.path.dirname(os.path.abspath(file_path))
        meta = yaml_data.pop(yaml_io.STUDIO_META_KEY, None)
        if meta is None:
            try:
                with open(file_path, encoding="utf-8") as f:
                    meta = {"overrides": overrides.parse_overrides_comment(f.read())}
            except OSError:
                return
        if self.editor is not None and meta.get("overrides"):
            self.editor.override_manager.from_dict(meta, folder)

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load_from_yaml_data(self, yaml_data: dict, operation_name: str = "LOAD"):
        """Replace the current graph with *yaml_data* and render it."""
        graph = self.nm.graph
        graph.clear()
        yaml_io.load_into(graph, yaml_data)
        unplaced = [u for u, nd in graph.nodes.items() if "gui_pos" not in nd]
        self.nm.sync_ui_from_graph()

        if graph.nodes and len(unplaced) == len(graph.nodes):
            # Plain SPECULA files carry no gui_pos: lay the graph out once the
            # nodes have been drawn (their sizes are only known after a frame).
            dpg.set_frame_callback(
                dpg.get_frame_count() + 3,
                (lambda: self.editor.auto_layout()) if self.editor is not None
                else (lambda: auto_layout_nodes(graph, self.nm.uuid_to_dpg)),
            )
        elif unplaced and operation_name == "LOAD":
            # Some objects without a place, e.g. the DataSource of SPECULA's
            # replay_params.yml (the other objects keep their gui_pos): next
            # to the objects they are linked to.  (Overrides place their own.)
            spacing = (int(self.editor.preferences["layout_spacing"])
                       if self.editor is not None else 60)
            dpg.set_frame_callback(
                dpg.get_frame_count() + 3,
                lambda: place_new_nodes(graph, self.nm.uuid_to_dpg, unplaced, spacing))
        log.info("[%s] %d objects, %d connections",
                 operation_name, len(graph.nodes), len(graph.connections))

    def load_simulation(self, file_path):
        """Load a simulation from a YAML file."""
        yaml_data = yaml_io.read_yaml_file(file_path)
        if yaml_data is None:
            return False
        self._load_overrides_metadata(yaml_data, file_path)
        self._load_from_yaml_data(yaml_data, operation_name="LOAD")
        log.info("Simulation loaded from %s", file_path)
        return True

    def load_from_yaml_dict(self, yaml_data: dict):
        """Load from an in-memory dict (used by the override mechanism)."""
        data = dict(yaml_data)
        data.pop(yaml_io.STUDIO_META_KEY, None)
        self._load_from_yaml_data(data, operation_name="OVERRIDE-RELOAD")

    # ── Saving ────────────────────────────────────────────────────────────────

    def capture_positions(self):
        """Copy the current node positions from the canvas into the model."""
        for node_uuid, dpg_id in self.nm.uuid_to_dpg.items():
            node_data = self.nm.graph.nodes.get(node_uuid)
            if node_data is not None and dpg.does_item_exist(dpg_id):
                node_data['gui_pos'] = list(dpg.get_item_pos(dpg_id))

    def save_simulation(self, file_path, include_defaults=False):
        """Capture current positions then export to a YAML file."""
        self.capture_positions()
        self.export_simulation(file_path, include_defaults=include_defaults)
        log.info("Simulation saved to %s", file_path)

    def export_simulation(self, file_path, include_defaults=False, yaml_data=None):
        """Write the graph (or *yaml_data*) as SPECULA-compatible YAML, with
        the list of its override files in comment lines."""
        if yaml_data is None:
            yaml_data = self.export_to_yaml_dict(include_defaults=include_defaults)
        text = yaml_io.dump_yaml(yaml_data) + self._overrides_comment(file_path)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text)

    def export_to_yaml_string(self, include_defaults=False) -> str:
        return yaml_io.dump_yaml(self.export_to_yaml_dict(include_defaults=include_defaults))

    def export_to_yaml_dict(self, include_defaults=False) -> dict:
        """Export the current graph to an in-memory OrderedDict."""
        return yaml_io.export_graph(self.nm.graph, include_defaults=include_defaults)
