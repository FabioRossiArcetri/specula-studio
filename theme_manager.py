# theme_manager.py
import json
import os
import dearpygui.dearpygui as dpg

class Theme:
    """Holds all color definitions for a theme (dark or light)."""
    def __init__(self, name: str, colors: dict):
        self.name = name
        self.colors = colors   # mapping from logical name to RGBA tuple (0-255)

    def get(self, key: str):
        return self.colors.get(key, (255, 255, 255, 255))


class ThemeManager:
    """Singleton manager to load, save, and apply themes."""
    _instance = None

    # Logical color keys used throughout the application
    COLOR_KEYS = [
        # Core UI
        "window_bg", "child_bg", "popup_bg",
        "text_default", "text_selected", "text_disabled",
        "frame_bg", "frame_hovered", "frame_active",
        "button_bg", "button_hovered", "button_active",
        "title_bg", "title_text",
        "menu_bar_bg", "menu_bar_text",
        # Node editor (global)
        "node_editor_bg", "plot_grid", "link_default", "link_selected",
        "node_title_bg", "node_selected_border",
        # Plotting
        "plot_bg", "plot_line",
        # Property panel
        "property_group_bg", "property_label",
        # Help window
        "help_bg", "help_text",
        # Semantic / main.py
        "text_instruction", "text_success", "text_muted", "text_secondary",
        "dialog_title", "section_header", "font_selected_text", "accent",
        "status_text", "text_hint",
        # Monitors (common)
        "monitor_output_label", "monitor_status_waiting",
        "monitor_status_receiving", "monitor_status_subscribed", "monitor_status_error",
        # Help window specific
        "text_error", "help_param_name", "help_param_required",
        "help_param_type", "help_default", "help_default_required",
        # Node manager specific
        "node_class_label", "node_ref_input_label", "node_input_label",
        "node_output_label", "node_hint_text", "node_data_obj_ref_output",
        "node_dynamic_output_label", "node_feedback_link_color",
        "node_ref_link_color", "node_io_output_label",
        # Property panel specific
        "param_default", "param_modified", "param_hint", "param_unit",
        "tooltip_text", "panel_section_header", "panel_label_normal",
        "panel_label_required", "panel_label_data_obj", "panel_label_ref",
        "panel_text_secondary", "panel_text_muted", "panel_text_hint",
        "panel_conn_src_name", "panel_conn_dst_name", "panel_monitor_active",
        # Simulation control
        "control_section_header", "control_subsection_header", "control_terminal_title",
        # Node themes (dpg_utils)
        "node_data_bg", "node_data_selected", "node_data_outline",
        "node_proc_bg", "node_proc_selected", "node_proc_outline",
        "node_incomplete_outline",
        # Table / zebra theme
        "table_row_bg", "table_row_bg_alt", "table_header_bg",
    ]
    
    DARK_THEME = {
        # Core
        "window_bg": (28, 28, 32, 255),        # slightly darker
        "child_bg": (36, 36, 40, 255),
        "popup_bg": (45, 45, 50, 255),
        "text_default": (240, 240, 245, 255),
        "text_selected": (0, 160, 210, 255),
        "text_disabled": (128, 128, 135, 255),
        "frame_bg": (52, 52, 58, 255),
        "frame_hovered": (70, 70, 80, 255),
        "frame_active": (30, 110, 160, 255),
        "button_bg": (62, 62, 70, 255),
        "button_hovered": (85, 85, 95, 255),
        "button_active": (45, 125, 185, 255),
        "title_bg": (22, 22, 26, 255),
        "title_text": (245, 245, 250, 255),
        "menu_bar_bg": (35, 35, 40, 255),
        "menu_bar_text": (235, 235, 240, 255),

        # Node editor
        "node_editor_bg": (32, 32, 38, 255),
        "plot_grid": (70, 70, 85, 255),
        "link_default": (160, 170, 210, 255),
        "link_selected": (0, 190, 230, 255),
        "node_title_bg": (55, 55, 70, 255),
        "node_selected_border": (0, 170, 200, 255),

        # Plotting
        "plot_bg": (20, 20, 24, 255),
        "plot_line": (210, 130, 55, 255),

        # Property panel
        "property_group_bg": (38, 38, 44, 255),
        "property_label": (215, 215, 220, 255),

        # Help window
        "help_bg": (28, 28, 35, 255),
        "help_text": (235, 235, 245, 255),

        # Semantic
        "text_instruction": (205, 205, 210, 255),
        "text_success": (100, 220, 100, 255),
        "text_muted": (140, 140, 150, 255),
        "text_secondary": (190, 190, 200, 255),
        "dialog_title": (245, 245, 160, 255),
        "section_header": (90, 190, 255, 255),
        "font_selected_text": (140, 210, 140, 255),
        "accent": (90, 190, 255, 255),
        "status_text": (170, 170, 180, 255),
        "text_hint": (140, 140, 150, 255),

        # Monitors
        "monitor_output_label": (90, 255, 90, 255),
        "monitor_status_waiting": (255, 200, 50, 255),
        "monitor_status_receiving": (0, 200, 240, 255),
        "monitor_status_subscribed": (90, 255, 90, 255),
        "monitor_status_error": (255, 80, 80, 255),

        # Help window specific
        "text_error": (255, 100, 100, 255),
        "help_param_name": (255, 200, 100, 255),
        "help_param_required": (255, 120, 120, 255),
        "help_param_type": (150, 200, 255, 255),
        "help_default": (180, 180, 190, 255),
        "help_default_required": (255, 80, 80, 255),

        # Node manager
        "node_class_label": (180, 180, 195, 255),
        "node_ref_input_label": (140, 255, 140, 255),
        "node_input_label": (240, 240, 250, 255),
        "node_output_label": (240, 240, 250, 255),
        "node_hint_text": (150, 150, 160, 255),
        "node_data_obj_ref_output": (100, 200, 255, 255),
        "node_dynamic_output_label": (100, 255, 255, 255),
        "node_feedback_link_color": (255, 50, 50, 255),
        "node_ref_link_color": (200, 200, 210, 80),   # semi-transparent
        "node_io_output_label": (255, 200, 100, 255),

        # Property panel specific
        "param_default": (110, 110, 120, 255),
        "param_modified": (245, 245, 250, 255),
        "param_hint": (90, 90, 100, 255),
        "param_unit": (120, 180, 120, 255),
        "tooltip_text": (230, 230, 190, 255),
        "panel_section_header": (100, 200, 255, 255),
        "panel_label_normal": (255, 255, 255, 255),
        "panel_label_required": (255, 100, 100, 255),
        "panel_label_data_obj": (150, 200, 255, 255),
        "panel_label_ref": (255, 200, 150, 255),
        "panel_text_secondary": (200, 200, 210, 255),
        "panel_text_muted": (150, 150, 160, 255),
        "panel_text_hint": (255, 150, 100, 255),
        "panel_conn_src_name": (150, 255, 150, 255),
        "panel_conn_dst_name": (150, 255, 150, 255),
        "panel_monitor_active": (0, 255, 0, 255),

        # Simulation control
        "control_section_header": (255, 200, 100, 255),
        "control_subsection_header": (100, 200, 255, 255),
        "control_terminal_title": (150, 150, 160, 255),

        # Node themes
        "node_data_bg": (55, 55, 65, 255),
        "node_data_selected": (170, 80, 20, 255),
        "node_data_outline": (80, 80, 95, 255),
        "node_proc_bg": (40, 60, 90, 255),
        "node_proc_selected": (55, 115, 55, 255),
        "node_proc_outline": (60, 90, 125, 255),
        "node_incomplete_outline": (220, 50, 50, 255),

        # Table / zebra
        "table_row_bg": (42, 42, 48, 255),
        "table_row_bg_alt": (48, 48, 54, 255),
        "table_header_bg": (55, 55, 65, 255),
    }

    LIGHT_THEME = {
        # Core
        "window_bg": (245, 245, 250, 255),
        "child_bg": (252, 252, 255, 255),
        "popup_bg": (255, 255, 255, 255),
        "text_default": (25, 25, 30, 255),
        "text_selected": (0, 110, 210, 255),
        "text_disabled": (150, 150, 160, 255),
        "frame_bg": (230, 232, 238, 255),
        "frame_hovered": (210, 215, 225, 255),
        "frame_active": (180, 210, 250, 255),
        "button_bg": (220, 225, 235, 255),
        "button_hovered": (200, 208, 220, 255),
        "button_active": (160, 190, 230, 255),
        "title_bg": (235, 237, 242, 255),
        "title_text": (15, 15, 20, 255),
        "menu_bar_bg": (240, 242, 248, 255),
        "menu_bar_text": (0, 0, 0, 255),

        # Node editor
        "node_editor_bg": (248, 248, 253, 255),
        "plot_grid": (210, 212, 220, 255),
        "link_default": (80, 80, 120, 255),
        "link_selected": (0, 130, 200, 255),
        "node_title_bg": (220, 225, 235, 255),
        "node_selected_border": (0, 130, 200, 255),

        # Plotting
        "plot_bg": (255, 255, 255, 255),
        "plot_line": (200, 80, 40, 255),

        # Property panel
        "property_group_bg": (240, 242, 248, 255),
        "property_label": (40, 40, 45, 255),

        # Help window
        "help_bg": (250, 250, 252, 255),
        "help_text": (30, 30, 35, 255),

        # Semantic
        "text_instruction": (60, 60, 70, 255),
        "text_success": (35, 150, 35, 255),
        "text_muted": (100, 100, 110, 255),
        "text_secondary": (80, 80, 90, 255),
        "dialog_title": (100, 90, 30, 255),
        "section_header": (0, 120, 200, 255),
        "font_selected_text": (35, 130, 35, 255),
        "accent": (0, 120, 200, 255),
        "status_text": (80, 80, 90, 255),
        "text_hint": (100, 100, 110, 255),

        # Monitors
        "monitor_output_label": (35, 145, 35, 255),
        "monitor_status_waiting": (200, 130, 0, 255),
        "monitor_status_receiving": (0, 120, 200, 255),
        "monitor_status_subscribed": (35, 145, 35, 255),
        "monitor_status_error": (200, 40, 40, 255),

        # Help window specific
        "text_error": (200, 40, 40, 255),
        "help_param_name": (180, 110, 30, 255),
        "help_param_required": (200, 60, 60, 255),
        "help_param_type": (0, 100, 180, 255),
        "help_default": (80, 80, 90, 255),
        "help_default_required": (200, 40, 40, 255),

        # Node manager
        "node_class_label": (100, 100, 110, 255),
        "node_ref_input_label": (55, 200, 55, 255),
        "node_input_label": (0, 0, 0, 255),
        "node_output_label": (0, 0, 0, 255),
        "node_hint_text": (80, 80, 90, 255),
        "node_data_obj_ref_output": (0, 120, 200, 255),
        "node_dynamic_output_label": (0, 170, 170, 255),
        "node_feedback_link_color": (200, 0, 0, 200),
        "node_ref_link_color": (100, 100, 110, 100),   # semi-transparent
        "node_io_output_label": (180, 120, 30, 255),

        # Property panel specific
        "param_default": (90, 90, 100, 255),
        "param_modified": (25, 25, 30, 255),
        "param_hint": (110, 110, 120, 255),
        "param_unit": (45, 105, 45, 255),
        "tooltip_text": (45, 45, 35, 255),
        "panel_section_header": (0, 120, 200, 255),
        "panel_label_normal": (0, 0, 0, 255),
        "panel_label_required": (200, 50, 50, 255),
        "panel_label_data_obj": (0, 100, 180, 255),
        "panel_label_ref": (180, 110, 30, 255),
        "panel_text_secondary": (80, 80, 90, 255),
        "panel_text_muted": (120, 120, 130, 255),
        "panel_text_hint": (200, 70, 40, 255),
        "panel_conn_src_name": (45, 150, 45, 255),
        "panel_conn_dst_name": (45, 150, 45, 255),
        "panel_monitor_active": (0, 150, 0, 255),

        # Simulation control
        "control_section_header": (180, 120, 30, 255),
        "control_subsection_header": (0, 120, 200, 255),
        "control_terminal_title": (100, 100, 110, 255),

        # Node themes
        "node_data_bg": (235, 237, 242, 255),
        "node_data_selected": (200, 120, 40, 255),
        "node_data_outline": (180, 182, 190, 255),
        "node_proc_bg": (205, 220, 240, 255),
        "node_proc_selected": (100, 160, 100, 255),
        "node_proc_outline": (160, 175, 195, 255),
        "node_incomplete_outline": (200, 60, 60, 255),

        # Table / zebra
        "table_row_bg": (240, 242, 248, 255),
        "table_row_bg_alt": (245, 247, 252, 255),
        "table_header_bg": (225, 228, 235, 255),
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.current_theme = None
        self.config_path = os.path.join(os.path.dirname(__file__), "user_prefs.json")
        self.load_preference()

    def load_preference(self):
        theme_name = "dark"
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    prefs = json.load(f)
                    theme_name = prefs.get("theme", "dark")
            except:
                pass
        if theme_name == "light":
            self.current_theme = Theme("light", self.LIGHT_THEME)
        else:
            self.current_theme = Theme("dark", self.DARK_THEME)

    def save_preference(self, theme_name: str):
        prefs = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    prefs = json.load(f)
            except:
                pass
        prefs["theme"] = theme_name
        with open(self.config_path, "w") as f:
            json.dump(prefs, f, indent=2)
        self.load_preference()
        self.apply_theme()

    def apply_theme(self):
        """Apply global theme (window backgrounds, buttons, etc.)"""
        if not self.current_theme:
            return

        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvThemeCat_Core):
                color_mappings = {
                    dpg.mvThemeCol_WindowBg: "window_bg",
                    dpg.mvThemeCol_ChildBg: "child_bg",
                    dpg.mvThemeCol_PopupBg: "popup_bg",
                    dpg.mvThemeCol_Text: "text_default",
                    dpg.mvThemeCol_TextSelectedBg: "text_selected",
                    dpg.mvThemeCol_TextDisabled: "text_disabled",
                    dpg.mvThemeCol_FrameBg: "frame_bg",
                    dpg.mvThemeCol_FrameBgHovered: "frame_hovered",
                    dpg.mvThemeCol_FrameBgActive: "frame_active",
                    dpg.mvThemeCol_Button: "button_bg",
                    dpg.mvThemeCol_ButtonHovered: "button_hovered",
                    dpg.mvThemeCol_ButtonActive: "button_active",
                    dpg.mvThemeCol_TitleBg: "title_bg",
                    dpg.mvThemeCol_TitleBgActive: "title_bg",
                    dpg.mvThemeCol_TitleBgCollapsed: "title_bg",
                    dpg.mvThemeCol_MenuBarBg: "menu_bar_bg",
                }
                for dpg_key, logical_key in color_mappings.items():
                    dpg.add_theme_color(dpg_key, self.current_theme.get(logical_key))

                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 3)

                dpg.add_theme_color(
                    dpg.mvNodeCol_NodeBackground,
                    self.current_theme.get("node_editor_bg"),
                    category=dpg.mvThemeCat_Nodes
                )
                # Grid background (often the same area)
                dpg.add_theme_color(
                    dpg.mvNodeCol_GridBackground,
                    self.current_theme.get("node_editor_bg"),
                    category=dpg.mvThemeCat_Nodes
                )
                # Grid lines (optional – use a subtle contrast)
                dpg.add_theme_color(
                    dpg.mvNodeCol_GridLine,
                    self.current_theme.get("plot_grid"),
                    category=dpg.mvThemeCat_Nodes
                )
                # Link colours (already there)
                dpg.add_theme_color(
                    dpg.mvNodeCol_Link,
                    self.current_theme.get("link_default"),
                    category=dpg.mvThemeCat_Nodes
                )
                dpg.add_theme_color(
                    dpg.mvNodeCol_LinkSelected,
                    self.current_theme.get("link_selected"),
                    category=dpg.mvThemeCat_Nodes
                )

        dpg.bind_theme(global_theme)

    def get_color(self, key: str):
        return self.current_theme.get(key)