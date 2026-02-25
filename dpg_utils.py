import dearpygui.dearpygui as dpg


def create_data_node_theme_incomplete():
    """Yellow theme for incomplete data nodes."""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            dpg.add_theme_color(dpg.mvNodeCol_TitleBar, (200, 200, 100, 255))  # Yellow
            dpg.add_theme_color(dpg.mvNodeCol_TitleBarHovered, (220, 220, 120, 255))
            dpg.add_theme_color(dpg.mvNodeCol_TitleBarSelected, (240, 240, 140, 255))
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackground, (30, 30, 30, 200))
            dpg.add_theme_color(dpg.mvNodeCol_NodeOutline, (200, 200, 100, 128))
            
        with dpg.theme_component(dpg.mvNodeAttribute):
            dpg.add_theme_color(dpg.mvNodeCol_Pin, (220, 220, 120, 255))
            dpg.add_theme_color(dpg.mvNodeCol_PinHovered, (240, 240, 140, 255))
            
        with dpg.theme_component(dpg.mvText):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 220, 255))
            
    return theme

def create_proc_node_theme_incomplete():
    """Yellow theme for incomplete processing nodes."""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            dpg.add_theme_color(dpg.mvNodeCol_TitleBar, (220, 200, 80, 255))  # Golden yellow
            dpg.add_theme_color(dpg.mvNodeCol_TitleBarHovered, (240, 220, 100, 255))
            dpg.add_theme_color(dpg.mvNodeCol_TitleBarSelected, (255, 235, 120, 255))
            dpg.add_theme_color(dpg.mvNodeCol_NodeBackground, (40, 30, 20, 200))
            dpg.add_theme_color(dpg.mvNodeCol_NodeOutline, (220, 200, 80, 128))
            
        with dpg.theme_component(dpg.mvNodeAttribute):
            dpg.add_theme_color(dpg.mvNodeCol_Pin, (230, 210, 90, 255))
            dpg.add_theme_color(dpg.mvNodeCol_PinHovered, (250, 230, 110, 255))
            
        with dpg.theme_component(dpg.mvText):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 240, 200, 255))
            
    return theme

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
    """Organize nodes into a grid layout, ignoring feedback loops and references."""
    print(f"[AUTO_LAYOUT] Starting auto layout with {len(graph.nodes)} nodes")
    
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
            print(f"[AUTO_LAYOUT] Skipping reference connection: {src}.{src_attr} -> {dst}.{dst_attr}")
            continue
        
        # Check connection properties for delay
        conn_props = graph.connection_properties.get(conn, {})
        delay = conn_props.get('delay', 0)
        
        # Skip feedback connections (delay = -1)
        if delay == -1:
            print(f"[AUTO_LAYOUT] Skipping feedback connection (delay={delay}): {src}.{src_attr} -> {dst}.{dst_attr}")
            continue
        
        # Also check for feedback patterns in attribute names
        if ":-" in str(src_attr):
            print(f"[AUTO_LAYOUT] Skipping feedback connection (pattern): {src}.{src_attr} -> {dst}.{dst_attr}")
            continue
        
        clean_connections.append((src, dst))
        print(f"[AUTO_LAYOUT] Keeping connection for layout: {src} -> {dst}")
    
    print(f"[AUTO_LAYOUT] Using {len(clean_connections)} clean connections for dependency analysis")
    
    # Build adjacency lists and in-degree counts
    adj = {node: [] for node in nodes}
    in_degree = {node: 0 for node in nodes}
    
    for src, dst in clean_connections:
        if src in adj and dst in adj:
            adj[src].append(dst)
            in_degree[dst] += 1
    
    # Topological sort using Kahn's algorithm
    from collections import deque
    
    # Find nodes with no incoming edges
    queue = deque([node for node in nodes if in_degree[node] == 0])
    
    # If all nodes have dependencies, start with the first node
    if not queue and nodes:
        queue.append(nodes[0])
        print(f"[AUTO_LAYOUT] No nodes with zero in-degree, starting with {nodes[0]}")
    
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
            print(f"[AUTO_LAYOUT] Node {node} had no level assigned, setting to 0")
    
    # Group nodes by level
    level_groups = {}
    for node, lvl in levels.items():
        if lvl not in level_groups:
            level_groups[lvl] = []
        level_groups[lvl].append(node)
    
    print(f"[AUTO_LAYOUT] Found {len(level_groups)} levels")
    for lvl, nodes_in_level in sorted(level_groups.items()):
        print(f"  Level {lvl}: {len(nodes_in_level)} nodes")
        for node in nodes_in_level:
            node_name = graph.nodes[node].get('name', node[:4])
            print(f"    - {node_name}")
    
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
                print(f"[AUTO_LAYOUT] Warning: Node {node_id} has no valid DPG ID")
                continue
            
            # Calculate position - this is the original grid layout from your code
            x = 30 + level * 330
            y = 30 + i * 220
            
            node_name = graph.nodes[node_id].get('name', node_id[:4])
            print(f"[AUTO_LAYOUT] Positioning {node_name} at ({x}, {y})")
            
            dpg.set_item_pos(dpg_id, [x, y])
            positioned_nodes += 1
    
    print(f"[AUTO_LAYOUT] Layout complete. Positioned {positioned_nodes} nodes")
