import dearpygui.dearpygui as dpg

def create_data_node_theme():
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            # Grey background, Orange selection
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackground, [60, 60, 60], category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackgroundSelected, [155, 70, 0], category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_color(dpg.mvNodeCol_NodeOutline, [80, 80, 80], category=dpg.mvThemeCat_Nodes)
    return theme

def create_proc_node_theme():
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            # Blue background, Green selection
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackground, [40, 60, 90], category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackgroundSelected, [50, 105, 50], category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_color(dpg.mvNodeCol_NodeOutline, [60, 90, 120], category=dpg.mvThemeCat_Nodes)
    return theme

def apply_feedback_link_style(link_id):
    """Sets the visual style for feedback links (Solid Red)."""
    with dpg.theme() as link_theme:
        with dpg.theme_component(dpg.mvNodeLink):
            # Solid Bright Red
            dpg.add_theme_color(dpg.mvNodeCol_Link, [255, 0, 0, 255], category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_style(dpg.mvNodeStyleVar_LinkThickness, 1.0, category=dpg.mvThemeCat_Nodes)
            
    dpg.bind_item_theme(link_id, link_theme)

def apply_ref_link_style(link_id):
    """Sets the visual style for reference links (thin/subtle)."""
    with dpg.theme() as link_theme:
        with dpg.theme_component(dpg.mvNodeLink):
            # Semi-transparent light gray
            dpg.add_theme_color(dpg.mvNodeCol_Link, [200, 200, 200, 60], category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_style(dpg.mvNodeStyleVar_LinkThickness, 1.0, category=dpg.mvThemeCat_Nodes)
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

def auto_layout_nodes(graph, uuid_to_dpg):
    """Organize nodes into layers, ignoring feedback loops and references."""
    levels = {}
    nodes = list(graph.nodes.keys())
    
    # 1. Filter connections to remove layout-breaking cycles
    # We ignore: 1. References (_ref) 2. Feedback indices (:-1)
    clean_connections = [
        (src, src_at, dst, dst_at) for src, src_at, dst, dst_at in graph.connections
        if not (dst_at.endswith("_ref") or ":-" in src_at or "params" in dst_at.lower())
    ]

    # 2. Topological Layering (Breadth-First approach)
    # Start with nodes that have no 'clean' inputs
    queue = []
    for node_id in nodes:
        parents = [c[0] for c in clean_connections if c[2] == node_id]
        if not parents:
            levels[node_id] = 0
            queue.append(node_id)

    # Iteratively assign levels to children
    while queue:
        u = queue.pop(0)
        children = [c[2] for c in clean_connections if c[0] == u]
        for v in children:
            # Level is the maximum depth of all parents + 1
            new_lvl = levels[u] + 1
            if v not in levels or new_lvl > levels[v]:
                levels[v] = new_lvl
                queue.append(v)

    # Handle 'orphan' nodes that might have been skipped due to remaining cycles
    for node_id in nodes:
        if node_id not in levels:
            levels[node_id] = 0

    # 3. Apply positions to DPG items
    lvl_counts = {}
    for u_id, lvl in levels.items():
        dpg_id = uuid_to_dpg.get(u_id)
        if not dpg_id or not dpg.does_item_exist(dpg_id): 
            continue
        
        row = lvl_counts.get(lvl, 0)
        # spacing: 450px Horizontal, 300px Vertical
        dpg.set_item_pos(dpg_id, [30 + lvl * 330, 30 + row * 220])
        lvl_counts[lvl] = row + 1