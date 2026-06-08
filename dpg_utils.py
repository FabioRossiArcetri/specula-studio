import dearpygui.dearpygui as dpg
from collections import deque
import render_scale
from theme_manager import ThemeManager


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
    """Data node theme – complete (green/grey)."""
    return create_node_theme(
        node_background_key          = "node_data_bg",
        node_background_selected_key = "node_data_selected",
        node_outline_key             = "node_data_outline",
        use_category                 = True,
    )


def create_proc_node_theme():
    """Processing node theme – complete (blue/green)."""
    return create_node_theme(
        node_background_key          = "node_proc_bg",
        node_background_selected_key = "node_proc_selected",
        node_outline_key             = "node_proc_outline",
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
        border_thickness             = 2.0,
        use_category                 = True,
    )


def apply_link_style(link_id: int, color: list, thickness: float = 1.0) -> None:
    """Apply a colour/thickness theme to a node link."""
    with dpg.theme() as link_theme:
        with dpg.theme_component(dpg.mvNodeLink):
            dpg.add_theme_color(dpg.mvNodeCol_Link, color, category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_style(dpg.mvNodeStyleVar_LinkThickness, thickness,
                                category=dpg.mvThemeCat_Nodes)
    dpg.bind_item_theme(link_id, link_theme)


def set_zebra_theme():
    """Apply a zebra-stripe theme for tables (file dialogs, property tables)."""
    tm = ThemeManager()
    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg,    tm.get_color("table_row_bg"),    category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, tm.get_color("table_row_bg_alt"), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, tm.get_color("table_header_bg"),  category=dpg.mvThemeCat_Core)            
    dpg.bind_theme(global_theme)


def auto_layout_nodes(graph, uuid_to_dpg, debug=False):
    """Organize nodes into a grid layout using actual node sizes to prevent overlap."""
    if debug:
        print(f"[AUTO_LAYOUT] Starting auto layout with {len(graph.nodes)} nodes")

    if not graph.nodes:
        print("[AUTO_LAYOUT] No nodes to layout")
        return

    nodes = list(graph.nodes.keys())

    # --- Build clean connection list (skip feedback / reference edges) ---
    clean_connections = []
    for conn in graph.connections:
        src, src_attr, dst, dst_attr = conn

        if dst_attr.endswith("_ref") or dst_attr == "layer_list" or "params" in dst_attr.lower():
            if debug:
                print(f"[AUTO_LAYOUT] Skipping reference: {src}.{src_attr} -> {dst}.{dst_attr}")
            continue

        conn_props = graph.connection_properties.get(conn, {})
        if conn_props.get('delay', 0) == -1:
            if debug:
                print(f"[AUTO_LAYOUT] Skipping feedback: {src}.{src_attr} -> {dst}.{dst_attr}")
            continue

        if ":-" in str(src_attr):
            if debug:
                print(f"[AUTO_LAYOUT] Skipping feedback pattern: {src}.{src_attr} -> {dst}.{dst_attr}")
            continue

        clean_connections.append((src, dst))

    if debug:
        print(f"[AUTO_LAYOUT] Using {len(clean_connections)} connections for layout")

    # --- Topological sort → assign depth levels ---
    adj       = {n: [] for n in nodes}
    in_degree = {n: 0  for n in nodes}

    for src, dst in clean_connections:
        if src in adj and dst in adj:
            adj[src].append(dst)
            in_degree[dst] += 1

    queue = deque([n for n in nodes if in_degree[n] == 0])
    if not queue and nodes:
        queue.append(nodes[0])

    levels = {}
    level  = 0
    while queue:
        next_queue = deque()
        for _ in range(len(queue)):
            node = queue.popleft()
            if node not in levels:
                levels[node] = level
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_queue.append(neighbor)
        queue  = next_queue
        level += 1

    for node in nodes:
        if node not in levels:
            levels[node] = 0
            if debug:
                print(f"[AUTO_LAYOUT] Node {node} unleveled (cycle/disconnected), assigning level 0")

    # --- Group by level ---
    level_groups: dict = {}
    for node, lvl in levels.items():
        level_groups.setdefault(lvl, []).append(node)

    # --- Collect actual node sizes from DPG ---
    node_sizes: dict = {}
    fallback_w = render_scale.layout_horizontal_spacing()
    fallback_h = render_scale.layout_vertical_spacing()

    for node_id in nodes:
        dpg_id = uuid_to_dpg.get(node_id)
        if dpg_id and dpg.does_item_exist(dpg_id):
            try:
                w, h = dpg.get_item_rect_size(dpg_id)
                node_sizes[node_id] = (w if w > 0 else fallback_w,
                                       h if h > 0 else fallback_h)
            except Exception:
                node_sizes[node_id] = (fallback_w, fallback_h)
        else:
            node_sizes[node_id] = (fallback_w, fallback_h)

    # --- Spacing constants ---
    pad_x  = render_scale.layout_horizontal_spacing()
    pad_y  = render_scale.layout_vertical_spacing()
    base_x = render_scale.auto_layout_base_x()
    base_y = render_scale.auto_layout_base_y()

    # --- Compute column x-positions ---
    sorted_levels = sorted(level_groups.keys())

    col_x: dict = {}
    cursor_x = base_x
    for lvl in sorted_levels:
        col_x[lvl] = cursor_x
        max_w = max(node_sizes[n][0] for n in level_groups[lvl])
        cursor_x += max_w + pad_x

    # --- Position each node ---
    positioned = 0
    for lvl in sorted_levels:
        nodes_in_level = level_groups[lvl]
        x        = col_x[lvl]
        cursor_y = base_y

        for node_id in nodes_in_level:
            dpg_id = uuid_to_dpg.get(node_id)
            if not dpg_id or not dpg.does_item_exist(dpg_id):
                if debug:
                    print(f"[AUTO_LAYOUT] Skipping {node_id}: no valid DPG ID")
                continue

            w, h = node_sizes[node_id]
            node_name = graph.nodes[node_id].get('name', node_id[:4])

            if debug:
                print(f"[AUTO_LAYOUT] [{lvl}] {node_name} → ({x:.0f}, {cursor_y:.0f})  size=({w:.0f}x{h:.0f})")

            dpg.set_item_pos(dpg_id, [x, cursor_y])
            cursor_y += h + pad_y
            positioned += 1

    if debug:
        print(f"[AUTO_LAYOUT] Done. Positioned {positioned}/{len(nodes)} nodes across {len(sorted_levels)} columns")