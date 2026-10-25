import dearpygui.dearpygui as dpg
from . import render_scale
from .core import layout
from .theme_manager import ThemeManager
import logging

log = logging.getLogger(__name__)


def create_node_theme(
    node_background_key,
    node_outline_key,
    node_background_selected_key=None,
    title_bar_key=None,
    title_bar_hovered_key=None,
    title_bar_selected_key=None,
    pin_key=None,
    pin_hovered_key=None,
    text_key=None,
    use_category: bool = False,
    border_thickness: float = None,
):
    """
    Generic node theme factory.

    All color parameters are **keys** into the ThemeManager.
    The actual RGBA values are fetched from the current theme at runtime.

    Set use_category=True to pass category=dpg.mvThemeCat_Nodes on every
    color call (required by the "complete" data/proc themes).
    Components for pins and text are only emitted when their keys are given.
    border_thickness, when provided, sets mvNodeStyleVar_NodeBorderThickness.
    """
    tm = ThemeManager()
    ckw = {"category": dpg.mvThemeCat_Nodes} if use_category else {}

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            if title_bar_key is not None:
                dpg.add_theme_color(dpg.mvNodeCol_TitleBar, tm.get_color(title_bar_key), **ckw)
            if title_bar_hovered_key is not None:
                dpg.add_theme_color(dpg.mvNodeCol_TitleBarHovered, tm.get_color(title_bar_hovered_key), **ckw)
            if title_bar_selected_key is not None:
                dpg.add_theme_color(dpg.mvNodeCol_TitleBarSelected, tm.get_color(title_bar_selected_key), **ckw)
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackground, tm.get_color(node_background_key), **ckw)
            if node_background_selected_key is not None:
                dpg.add_theme_color(dpg.mvNodeCol_NodeBackgroundSelected, tm.get_color(node_background_selected_key), **ckw)
            dpg.add_theme_color(dpg.mvNodeCol_NodeOutline, tm.get_color(node_outline_key), **ckw)
            if border_thickness is not None:
                dpg.add_theme_style(
                    dpg.mvNodeStyleVar_NodeBorderThickness,
                    border_thickness,
                    category=dpg.mvThemeCat_Nodes,
                )

        if pin_key is not None or pin_hovered_key is not None:
            with dpg.theme_component(dpg.mvNodeAttribute):
                if pin_key is not None:
                    dpg.add_theme_color(dpg.mvNodeCol_Pin, tm.get_color(pin_key), **ckw)
                if pin_hovered_key is not None:
                    dpg.add_theme_color(dpg.mvNodeCol_PinHovered, tm.get_color(pin_hovered_key), **ckw)

        if text_key is not None:
            with dpg.theme_component(dpg.mvText):
                dpg.add_theme_color(dpg.mvThemeCol_Text, tm.get_color(text_key), **ckw)

    return theme


def create_data_node_theme():
    """Data node theme (complete)."""
    return create_node_theme(
        node_background_key          = "node_data_bg",
        node_background_selected_key = "node_data_selected",
        node_outline_key             = "node_data_outline",
        title_bar_key                = "node_data_title",
        title_bar_hovered_key        = "node_data_title_hover",
        title_bar_selected_key       = "node_data_title_selected",
        pin_key                      = "node_pin",
        pin_hovered_key              = "node_pin_hover",
        use_category                 = True,
    )


def create_proc_node_theme():
    """Processing node theme (complete)."""
    return create_node_theme(
        node_background_key          = "node_proc_bg",
        node_background_selected_key = "node_proc_selected",
        node_outline_key             = "node_proc_outline",
        title_bar_key                = "node_proc_title",
        title_bar_hovered_key        = "node_proc_title_hover",
        title_bar_selected_key       = "node_proc_title_selected",
        pin_key                      = "node_pin",
        pin_hovered_key              = "node_pin_hover",
        use_category                 = True,
    )


def create_data_node_theme_incomplete():
    """
    Incomplete data node theme – same as complete but with a red 2 px border.
    """
    return create_node_theme(
        node_background_key          = "node_data_bg",
        node_background_selected_key = "node_data_selected",
        node_outline_key             = "node_incomplete_outline",
        title_bar_key                = "node_data_title",
        title_bar_hovered_key        = "node_data_title_hover",
        title_bar_selected_key       = "node_data_title_selected",
        pin_key                      = "node_pin",
        pin_hovered_key              = "node_pin_hover",
        border_thickness             = 2.0,
        use_category                 = True,
    )


def create_proc_node_theme_incomplete():
    """
    Incomplete processing node theme – same as complete but with a red 2 px border.
    """
    return create_node_theme(
        node_background_key          = "node_proc_bg",
        node_background_selected_key = "node_proc_selected",
        node_outline_key             = "node_incomplete_outline",
        title_bar_key                = "node_proc_title",
        title_bar_hovered_key        = "node_proc_title_hover",
        title_bar_selected_key       = "node_proc_title_selected",
        pin_key                      = "node_pin",
        pin_hovered_key              = "node_pin_hover",
        border_thickness             = 2.0,
        use_category                 = True,
    )


def create_ref_pin_theme():
    """Reference pins (bound to their node attributes): yellow, not orange."""
    tm = ThemeManager()
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNodeAttribute):
            dpg.add_theme_color(dpg.mvNodeCol_Pin, tm.get_color("node_ref_pin"),
                                category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_color(dpg.mvNodeCol_PinHovered, tm.get_color("node_ref_pin_hover"),
                                category=dpg.mvThemeCat_Nodes)
    return theme


def apply_link_style(link_id: int, color: list, thickness: float = 1.0) -> None:
    """Apply a colour/thickness theme to a node link."""
    with dpg.theme() as link_theme:
        with dpg.theme_component(dpg.mvNodeLink):
            dpg.add_theme_color(dpg.mvNodeCol_Link, color, category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_style(dpg.mvNodeStyleVar_LinkThickness, thickness,
                                category=dpg.mvThemeCat_Nodes)
    dpg.bind_item_theme(link_id, link_theme)


def place_new_nodes(graph, uuid_to_dpg, new_uuids, spacing: int = 60) -> None:
    """Move the drawn nodes *new_uuids* next to the nodes they are linked to
    (:func:`core.layout.place_new`); the other nodes stay where they are."""
    drawn = {u: i for u, i in uuid_to_dpg.items() if i and dpg.does_item_exist(i)}
    new = [u for u in new_uuids if u in drawn]
    if not new:
        return
    fallback = (render_scale.node_header_spacer_width(), 4 * render_scale.font_size())
    sizes = {}
    for u, item in drawn.items():
        w, h = dpg.get_item_rect_size(item)
        sizes[u] = (w if w > 0 else fallback[0], h if h > 0 else fallback[1])
    fixed = {u: tuple(dpg.get_item_pos(i)) for u, i in drawn.items() if u not in new}
    edges, _ = layout.graph_edges(graph)
    for u, (x, y) in layout.place_new(new, fixed, sizes, edges, spacing).items():
        dpg.set_item_pos(drawn[u], [round(x), round(y)])


def auto_layout_nodes(graph, uuid_to_dpg, strategy: str = "left_to_right",
                      spacing: int = 60, data_types=()):
    """Lay the graph out with one of :data:`core.layout.STRATEGIES`, using the
    actual node sizes (nodes must have been drawn once): *spacing* pixels
    between nodes, *data_types* the classes of data objects."""
    if not graph.nodes:
        return
    nodes = [u for u in graph.nodes if uuid_to_dpg.get(u) and dpg.does_item_exist(uuid_to_dpg[u])]
    fallback = (render_scale.node_header_spacer_width(), 4 * render_scale.font_size())
    sizes = {}
    for u in nodes:
        w, h = dpg.get_item_rect_size(uuid_to_dpg[u])
        sizes[u] = (w if w > 0 else fallback[0], h if h > 0 else fallback[1])
    edges, refs = layout.graph_edges(graph)
    options = dict(gap_x=spacing, gap_y=spacing,
                   origin=(render_scale.auto_layout_base_x(), render_scale.auto_layout_base_y()))
    if strategy == "compact":
        try:
            options["max_width"] = dpg.get_item_rect_size("specula_editor_parent")[0] - 40
        except Exception:
            pass
    elif strategy == "data_objects_on_top":
        options["data_objects"] = {u for u in nodes if graph.nodes[u].get("type") in data_types}
    positions = layout.STRATEGIES[strategy](nodes, sizes, edges, refs, **options)
    for u, (x, y) in positions.items():
        dpg.set_item_pos(uuid_to_dpg[u], [round(x), round(y)])
    log.debug("[AUTO_LAYOUT] %s: %d nodes", strategy, len(positions))
