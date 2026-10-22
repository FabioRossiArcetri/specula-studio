"""Preferences dialog: theme, render size, font and editor options.

Mixin of :class:`specula_studio.app.SpeculaEditor`; it relies on the editor's
attributes (``nm``, ``fh``, ``preferences``, ``theme_manager``, ...).
"""

import logging
import os
import pathlib

import dearpygui.dearpygui as dpg

from .. import render_scale


from ..fonts import FONT_PATH, MATPLOTLIB_FONTS_PATH

log = logging.getLogger(__name__)


class PreferencesMixin:
    def on_theme_changed(self, sender, app_data):
        theme_name = app_data
        self.theme_manager.save_preference(theme_name)

        # Node themes capture colours at creation time: recreate them for the
        # new palette, then rebuild all nodes so they pick up the new themes.
        self.nm.init_themes()
        self.nm.rebuild_all_nodes_ui()

        # Refresh static UI elements that are not part of the node graph
        if dpg.does_item_exist("status_bar_text"):
            dpg.configure_item("status_bar_text", color=self.theme_manager.get_color("status_text"))

        # Refresh property panel if visible
        if dpg.does_item_exist("property_panel") and dpg.is_item_shown("property_panel"):
            selected = self.nm.get_selected_nodes()
            if len(selected) == 1:
                self.nm.update_property_panel(selected[0], "property_panel")
            elif self.nm._selected_link_id is not None:
                self.nm.update_connection_panel(self.nm._selected_link_id, "property_panel")

        # Recreate the docked simulation control panel if open (the separate
        # control window picks up the theme the next time it is opened)
        if dpg.does_item_exist("sim_control_window"):
            was_open = dpg.is_item_shown("sim_control_window")
            dpg.delete_item("sim_control_window")
            self.sim_control.view = None
            if was_open:
                self.sim_control.show_docked_panel()

        # Close preferences dialog (it will be recreated on next open)
        if dpg.does_item_exist("pref_window"):
            dpg.delete_item("pref_window")

        # Force a redraw
        dpg.split_frame()

    def _font_path(self) -> str:
        """Preferred font if it still exists, matplotlib's DejaVuSerif otherwise."""
        path = self.preferences.get('font_path')
        if path and os.path.exists(path):
            return path
        return FONT_PATH

    def _save_settings(self):
        """Persist current preferences."""
        self.preferences.save()

    def _show_preferences_dialog(self):
        if not dpg.does_item_exist("preferences_dialog"):
            self._create_preferences_dialog()
        else:
            dpg.set_value("pref_auto_simul_params_checkbox", self.preferences['auto_simul_params'])
            dpg.set_value("pref_include_defaults_checkbox",  self.preferences['include_defaults'])
            dpg.set_value("pref_render_size_radio",          self.preferences['render_size'])
            # Update font path display
            font_name = pathlib.Path(self._font_path()).name
            dpg.set_value("pref_font_path_text", f"Selected: {font_name}")
        self._center_dialog("preferences_dialog")
        dpg.show_item("preferences_dialog")

    def _create_preferences_dialog(self):
        with dpg.window(
            label="Preferences", tag="preferences_dialog",
            modal=True, show=False, width=540, height=840, no_resize=True
        ):
            dpg.add_text("Preferences", color=self.theme_manager.get_color("dialog_title"))
            dpg.add_separator()
            dpg.add_spacer(height=12)

            dpg.add_text("Appearance")
            current = self.theme_manager.current_theme.name
            dpg.add_radio_button(
                label="Theme",
                items=["dark", "light"],
                default_value=current,
                tag="theme_radio",
                callback=self.on_theme_changed
            )

            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── Render Size ───────────────────────────────────────────────────
            dpg.add_text("Render Size", color=self.theme_manager.get_color("section_header"))
            dpg.add_radio_button(
                items=render_scale.RENDER_SIZES,
                tag="pref_render_size_radio",
                default_value=self.preferences['render_size'],
                horizontal=True,
                callback=self._on_render_size_changed,
            )
            dpg.add_text(
                "Controls font size and node dimensions.\n"
                "MICRO, SMALL,  MEDIUM, LARGE .\n"
                "All text and nodes are updated immediately.",
                color=self.theme_manager.get_color("text_hint"),
                wrap=490,
            )

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── Font Selection ────────────────────────────────────────────────
            dpg.add_text("Font", color=self.theme_manager.get_color("section_header"))
            font_name = pathlib.Path(self._font_path()).name
            dpg.add_text(
                f"Selected: {font_name}",
                tag="pref_font_path_text",
                color=self.theme_manager.get_color("font_selected_text")
            )
            dpg.add_button(
                label="Browse Fonts",
                width=-1,
                callback=self._on_browse_fonts_clicked,
            )
            dpg.add_text(
                "Select a TrueType font (.ttf) from the matplotlib font directory.\n"
                "The GUI will refresh with the new font.",
                color=self.theme_manager.get_color("text_hint"),
                wrap=490,
            )

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── AutoSimulParams ───────────────────────────────────────────────
            with dpg.group(horizontal=False):
                dpg.add_checkbox(
                    label="Auto Connect SimulParams (AutoSimulParams)",
                    tag="pref_auto_simul_params_checkbox",
                    default_value=self.preferences['auto_simul_params'],
                    callback=self._on_auto_simul_params_changed,
                )
                dpg.add_text(
                    "When enabled, newly added nodes with SimulParams reference\n"
                    "will automatically connect to the existing SimulParams node.",
                    color=self.theme_manager.get_color("text_hint"),
                    wrap=490,
                )

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── Include Defaults ──────────────────────────────────────────────
            with dpg.group(horizontal=False):
                dpg.add_checkbox(
                    label="Include Default Values in Saved Simulations",
                    tag="pref_include_defaults_checkbox",
                    default_value=self.preferences['include_defaults'],
                    callback=self._on_include_defaults_changed,
                )
                dpg.add_text(
                    "When enabled, default parameter values will be included\n"
                    "when saving simulations.",
                    color=self.theme_manager.get_color("text_hint"),
                    wrap=490,
                )

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── Simulation control panel ──────────────────────────────────────
            dpg.add_checkbox(
                label="Simulation control panel in a separate window",
                default_value=self.preferences.get("control_panel", "window") != "docked",
                callback=self._on_control_panel_mode_changed,
            )
            dpg.add_text(
                "Off: the panel opens as a window inside the editor.",
                color=self.theme_manager.get_color("text_hint"),
                wrap=490,
            )
            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── Class metadata source ─────────────────────────────────────────
            dpg.add_text("Class Metadata", color=self.theme_manager.get_color("section_header"))
            dpg.add_radio_button(
                items=["Installed SPECULA", "Bundled templates"],
                tag="pref_class_source_radio",
                default_value=("Bundled templates"
                               if self.preferences.get("class_source") == "bundled"
                               else "Installed SPECULA"),
                callback=self._on_class_source_changed,
            )
            dpg.add_text(
                f"In use: {self.catalog_info}\n"
                "The catalog of the installed SPECULA is generated in the\n"
                "background and cached; changes apply at the next start.",
                color=self.theme_manager.get_color("text_hint"),
                wrap=490,
            )

            dpg.add_spacer(height=20)
            dpg.add_separator()
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Close",
                    width=100,
                    callback=lambda: dpg.hide_item("preferences_dialog")
                )

    def _on_browse_fonts_clicked(self):
        """Open file dialog to select a font (modal, appears over preferences dialog)."""
        # Hide preferences dialog
        if dpg.does_item_exist("preferences_dialog"):
            dpg.hide_item("preferences_dialog")

        dialog_tag = "font_selection_dialog"
        if dpg.does_item_exist(dialog_tag):
            dpg.show_item(dialog_tag)
            dpg.focus_item(dialog_tag)
            return

        with dpg.file_dialog(
            label="Select Font (TTF)",
            show=True,
            callback=self._on_font_selected,
            tag=dialog_tag,
            width=700,
            height=400,
            default_path=str(MATPLOTLIB_FONTS_PATH),
            modal=True,
        ):
            dpg.add_file_extension(".ttf", color=(150, 150, 150), custom_text="TrueType Font")
            dpg.add_file_extension(".*", color=(100, 100, 100), custom_text="All files")

    def _on_font_selected(self, sender, app_data):
        """Callback when a font file is selected."""
        file_path = app_data.get('file_path_name')
        if not file_path or not os.path.exists(file_path):
            log.info("[FONT] No valid file selected")
            return

        if not file_path.lower().endswith('.ttf'):
            log.info("[FONT] Selected file is not a .ttf font file")
            return

        # Update preferences
        self.preferences['font_path'] = file_path
        self._save_settings()

        # Reload font and refresh UI
        self._reload_font()

        # Update the preference dialog display
        if dpg.does_item_exist("pref_font_path_text"):
            font_name = pathlib.Path(file_path).name
            dpg.set_value("pref_font_path_text", f"Selected: {font_name}")

        log.info(f"[FONT] Font changed to: {file_path}")

    def _reload_font(self):
        """Reload the font in DPG and refresh the GUI."""
        font_path = self._font_path()

        if not os.path.exists(font_path):
            log.warning(f"[FONT] Font file not found: {font_path}")
            return

        try:
            # Remove old font handle if it exists
            if self._font_handle is not None and dpg.does_item_exist(self._font_handle):
                dpg.delete_item(self._font_handle)

            # Load new font
            with dpg.font_registry():
                current_fs = render_scale.font_size()
                self._font_handle = dpg.add_font(font_path, current_fs)
                log.info(f"[FONT] Loaded font from {font_path} at {current_fs} px")

            # Bind the new font
            dpg.bind_font(self._font_handle)

            # Rebuild UI with new font
            if self.nm.graph.nodes:
                self.nm.rebuild_all_nodes_ui()

            log.info("[FONT] GUI refreshed with new font")
        except Exception as e:
            log.error(f"[FONT] Error loading font: {e}")

    def _on_render_size_changed(self, sender, app_data):
        new_size = app_data
        if new_size == self.preferences['render_size']:
            return

        self.preferences['render_size'] = new_size
        render_scale.set_size(new_size)

        log.info(f"[RENDER] Render size → {new_size}")
        self._reload_font()

        if self.nm.graph.nodes:
            self.nm.rebuild_all_nodes_ui()

        self._save_settings()
        log.info(f"[PREFERENCES] Render size set to: {new_size}")

    def _on_auto_simul_params_changed(self, sender, app_data):
        self.preferences['auto_simul_params'] = app_data
        self._save_settings()
        log.info(f"[PREFERENCES] AutoSimulParams set to: {app_data}")

    def _on_include_defaults_changed(self, sender, app_data):
        self.preferences['include_defaults'] = app_data
        self._save_settings()
        log.info(f"[PREFERENCES] Include Defaults set to: {app_data}")

    def _on_control_panel_mode_changed(self, sender, app_data):
        self.preferences['control_panel'] = "window" if app_data else "docked"
        self._save_settings()

    def _on_class_source_changed(self, sender, app_data):
        self.preferences['class_source'] = "bundled" if app_data.startswith("Bundled") else "installed"
        self._save_settings()
        log.info("Class metadata source set to %s (applies at next start)",
                 self.preferences['class_source'])
