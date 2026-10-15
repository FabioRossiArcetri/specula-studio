"""Overrides menu: loading, toggling (preview) and removing override files.

Mixin of :class:`specula_studio.app.SpeculaEditor`; it relies on the editor's
attributes (``nm``, ``fh``, ``preferences``, ``theme_manager``, ...).
"""

import logging
import pathlib

import dearpygui.dearpygui as dpg


try:
    from ..help_window import show_help_window
    _HELP_AVAILABLE = True
except ImportError:
    _HELP_AVAILABLE = False

    def show_help_window(*_):
        pass


log = logging.getLogger(__name__)


class OverridesMixin:
    def _show_load_overrides_dialog(self):
        """Show file dialog to select override YAML file(s)."""
        if not dpg.does_item_exist("load_overrides_dialog"):
            with dpg.file_dialog(
                label="Load Override File(s)",
                show=False,
                callback=self._on_overrides_loaded,
                id="load_overrides_dialog",
                width=700,
                height=400,
                default_filename="",
            ):
                dpg.add_file_extension(".yml")
                dpg.add_file_extension(".yaml")
        dpg.show_item("load_overrides_dialog")

    def _on_overrides_loaded(self, sender, app_data):
        """Callback: override file(s) chosen in the file dialog."""
        selections = app_data.get('file_path_name')
        if not selections:
            return
        if isinstance(selections, str):
            selections = [selections]

        loaded = self.override_manager.load_overrides(selections)
        if loaded:
            log.info(f"[OVERRIDES] Loaded {loaded} file(s) — click a name in the "
                  f"Overrides menu to enable/apply")
        # Do NOT apply yet — overrides load disabled; user enables via menu click
        self._refresh_overrides_menu()

    def _refresh_overrides_menu(self):
        """
        Rebuild the dynamic portion of the Overrides menu.

        One menu item per registered override is inserted between
        'overrides_sep_top' and 'overrides_sep_bottom'.  Each item shows
        the current enabled state (✓ / ○) and toggles it on click.

        FIX: DPG calls every callback as callback(sender, app_data, user_data).
        Using a plain lambda with a default argument (lambda s, a, p=path:)
        causes DPG's positional user_data=None to override the captured path.
        The correct pattern is to pass user_data=path explicitly and read it
        as the third positional argument.
        """
        # Remove previously created dynamic items
        for tag in self._override_item_tags:
            if dpg.does_item_exist(tag):
                dpg.delete_item(tag)
        self._override_item_tags.clear()

        for i, path in enumerate(self.override_manager.get_all_overrides()):
            is_enabled = self.override_manager.is_enabled(path)
            label = ("✓  " if is_enabled else "○  ") + pathlib.Path(path).name
            tag = f"_override_mi_{i}"
            dpg.add_menu_item(
                label=label,
                tag=tag,
                parent="overrides_menu",
                before="overrides_sep_bottom",
                # user_data carries the path; DPG passes it as the 3rd arg
                user_data=path,
                callback=lambda s, a, u: self._on_override_toggled(u),
            )
            self._override_item_tags.append(tag)

    def _on_override_toggled(self, path: str):
        """
        Called when the user clicks an override entry in the menu.

        1. If this is the first override being enabled and no snapshot exists
           yet → take a snapshot of the current (clean) simulation.
        2. Toggle the override's enabled state.
        3. Re-derive the simulation: base snapshot + all enabled overrides.
        4. If no overrides remain enabled → restore from snapshot and clear it.
        5. Rebuild the menu to reflect the new state.
        """
        # ── 1. Take snapshot before any override is applied ───────────────────
        if not self.override_manager.any_enabled() and \
                not self.override_manager.has_base_snapshot():
            self._take_base_snapshot()

        # ── 2. Toggle ────────────────────────────────────────────────────────
        self.override_manager.toggle_override(path)

        # ── 3 / 4. Apply or restore ───────────────────────────────────────────
        self._apply_overrides_to_simulation()

        # ── 5. Rebuild menu ───────────────────────────────────────────────────
        self._refresh_overrides_menu()

    def _current_export(self) -> dict:
        """Export the editor graph (with current positions), no override metadata."""
        self.fh.capture_positions()
        return self.fh.export_to_yaml_dict(
            include_defaults=self.preferences.get('include_defaults', False),
            include_override_metadata=False,
        )

    def _take_base_snapshot(self):
        """Store the current (override-free) simulation as the preview base."""
        snapshot = self._current_export()
        self.override_manager.set_base_snapshot(snapshot)
        log.info("Override base snapshot taken: %d object(s)", len(snapshot))

    def _apply_overrides_to_simulation(self):
        """
        Reload the graph to reflect the current override state.

        Edits made while a preview was active are folded into the base first,
        so toggling overrides never loses them.

        * At least one override enabled → base + enabled overrides
        * No overrides enabled          → base (preview ends)
        """
        om = self.override_manager
        if not om.has_base_snapshot():
            if not om.any_enabled():
                return
            self._take_base_snapshot()
        else:
            om.commit_edits(self._current_export())

        base = om.get_base_snapshot()
        if om.any_enabled():
            merged = om.apply_overrides(base)
            log.info("Reloading with %d active override(s)", len(om.get_enabled_overrides()))
            self.fh.load_from_yaml_dict(merged)
            om.set_merged_snapshot(self._current_export())
        else:
            log.info("All overrides disabled — restoring the base simulation")
            self.fh.load_from_yaml_dict(base)
            om.clear_base_snapshot()

    def _show_remove_override_menu(self):
        """Open a modal popup listing loaded overrides with Remove buttons."""
        overrides = self.override_manager.get_all_overrides()
        if not overrides:
            log.info("[OVERRIDES] No overrides loaded")
            return

        if dpg.does_item_exist("override_remove_popup"):
            dpg.delete_item("override_remove_popup")

        with dpg.window(
            label="Remove Override",
            tag="override_remove_popup",
            modal=True,
            show=True,
            width=520,
            height=min(120 + len(overrides) * 36, 500),
            no_resize=False,
        ):
            dpg.add_text(
                "Click a button to remove that override file:",
                color=self.theme_manager.get_color("dialog_title")
            )
            dpg.add_separator()
            dpg.add_spacer(height=4)

            for path in overrides:
                is_enabled = self.override_manager.is_enabled(path)
                state_badge = "ON " if is_enabled else "OFF"
                btn_label   = f"[{state_badge}]  Remove: {pathlib.Path(path).name}"
                # Text colour is handled by the theme below
                dpg.add_button(
                    label=btn_label,
                    width=-1,
                    callback=lambda s, a, p=path: self._on_remove_override(p),
                )
                # Set text colour for the button: green if enabled, otherwise muted
                btn_color = self.theme_manager.get_color("text_success") if is_enabled else self.theme_manager.get_color("text_muted")
                dpg.bind_item_theme(dpg.last_item(),
                                    self._make_text_color_theme(btn_color))

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_button(
                label="Close",
                width=-1,
                callback=lambda: dpg.delete_item("override_remove_popup"),
            )

    @staticmethod
    def _make_text_color_theme(color):
        """Create a one-off button theme with the given text colour."""
        with dpg.theme() as t:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Text, color)
        return t

    def _on_remove_override(self, file_path: str):
        """
        Remove an override from the registry, then re-derive simulation state.
        If the removed override was enabled, re-apply the remaining ones.
        """
        was_enabled = self.override_manager.is_enabled(file_path)
        self.override_manager.remove_override(file_path)

        if dpg.does_item_exist("override_remove_popup"):
            dpg.delete_item("override_remove_popup")

        if was_enabled:
            # Re-apply remaining overrides (restores base if none are left)
            self._apply_overrides_to_simulation()

        self._refresh_overrides_menu()
