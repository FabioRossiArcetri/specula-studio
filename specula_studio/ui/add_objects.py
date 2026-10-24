"""Adding nodes: menu entries, the multi-add dialog, auto SimulParams wiring.

Mixin of :class:`specula_studio.app.SpeculaEditor`; it relies on the editor's
attributes (``nm``, ``fh``, ``preferences``, ``theme_manager``, ...).
"""

import logging

import dearpygui.dearpygui as dpg



log = logging.getLogger(__name__)


class AddObjectsMixin:
    def _mo_on_double_click(self, sender, app_data):
        clicked_id   = app_data[1]
        parent_id    = dpg.get_item_info(clicked_id)["parent"]
        alias        = dpg.get_item_alias(clicked_id)
        parent_alias = dpg.get_item_alias(parent_id)
        if alias == "_mo_proc_listbox" or parent_alias == "_mo_proc_listbox":
            self._mo_add_proc()
        elif alias == "_mo_data_listbox" or parent_alias == "_mo_data_listbox":
            self._mo_add_data()

    def _setup_add_multiple_dialog(self):
        self._multi_add_queue = []
        proc_types = sorted(self.proc_obj_templates.keys())
        data_types = sorted(self.data_obj_templates.keys())
        COL_W = 260

        if not dpg.does_item_exist("mo_double_click_handler"):
            with dpg.item_handler_registry(tag="mo_double_click_handler"):
                dpg.add_item_double_clicked_handler(callback=self._mo_on_double_click)

        with dpg.window(
            label="Add Multiple Objects", tag="add_multiple_dialog",
            modal=True, show=False, width=1000, height=550,
            no_resize=True, on_close=self._on_add_multiple_close,
        ):
            dpg.add_text(
                "Select items from the lists, use the arrows to stage them, then click Confirm.",
                color=self.theme_manager.get_color("text_instruction"),
            )
            dpg.add_separator()
            dpg.add_spacer(height=4)
            with dpg.group(horizontal=True):
                with dpg.group(width=COL_W):
                    dpg.add_text("Processing Objects")
                    dpg.add_listbox(items=proc_types, tag="_mo_proc_listbox", num_items=16, width=COL_W)
                    dpg.bind_item_handler_registry("_mo_proc_listbox", "mo_double_click_handler")
                dpg.add_spacer(width=8)
                with dpg.group(width=COL_W):
                    dpg.add_text("Data Objects")
                    dpg.add_listbox(items=data_types, tag="_mo_data_listbox", num_items=16, width=COL_W)
                    dpg.bind_item_handler_registry("_mo_data_listbox", "mo_double_click_handler")
                dpg.add_spacer(width=8)
                with dpg.group(width=70):
                    dpg.add_spacer(height=90)
                    dpg.add_button(label="Add Proc →", width=70, callback=self._mo_add_proc)
                    dpg.add_spacer(height=12)
                    dpg.add_button(label="Add Data →", width=70, callback=self._mo_add_data)
                    dpg.add_spacer(height=12)
                    dpg.add_button(label="← Remove",  width=70, callback=self._mo_remove)
                dpg.add_spacer(width=8)
                with dpg.group(width=COL_W):
                    dpg.add_text(
                        "Staged to Add",
                        color=self.theme_manager.get_color("text_success")
                    )
                    dpg.add_listbox(items=[], tag="_mo_staged_listbox", num_items=16, width=COL_W)
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Confirm", tag="_mo_confirm_btn", width=160, callback=self._mo_confirm)
                dpg.add_spacer(width=8)
                dpg.add_button(label="Cancel", width=100, callback=self._mo_cancel)
                dpg.add_spacer(width=20)
                dpg.add_text("", tag="_mo_status_text", color=self.theme_manager.get_color("text_hint"))

    def _show_add_multiple_dialog(self):
        self._multi_add_queue.clear()
        self._mo_refresh_staged()
        self._center_dialog("add_multiple_dialog")
        dpg.show_item("add_multiple_dialog")

    def _on_add_multiple_close(self):
        self._multi_add_queue.clear()

    def _mo_refresh_staged(self):
        dpg.configure_item("_mo_staged_listbox", items=list(self._multi_add_queue))
        count = len(self._multi_add_queue)
        dpg.configure_item("_mo_confirm_btn", label=f"Confirm  ({count} node{'s' if count != 1 else ''})")
        dpg.set_value("_mo_status_text", "")

    def _mo_add_from_listbox(self, listbox_tag: str):
        selected = dpg.get_value(listbox_tag)
        if selected and selected.strip():
            self._multi_add_queue.append(selected)
            self._mo_refresh_staged()

    def _mo_add_proc(self):  self._mo_add_from_listbox("_mo_proc_listbox")

    def _mo_add_data(self):  self._mo_add_from_listbox("_mo_data_listbox")

    def _mo_remove(self):
        selected = dpg.get_value("_mo_staged_listbox")
        if selected and selected in self._multi_add_queue:
            idx = len(self._multi_add_queue) - 1 - self._multi_add_queue[::-1].index(selected)
            self._multi_add_queue.pop(idx)
            self._mo_refresh_staged()

    def _mo_confirm(self):
        if not self._multi_add_queue:
            dpg.set_value("_mo_status_text", "Nothing staged.")
            return
        nodes_to_create = list(self._multi_add_queue)
        if self.preferences['auto_simul_params']:
            simul_params_nodes = [n for n in nodes_to_create if n == "SimulParams"]
            other_nodes        = [n for n in nodes_to_create if n != "SimulParams"]
            nodes_to_create    = simul_params_nodes + other_nodes
        created_uuids = []
        for node_type in nodes_to_create:
            node_uuid = self.nm.create_node(node_type=node_type)
            created_uuids.append((node_uuid, node_type))
        dpg.split_frame()
        dpg.split_frame()
        if self.preferences['auto_simul_params']:
            self._apply_auto_simul_params(created_uuids)
        log.info(f"[ADD_MULTIPLE] Created {len(self._multi_add_queue)} node(s): {self._multi_add_queue}")
        self._multi_add_queue.clear()
        dpg.hide_item("add_multiple_dialog")

    def _apply_auto_simul_params(self, created_uuids):
        simul_params_uuid = None
        for uuid, node_type in created_uuids:
            if node_type == "SimulParams":
                simul_params_uuid = uuid
                break
        if not simul_params_uuid:
            for uuid, node_data in self.nm.graph.nodes.items():
                if node_data.get("type") == "SimulParams":
                    simul_params_uuid = uuid
                    break
        if not simul_params_uuid:
            log.warning("[AUTOSIMULPARAMS] No SimulParams node found, skipping auto-connection")
            return
        simul_params_name = self.nm.graph.nodes[simul_params_uuid].get("name", simul_params_uuid)
        for node_uuid, node_type in created_uuids:
            if node_type == "SimulParams":
                continue
            node_data       = self.nm.graph.nodes.get(node_uuid, {})
            template        = self.nm.all_templates.get(node_type, {})
            template_params = template.get("parameters", {})
            if "simul_params" in template_params:
                param_meta = template_params.get("simul_params", {})
                if param_meta.get("kind") == "reference":
                    ref_key = "simul_params_ref"
                    if not node_data.get("values", {}).get(ref_key):
                        success   = self.nm.manual_link(simul_params_uuid, "ref", node_uuid, ref_key)
                        node_name = node_data.get("name", node_uuid)
                        if success:
                            log.info(f"[AUTOSIMULPARAMS] Connected '{node_name}' ({node_type}) "
                                  f"to SimulParams '{simul_params_name}'")
                        else:
                            log.warning(f"[AUTOSIMULPARAMS] Failed to connect {node_uuid} ({node_type}) to SimulParams")
                    else:
                        log.info(f"[AUTOSIMULPARAMS] Node {node_uuid} already has simul_param_ref connected")

    def _mo_cancel(self):
        self._multi_add_queue.clear()
        dpg.hide_item("add_multiple_dialog")

    def _on_menu_create(self, sender, app_data, user_data):
        node_uuid = self.nm.create_node(node_type=user_data)
        dpg.split_frame()
        dpg.split_frame()
        if self.preferences['auto_simul_params'] and user_data != "SimulParams":
            self._apply_auto_simul_params([(node_uuid, user_data)])
