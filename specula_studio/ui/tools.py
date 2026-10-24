"""
Productivity tools: issues panel, add-object search, object finder,
compare-with-saved-file and diagram export.

Mixin of :class:`specula_studio.app.SpeculaEditor`.
"""

import logging
import os
import shutil
import subprocess

import dearpygui.dearpygui as dpg

from ..core import diagram, validation, yaml_io

log = logging.getLogger(__name__)

try:
    from ..help_provider import is_input_optional as _is_input_optional
except ImportError:  # pragma: no cover
    def _is_input_optional(class_name, input_name):
        return False


class ToolsMixin:
    # ── focusing a node ───────────────────────────────────────────────────────

    def focus_node(self, node_uuid: str) -> None:
        """Show the node's properties and flash its border (no pan API in DPG)."""
        if node_uuid not in self.graph.nodes:
            return
        self.nm._last_selected_uuid = node_uuid
        self.nm._show_property_panel(node_uuid)
        self.nm.flash_node(node_uuid)

    # ── issues panel ──────────────────────────────────────────────────────────

    def _show_issues_window(self, *_):
        tag = "issues_window"
        if not dpg.does_item_exist(tag):
            with dpg.window(label="Issues", tag=tag, width=760, height=360):
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Refresh", callback=lambda: self._refresh_issues())
                    dpg.add_text("", tag="issues_summary")
                dpg.add_text("Click an object name to show it.",
                             color=self.theme_manager.get_color("text_hint"))
                dpg.add_child_window(tag="issues_list", border=False)
        dpg.show_item(tag)
        dpg.focus_item(tag)
        self._refresh_issues()

    def _refresh_issues(self):
        if not dpg.does_item_exist("issues_list"):
            return
        issues = validation.graph_issues(self.graph, _is_input_optional)
        n_err = sum(i.severity == validation.ERROR for i in issues)
        dpg.set_value("issues_summary",
                      f"{n_err} error(s), {len(issues) - n_err} warning(s)" if issues
                      else "No issues")
        dpg.delete_item("issues_list", children_only=True)
        tm = self.theme_manager
        for issue in sorted(issues, key=lambda i: (i.severity != validation.ERROR,
                                                   self._issue_name(i))):
            color = tm.get_color("text_error" if issue.severity == validation.ERROR
                                 else "monitor_status_waiting")
            with dpg.group(horizontal=True, parent="issues_list"):
                dpg.add_text("●", color=color)
                name = self._issue_name(issue)
                if issue.node_uuid:
                    dpg.add_button(label=name or "?", small=True, user_data=issue.node_uuid,
                                   callback=lambda s, a, u: self.focus_node(u))
                if issue.field:
                    dpg.add_text(issue.field, color=tm.get_color("panel_label_ref"))
                dpg.add_text(issue.message)

    def _issue_name(self, issue) -> str:
        return self.graph.nodes.get(issue.node_uuid, {}).get("name", "") if issue.node_uuid else ""

    # ── searchable lists (add object / find object) ───────────────────────────

    def _search_window(self, tag, title, items_fn, on_pick, hint):
        """Generic filter-as-you-type list; Enter or double-click picks."""
        for old in (tag, f"{tag}_keys"):
            if dpg.does_item_exist(old):
                dpg.delete_item(old)

        def matches():
            text = dpg.get_value(f"{tag}_filter").strip().lower()
            return [it for it in items_fn() if text in it.lower()]

        def refresh(*_):
            items = matches()
            dpg.configure_item(f"{tag}_list", items=items)
            if items:
                dpg.set_value(f"{tag}_list", items[0])

        def pick(*_):
            value = dpg.get_value(f"{tag}_list")
            items = matches()
            if value not in items:
                value = items[0] if items else None
            if value:
                dpg.delete_item(tag)
                on_pick(value)

        with dpg.window(label=title, tag=tag, width=420, height=440, modal=False):
            dpg.add_input_text(tag=f"{tag}_filter", hint=hint, width=-1,
                               callback=refresh, on_enter=False)
            dpg.add_listbox(tag=f"{tag}_list", items=[], num_items=16, width=-1)
            with dpg.group(horizontal=True):
                dpg.add_button(label="OK", width=80, callback=pick)
                dpg.add_button(label="Cancel", width=80, callback=lambda: dpg.delete_item(tag))
            with dpg.item_handler_registry() as reg:
                dpg.add_item_double_clicked_handler(callback=pick)
            dpg.bind_item_handler_registry(f"{tag}_list", reg)
        with dpg.handler_registry(tag=f"{tag}_keys"):
            dpg.add_key_press_handler(dpg.mvKey_Return,
                                      callback=lambda: dpg.does_item_exist(tag) and pick())
        self._center_dialog(tag)
        refresh()
        dpg.focus_item(f"{tag}_filter")

    def _show_add_object_search(self, *_):
        def items():
            return ([f"{n}  [processing]" for n in sorted(self.proc_obj_templates)]
                    + [f"{n}  [data]" for n in sorted(self.data_obj_templates)])

        def pick(value):
            self._on_menu_create(None, None, value.split()[0])

        self._search_window("add_object_search", "Add Object", items, pick,
                            "type to filter, Enter to add")

    def _show_find_object(self, *_):
        def items():
            return sorted(f"{nd.get('name')}  ({nd.get('type')})"
                          for nd in self.graph.nodes.values())

        def pick(value):
            uuid = self.graph.find_by_name(value.split()[0])
            if uuid:
                self.focus_node(uuid)

        self._search_window("find_object_search", "Find Object", items, pick,
                            "type to filter, Enter to show")

    # ── compare with the saved file ───────────────────────────────────────────

    def _show_diff_with_saved(self, *_):
        path = self.current_simulation_path
        if not path or not os.path.exists(path):
            log.info("The simulation has not been saved to a file yet")
            return
        saved_raw = yaml_io.read_yaml_file(path) or {}
        saved = yaml_io.export_graph(yaml_io.load_graph(saved_raw, self.all_templates))
        current = self._current_export()
        if self.override_manager.has_base_snapshot():
            current = self.override_manager.rebased(current)
        diff = yaml_io.semantic_diff(saved, current, os.path.basename(path), "editor")
        tag = "diff_window"
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)
        with dpg.window(label="Changes vs saved file", tag=tag, width=820, height=560):
            dpg.add_input_text(default_value=diff or "No differences.", multiline=True,
                               readonly=True, width=-1, height=-1)

    # ── diagram export ────────────────────────────────────────────────────────

    def _show_export_diagram_dialog(self, *_):
        if not dpg.does_item_exist("export_diagram_dialog"):
            with dpg.file_dialog(label="Export Diagram (Graphviz)", show=False,
                                 tag="export_diagram_dialog", width=700, height=400,
                                 callback=lambda s, a: self._export_diagram(a["file_path_name"])):
                dpg.add_file_extension(".dot")
        dpg.show_item("export_diagram_dialog")

    def _export_diagram(self, path: str) -> None:
        if not path.endswith(".dot"):
            path += ".dot"
        with open(path, "w", encoding="utf-8") as f:
            f.write(diagram.to_dot(self.graph, self.current_simulation_name or ""))
        log.info("Diagram written to %s", path)
        dot = shutil.which("dot")
        if dot:
            svg = path[:-4] + ".svg"
            result = subprocess.run([dot, "-Tsvg", path, "-o", svg],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                log.info("Rendered %s", svg)
            else:
                log.warning("Graphviz failed: %s", result.stderr.strip())
