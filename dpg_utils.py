import dearpygui.dearpygui as dpg
from collections import deque
    
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
    """Organize nodes into a grid layout, ignoring feedback loops and references."""
    if debug: print(f"[AUTO_LAYOUT] Starting auto layout with {len(graph.nodes)} nodes")
    
    if not graph.nodes:
        print("[AUTO_LAYOUT] No nodes to layout")
        return
    
    # Build dependency graph for topological sorting
    nodes = list(graph.nodes.keys())
    
    # Filter out connections that shouldn't affect layout
    # We need to access connection properties to check for delay
    clean_connections = []
    
    for conn in graph.connections:
        src, src_attr, dst, dst_attr = conn
        
        # Skip reference connections
        if dst_attr.endswith("_ref") or dst_attr == "layer_list" or "params" in dst_attr.lower():
            if debug: print(f"[AUTO_LAYOUT] Skipping reference connection: {src}.{src_attr} -> {dst}.{dst_attr}")
            continue
        
        # Check connection properties for delay
        conn_props = graph.connection_properties.get(conn, {})
        delay = conn_props.get('delay', 0)
        
        # Skip feedback connections (delay = -1)
        if delay == -1:
            if debug: print(f"[AUTO_LAYOUT] Skipping feedback connection (delay={delay}): {src}.{src_attr} -> {dst}.{dst_attr}")
            continue
        
        # Also check for feedback patterns in attribute names
        if ":-" in str(src_attr):
            if debug: print(f"[AUTO_LAYOUT] Skipping feedback connection (pattern): {src}.{src_attr} -> {dst}.{dst_attr}")
            continue
        
        clean_connections.append((src, dst))
        if debug: print(f"[AUTO_LAYOUT] Keeping connection for layout: {src} -> {dst}")
    
    if debug: print(f"[AUTO_LAYOUT] Using {len(clean_connections)} clean connections for dependency analysis")
    
    # Build adjacency lists and in-degree counts
    adj = {node: [] for node in nodes}
    in_degree = {node: 0 for node in nodes}
    
    for src, dst in clean_connections:
        if src in adj and dst in adj:
            adj[src].append(dst)
            in_degree[dst] += 1
    
    # Find nodes with no incoming edges
    queue = deque([node for node in nodes if in_degree[node] == 0])
    
    # If all nodes have dependencies, start with the first node
    if not queue and nodes:
        queue.append(nodes[0])
        if debug: print(f"[AUTO_LAYOUT] No nodes with zero in-degree, starting with {nodes[0]}")
    
    levels = {}
    level = 0
    
    # Process nodes level by level
    while queue:
        level_size = len(queue)
        next_queue = deque()
        
        for _ in range(level_size):
            node = queue.popleft()
            levels[node] = level
            
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_queue.append(neighbor)
        
        queue = next_queue
        level += 1
    
    # Assign level 0 to any remaining nodes (cycles or disconnected)
    for node in nodes:
        if node not in levels:
            levels[node] = 0
            if debug: print(f"[AUTO_LAYOUT] Node {node} had no level assigned, setting to 0")
    
    # Group nodes by level
    level_groups = {}
    for node, lvl in levels.items():
        if lvl not in level_groups:
            level_groups[lvl] = []
        level_groups[lvl].append(node)
    
    if debug: print(f"[AUTO_LAYOUT] Found {len(level_groups)} levels")
    for lvl, nodes_in_level in sorted(level_groups.items()):
        if debug: print(f"  Level {lvl}: {len(nodes_in_level)} nodes")
        for node in nodes_in_level:
            node_name = graph.nodes[node].get('name', node[:4])
            if debug: print(f"    - {node_name}")
    
    # Position nodes in a grid
    base_x = 50  # Starting X position
    base_y = 50  # Starting Y position
    horizontal_spacing = 450  # Space between columns
    vertical_spacing = 220    # Space between rows
    
    positioned_nodes = 0
    
    for level in sorted(level_groups.keys()):
        nodes_in_level = level_groups[level]
        num_nodes = len(nodes_in_level)
        
        # Calculate vertical positions
        for i, node_id in enumerate(nodes_in_level):
            dpg_id = uuid_to_dpg.get(node_id)
            if not dpg_id or not dpg.does_item_exist(dpg_id):
                if debug: print(f"[AUTO_LAYOUT] Warning: Node {node_id} has no valid DPG ID")
                continue
            
            # Calculate position - this is the original grid layout from your code
            x = 30 + level * 330
            y = 30 + i * 220
            
            node_name = graph.nodes[node_id].get('name', node_id[:4])
            if debug: print(f"[AUTO_LAYOUT] Positioning {node_name} at ({x}, {y})")
            
            dpg.set_item_pos(dpg_id, [x, y])
            positioned_nodes += 1
    
    if debug: print(f"[AUTO_LAYOUT] Layout complete. Positioned {positioned_nodes} nodes")
