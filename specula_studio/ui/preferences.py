"""Preferences dialog: theme, render size, font and editor options.

Mixin of :class:`specula_studio.app.SpeculaEditor`; it relies on the editor's
attributes (``nm``, ``fh``, ``preferences``, ``theme_manager``, ...).
"""

import logging
import os
import pathlib

import dearpygui.dearpygui as dpg

from .. import render_scale
from ..theme_manager import THEMES, theme_id, theme_label


from ..fonts import FONT_PATH, MATPLOTLIB_FONTS_PATH

log = logging.getLogger(__name__)


class PreferencesMixin:
    def on_theme_changed(self, sender, app_data):
        """*app_data*: a theme label (from the combo) or id."""
        self.theme_manager.save_preference(theme_id(app_data))
        self.preferences["theme"] = self.theme_manager.current_theme.name

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

        # Recreate the in-editor simulation control panel if open (the fallback
        # when its own window cannot start; that window picks up the theme the
        # next time it is opened)
        if dpg.does_item_exist("sim_control_window"):
            was_open = dpg.is_item_shown("sim_control_window")
            dpg.delete_item("sim_control_window")
            self.sim_control.view = None
            if was_open:
                self.sim_control.show_docked_panel()

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
            dpg.set_value("pref_layout_spacing_input",       int(self.preferences['layout_spacing']))
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
            dpg.add_combo(
                [label for label, _ in THEMES.values()],
                label="Theme",
                default_value=theme_label(current),
                tag="theme_radio",
                width=220,
                callback=self.on_theme_changed,
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
                "Controls font size and node dimensions. "
                "All text and nodes are updated immediately.",
                color=self.theme_manager.get_color("text_hint"),
                wrap=490,
            )

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            # ── Layout spacing ────────────────────────────────────────────────
            dpg.add_input_int(
                label="Layout Spacing (pixels)",
                tag="pref_layout_spacing_input",
                default_value=int(self.preferences["layout_spacing"]),
                min_value=10, max_value=400, min_clamped=True, max_clamped=True,
                step=10, width=160,
                callback=self._on_layout_spacing_changed,
            )
            dpg.add_text(
                "Space between objects in the automatic layouts (Layout menu) and "
                "when several objects are added at once.",
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
                    "When enabled, newly added nodes with a SimulParams reference "
                    "are connected automatically to the existing SimulParams node.",
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
            tm = self.theme_manager
            dpg.add_file_extension(".ttf", color=tm.get_color("file_ext_data"),
                                   custom_text="TrueType Font")
            dpg.add_file_extension(".*", color=tm.get_color("file_ext_other"),
                                   custom_text="All files")

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

    def _bind_ui_font(self) -> None:
        """Load the UI font (once per file and size) and bind it.

        Fonts are never deleted: a font in use must not be freed, and the
        sizes and fonts a session uses are few.  Main thread only (between
        frames): see :meth:`apply_pending_font`.
        """
        font_path = self._font_path()
        if not os.path.exists(font_path):
            log.warning(f"[FONT] Font file not found: {font_path}, using the default font")
            return
        size = render_scale.font_size()
        key = (os.path.normcase(os.path.abspath(font_path)), size)
        handle = self._fonts.get(key)
        if handle is None or not dpg.does_item_exist(handle):
            with dpg.font_registry():
                handle = dpg.add_font(font_path, size)
            self._fonts[key] = handle
            log.info(f"[FONT] Loaded {font_path} at {size} px")
        dpg.bind_font(handle)
        self._font_handle = handle

    def _reload_font(self):
        """Ask the main loop to switch to the current font and size.

        Callbacks run on DearPyGui's callback thread: changing fonts there,
        while the main thread renders a frame with them, crashed the
        application now and then.  :meth:`apply_pending_font` does it
        between two frames.
        """
        self._font_change_pending = True

    def apply_pending_font(self):
        """Main loop, between frames: switch the font if asked, then rebuild the nodes."""
        if not getattr(self, "_font_change_pending", False):
            return
        self._font_change_pending = False
        try:
            self._bind_ui_font()
            if self.nm.graph.nodes:
                self.nm.rebuild_all_nodes_ui()      # node spacers follow the render size
            log.info("[FONT] GUI refreshed with the new font")
        except Exception:
            log.exception("[FONT] Error switching the font")

    def _on_render_size_changed(self, sender, app_data):
        new_size = app_data
        if new_size == self.preferences['render_size']:
            return

        self.preferences['render_size'] = new_size
        render_scale.set_size(new_size)

        log.info(f"[RENDER] Render size → {new_size}")
        self._reload_font()                         # also rebuilds the nodes
        self._save_settings()
        log.info(f"[PREFERENCES] Render size set to: {new_size}")

    def _on_layout_spacing_changed(self, sender, app_data):
        self.preferences['layout_spacing'] = int(app_data)
        self._save_settings()

    def _on_auto_simul_params_changed(self, sender, app_data):
        self.preferences['auto_simul_params'] = app_data
        self._save_settings()
        log.info(f"[PREFERENCES] AutoSimulParams set to: {app_data}")

    def _on_include_defaults_changed(self, sender, app_data):
        self.preferences['include_defaults'] = app_data
        self._save_settings()
        log.info(f"[PREFERENCES] Include Defaults set to: {app_data}")

    def _on_class_source_changed(self, sender, app_data):
        self.preferences['class_source'] = "bundled" if app_data.startswith("Bundled") else "installed"
        self._save_settings()
        log.info("Class metadata source set to %s (applies at next start)",
                 self.preferences['class_source'])
