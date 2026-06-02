"""
help_window.py
==============
Full-help popup window for a selected Specula node.

Triggered by  Help → Selected Object  (or the H key).
Renders in a scrollable DearPyGui child window with sections for:
  • Class summary
  • Parameters table (name | type | default | description)
  • Inputs table
  • Outputs table
  • Full raw docstring (collapsed by default)
"""

import dearpygui.dearpygui as dpg

try:
    from help_provider import get_class_help
    _PROVIDER_OK = True
except ImportError:
    _PROVIDER_OK = False

# Window tag
_HELP_WIN_TAG = "specula_help_window"

# Colour palette
_COL_TITLE      = [100, 220, 255]
_COL_SECTION    = [100, 255, 100]
_COL_TABLE_HEAD = [200, 200, 100]
_COL_NAME       = [255, 200, 100]
_COL_TYPE       = [150, 200, 255]
_COL_DEFAULT    = [180, 180, 180]
_COL_DESC       = [220, 220, 220]
_COL_ERROR      = [255, 100, 100]
_COL_MUTED      = [130, 130, 130]


def show_help_window(class_name: str, node_name: str = ""):
    """
    Open (or refresh) the help window for *class_name*.

    Parameters
    ----------
    class_name : str
        SPECULA class name, e.g. "Modalrec", "SH", "Recmat".
    node_name : str
        Instance name shown in the window title.
    """
    if not _PROVIDER_OK:
        _show_error_window(
            "help_provider.py is not available.\n"
            "Make sure it is in the same directory as specula-studio."
        )
        return

    info = get_class_help(class_name)
    _build_window(info, node_name)


def _show_error_window(msg: str):
    tag = _HELP_WIN_TAG
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)
    with dpg.window(
        label="SPECULA Help – Error", tag=tag,
        width=500, height=200, modal=False,
        on_close=lambda: dpg.delete_item(tag),
    ):
        dpg.add_text(msg, color=_COL_ERROR, wrap=460)


def _build_window(info: dict, node_name: str):
    tag = _HELP_WIN_TAG
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)

    class_name = info["class_name"]
    title_str  = f"Help: {class_name}"
    if node_name and node_name != class_name:
        title_str += f"  [{node_name}]"

    with dpg.window(
        label=title_str, tag=tag,
        width=780, height=640,
        modal=False, no_collapse=False,
        on_close=lambda: dpg.delete_item(tag) if dpg.does_item_exist(tag) else None,
    ):
        # ── Error banner ──────────────────────────────────────────────────────
        if info.get("error"):
            dpg.add_text(info["error"], color=_COL_ERROR, wrap=740)
            dpg.add_separator()

        # ── Class header ──────────────────────────────────────────────────────
        cat = info.get("category", "").replace("_", " ")
        with dpg.group(horizontal=True):
            dpg.add_text(class_name, color=_COL_TITLE)
            if cat and cat != "unknown":
                dpg.add_text(f"  ({cat})", color=_COL_MUTED)

        if info.get("summary"):
            dpg.add_text(info["summary"], color=_COL_DESC, wrap=740)

        dpg.add_separator()
        dpg.add_spacer(height=6)

        # ── Scrollable body ───────────────────────────────────────────────────
        with dpg.child_window(width=-1, height=-1, border=False):

            # ── Parameters ────────────────────────────────────────────────────
            params = info.get("parameters", {})
            if params:
                dpg.add_text("Parameters", color=_COL_SECTION)
                dpg.add_separator()
                _render_params_table(params)
                dpg.add_spacer(height=10)

            # ── Inputs ────────────────────────────────────────────────────────
            inputs = info.get("inputs", {})
            if inputs:
                dpg.add_text("Inputs", color=_COL_SECTION)
                dpg.add_separator()
                _render_io_table(inputs)
                dpg.add_spacer(height=10)

            # ── Outputs ───────────────────────────────────────────────────────
            outputs = info.get("outputs", {})
            if outputs:
                dpg.add_text("Outputs", color=_COL_SECTION)
                dpg.add_separator()
                _render_io_table(outputs)
                dpg.add_spacer(height=10)

            # ── Full docstring (collapsible) ───────────────────────────────────
            full_doc = info.get("full_doc", "").strip()
            if full_doc:
                dpg.add_separator()
                with dpg.collapsing_header(label="Full Docstring", default_open=False):
                    dpg.add_spacer(height=4)
                    dpg.add_text(full_doc, color=_COL_MUTED, wrap=720)

    # Centre the window on first open
    try:
        vw = dpg.get_viewport_width()
        vh = dpg.get_viewport_height()
        dpg.set_item_pos(tag, [(vw - 780) // 2, max(40, (vh - 640) // 2)])
    except Exception:
        pass


def _render_params_table(params: dict):
    """Render a 4-column table: Name | Type | Default | Description."""
    with dpg.table(
        header_row=True,
        borders_innerV=True,
        borders_outerH=True,
        borders_outerV=True,
        row_background=True,
        resizable=True,
        width=-1,
    ):
        dpg.add_table_column(label="Parameter",   width_fixed=True,  init_width_or_weight=150)
        dpg.add_table_column(label="Type",        width_fixed=True,  init_width_or_weight=120)
        dpg.add_table_column(label="Default",     width_fixed=True,  init_width_or_weight=100)
        dpg.add_table_column(label="Description", width_stretch=True)

        for pname, pmeta in params.items():
            with dpg.table_row():
                is_req = pmeta.get("default", "") == "REQUIRED"
                dpg.add_text(pname, color=_COL_NAME if not is_req else [255, 120, 120])
                dpg.add_text(pmeta.get("type", ""),    color=_COL_TYPE)
                def_val = pmeta.get("default", "")
                dpg.add_text(
                    def_val,
                    color=[255, 80, 80] if is_req else _COL_DEFAULT,
                )
                desc = pmeta.get("desc", "")
                dpg.add_text(desc, color=_COL_DESC, wrap=0)


def _render_io_table(io_dict: dict):
    """Render a 3-column table: Name | Type | Description."""
    with dpg.table(
        header_row=True,
        borders_innerV=True,
        borders_outerH=True,
        borders_outerV=True,
        row_background=True,
        resizable=True,
        width=-1,
    ):
        dpg.add_table_column(label="Name",        width_fixed=True,  init_width_or_weight=160)
        dpg.add_table_column(label="Type",        width_fixed=True,  init_width_or_weight=130)
        dpg.add_table_column(label="Description", width_stretch=True)

        for name, meta in io_dict.items():
            with dpg.table_row():
                dpg.add_text(name,                    color=_COL_NAME)
                dpg.add_text(meta.get("type", ""),    color=_COL_TYPE)
                dpg.add_text(meta.get("desc", ""),    color=_COL_DESC, wrap=0)