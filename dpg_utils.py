import dearpygui.dearpygui as dpg
from collections import deque
import render_scale


def create_node_theme(
    node_background,
    node_outline,
    node_background_selected=None,  # "complete" themes only
    title_bar=None,                  # "incomplete" themes only
    title_bar_hovered=None,
    title_bar_selected=None,
    pin=None,                        # "incomplete" themes only
    pin_hovered=None,
    text=None,                       # "incomplete" themes only
    use_category: bool = False,      # True for "complete" themes
):
    """
    Generic node theme factory.

    Set use_category=True to pass category=dpg.mvThemeCat_Nodes on every
    color call (required by the "complete" data/proc themes).
    Components for pins and text are only emitted when their colors are given.
    """
    ckw = {"category": dpg.mvThemeCat_Nodes} if use_category else {}

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            if title_bar is not None:
                dpg.add_theme_color(dpg.mvNodeCol_TitleBar, title_bar, **ckw)
            if title_bar_hovered is not None:
                dpg.add_theme_color(dpg.mvNodeCol_TitleBarHovered, title_bar_hovered, **ckw)
            if title_bar_selected is not None:
                dpg.add_theme_color(dpg.mvNodeCol_TitleBarSelected, title_bar_selected, **ckw)
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackground, node_background, **ckw)
            if node_background_selected is not None:
                dpg.add_theme_color(dpg.mvNodeCol_NodeBackgroundSelected, node_background_selected, **ckw)
            dpg.add_theme_color(dpg.mvNodeCol_NodeOutline, node_outline, **ckw)

        if pin is not None or pin_hovered is not None:
            with dpg.theme_component(dpg.mvNodeAttribute):
                if pin is not None:
                    dpg.add_theme_color(dpg.mvNodeCol_Pin, pin, **ckw)
                if pin_hovered is not None:
                    dpg.add_theme_color(dpg.mvNodeCol_PinHovered, pin_hovered, **ckw)

        if text is not None:
            with dpg.theme_component(dpg.mvText):
                dpg.add_theme_color(dpg.mvThemeCol_Text, text, **ckw)

    return theme


def create_data_node_theme_incomplete():
    """Yellow theme for incomplete data nodes."""
    return create_node_theme(
        title_bar          = (200, 200, 100, 255),
        title_bar_hovered  = (220, 220, 120, 255),
        title_bar_selected = (240, 240, 140, 255),
        node_background    = (30,  30,  30,  200),
        node_outline       = (200, 200, 100, 128),
        pin                = (220, 220, 120, 255),
        pin_hovered        = (240, 240, 140, 255),
        text               = (255, 255, 220, 255),
    )


def create_proc_node_theme_incomplete():
    """Golden-yellow theme for incomplete processing nodes."""
    return create_node_theme(
        title_bar          = (220, 200,  80, 255),
        title_bar_hovered  = (240, 220, 100, 255),
        title_bar_selected = (255, 235, 120, 255),
        node_background    = ( 40,  30,  20, 200),
        node_outline       = (220, 200,  80, 128),
        pin                = (230, 210,  90, 255),
        pin_hovered        = (250, 230, 110, 255),
        text               = (255, 240, 200, 255),
    )


def create_data_node_theme():
    """Grey background, orange selection — complete data nodes."""
    return create_node_theme(
        node_background          = [60,  60,  60],
        node_background_selected = [155, 70,   0],
        node_outline             = [80,  80,  80],
        use_category             = True,
    )


def create_proc_node_theme():
    """Blue background, green selection — complete processing nodes."""
    return create_node_theme(
        node_background          = [40,  60,  90],
        node_background_selected = [50, 105,  50],
        node_outline             = [60,  90, 120],
        use_category             = True,
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
    """Fixes the file dialog alternating row colors."""
    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            # Table/File Dialog Fix
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, [45, 45, 45, 255])
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, [45, 45, 45, 255])
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, [60, 60, 60, 255])
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
            # Only set level if not already set (first-visit wins → shallowest level)
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
    level_groups: dict[int, list] = {}
    for node, lvl in levels.items():
        level_groups.setdefault(lvl, []).append(node)

    # --- Collect actual node sizes from DPG ---
    node_sizes: dict[str, tuple[float, float]] = {}   # uuid → (w, h)
    fallback_w = render_scale.layout_horizontal_spacing()
    fallback_h = render_scale.layout_vertical_spacing()

    for node_id in nodes:
        dpg_id = uuid_to_dpg.get(node_id)
        if dpg_id and dpg.does_item_exist(dpg_id):
            try:
                w, h = dpg.get_item_rect_size(dpg_id)
                # Guard against zero sizes (node not yet rendered)
                node_sizes[node_id] = (w if w > 0 else fallback_w,
                                       h if h > 0 else fallback_h)
            except Exception:
                node_sizes[node_id] = (fallback_w, fallback_h)
        else:
            node_sizes[node_id] = (fallback_w, fallback_h)

    # --- Spacing constants (padding between nodes, not total step) ---
    pad_x  = render_scale.layout_horizontal_spacing()   # horizontal gap between columns
    pad_y  = render_scale.layout_vertical_spacing()   # vertical gap between nodes
    base_x = render_scale.auto_layout_base_x()
    base_y = render_scale.auto_layout_base_y()

    # --- Compute column x-positions based on the widest node per column ---
    sorted_levels = sorted(level_groups.keys())

    col_x: dict[int, float] = {}   # level → left-edge x
    cursor_x = base_x
    for lvl in sorted_levels:
        col_x[lvl] = cursor_x
        max_w = max(node_sizes[n][0] for n in level_groups[lvl])
        cursor_x += max_w + pad_x

    # --- Position each node, stacking vertically with actual heights ---
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

