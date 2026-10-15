"""
File I/O glue between the editor UI and the pure :mod:`core.yaml_io` layer.

Loading parses the YAML into the graph model (no UI involved) and then asks
the NodeManager to render that model; saving exports the model.
"""

import logging

import dearpygui.dearpygui as dpg

from .core import yaml_io
from .dpg_utils import auto_layout_nodes

log = logging.getLogger(__name__)


class FileHandler:
    def __init__(self, node_manager):
        self.nm     = node_manager
        self.editor = None  # Set by SpeculaEditor after construction

    # ── Override metadata helpers ─────────────────────────────────────────────

    def _add_overrides_metadata(self, yaml_data: dict):
        if self.editor is not None and hasattr(self.editor, 'override_manager'):
            meta = self.editor.override_manager.to_dict()
            if meta.get('overrides'):
                yaml_data[yaml_io.STUDIO_META_KEY] = meta
        return yaml_data

    def _load_overrides_metadata(self, yaml_data: dict):
        if yaml_io.STUDIO_META_KEY in yaml_data:
            meta = yaml_data.pop(yaml_io.STUDIO_META_KEY)
            if self.editor is not None:
                self.editor.override_manager.from_dict(meta)

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load_from_yaml_data(self, yaml_data: dict, operation_name: str = "LOAD"):
        """Replace the current graph with *yaml_data* and render it."""
        graph = self.nm.graph
        graph.clear()
        yaml_io.load_into(graph, yaml_data, self.nm.all_templates)
        has_positions = any("gui_pos" in nd for nd in graph.nodes.values())
        self.nm.sync_ui_from_graph()

        if not has_positions and graph.nodes:
            # Plain SPECULA files carry no gui_pos: lay the graph out once the
            # nodes have been drawn (their sizes are only known after a frame).
            dpg.set_frame_callback(
                dpg.get_frame_count() + 3,
                lambda: auto_layout_nodes(graph, self.nm.uuid_to_dpg),
            )
        log.info("[%s] %d objects, %d connections",
                 operation_name, len(graph.nodes), len(graph.connections))

    def load_simulation(self, file_path):
        """Load a simulation from a YAML file."""
        yaml_data = yaml_io.read_yaml_file(file_path)
        if yaml_data is None:
            return False
        self._load_overrides_metadata(yaml_data)
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
        """Write the graph (or *yaml_data*) as SPECULA-compatible YAML."""
        if yaml_data is None:
            text = self.export_to_yaml_string(include_defaults=include_defaults,
                                              include_override_metadata=True)
        else:
            text = yaml_io.dump_yaml(yaml_data)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text)

    def export_to_yaml_string(self, include_defaults=False,
                              include_override_metadata=False) -> str:
        return yaml_io.dump_yaml(self.export_to_yaml_dict(
            include_defaults=include_defaults,
            include_override_metadata=include_override_metadata))

    def export_to_yaml_dict(self, include_defaults=False,
                            include_override_metadata=False) -> dict:
        """Export the current graph to an in-memory OrderedDict."""
        export_data = yaml_io.export_graph(self.nm.graph, include_defaults=include_defaults)
        if include_override_metadata:
            export_data = self._add_overrides_metadata(export_data)
        return export_data
