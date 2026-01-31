import gc
import numpy as np
import threading
import time
from queue import Queue
import socketio
import uuid
import os
import dearpygui.dearpygui as dpg
from dpg_utils import apply_ref_link_style, apply_feedback_link_style, create_data_node_theme, create_proc_node_theme
from dpg_plotting import DPGPlotter

# Reference shapes (Squares) vs Data shapes (Circles)
REF_SHAPE = dpg.mvNode_PinShape_QuadFilled
DATA_SHAPE = dpg.mvNode_PinShape_CircleFilled

DEFAULT_PARAM_COLOR = [110, 110, 110]
MODIFIED_PARAM_COLOR = [240, 240, 240]

class NodeManager:
    def __init__(self, graph_manager, all_templates, socketio_server='http://127.0.0.1:5000'):
        
        self.graph = graph_manager
        self.all_templates = all_templates
        self.simple_displays = {}

        # Registries
        self.dpg_to_uuid = {}
        self.uuid_to_dpg = {}
        self.input_attr_registry = {}
        self.output_attr_registry = {}
        self.link_registry = {}
        self._last_selected_uuid = None
        self.data_theme = None
        self.proc_theme = None
        self.class_name_counters = {}
        self.node_item_registry = {}

        self._selected_link_id = None

        # Socket.IO client

        if os.name == 'nt':  # Windows
            # Use different transport for Windows
            self.sio = socketio.Client(
                logger=True, 
                engineio_logger=True,  # Enable engineio logging
                reconnection=True,
                reconnection_attempts=5,
                reconnection_delay=1,
                reconnection_delay_max=5,
                randomization_factor=0.5
            )
        else:
            self.sio = socketio.Client(logger=True, engineio_logger=False)

        self.status_update_queue = Queue(maxsize=50)

        self.monitor_lock = threading.Lock()

        self.socketio_server = socketio_server
        self.socketio_connected = False
        self.socketio_enabled = True
        
        # Server state
        self.server_params = {}  # Parameters received from server
        self.server_nodes = {}   # Map: server_node_name -> {info}
        self.uuid_to_server_name = {}  # Map: our_uuid -> server_name
        
        # Setup Socket.IO event handlers
        #self._setup_socketio_handlers()
        
        # Monitor tracking
        self.active_monitors = {}  # {server_output_name: monitor_info}
        self.subscribed_outputs = set()  # Set of outputs we're subscribed to
        self.monitor_data_queue = Queue(maxsize=100)  # Limit to 100 items

        self.monitor_running = False                
        
        # Debug
        self.debug = True

        # Setup Socket.IO event handlers BEFORE connecting
        self._setup_socketio_handlers()
    
        # Try to connect immediately
        self._connect_socketio()

    # Add this method to NodeManager class:
    def _on_link_click(self, sender, app_data, user_data):
        """Callback when a link is clicked to select it."""
        link_id = user_data
        
        # Deselect previous link if any
        if self._selected_link_id and self._selected_link_id != link_id:
            # Reset previous link style
            self._reset_link_style(self._selected_link_id)
        
        # Select new link
        self._selected_link_id = link_id
        self._highlight_link(link_id)
        
        # Clear node selection when link is selected
        dpg.clear_selected_nodes("specula_editor")
        self._last_selected_uuid = None
        print(f"[LINK] Selected link: {link_id}")

    def _highlight_link(self, link_id):
        """Highlight a link to show it's selected."""
        if dpg.does_item_exist(link_id):
            # Change link color to yellow to indicate selection
            dpg.configure_item(link_id, color=[255, 255, 0, 255])

    def _reset_link_style(self, link_id):
        """Reset link style to normal based on its type."""
        if not dpg.does_item_exist(link_id):
            return
        
        # Get connection data to determine link type
        if link_id in self.link_registry:
            src_uuid, src_attr, dst_uuid, dst_attr = self.link_registry[link_id]
            
            # Check what type of link this is and reapply appropriate style
            if dst_attr.endswith("_ref") or "params" in dst_attr.lower():
                apply_ref_link_style(link_id)
            elif ":-" in str(src_attr):
                apply_feedback_link_style(link_id)
            else:
                # Default link style
                dpg.configure_item(link_id, color=[255, 255, 255, 255])

    def _clear_link_selection(self):
        """Clear link selection."""
        if self._selected_link_id:
            self._reset_link_style(self._selected_link_id)
            self._selected_link_id = None

    # Update the on_click_editor method to handle link clicks:
    def on_click_editor(self, sender, app_data):
        """Check selection and update property panel, also handle link clicks."""
        # Clear link selection when clicking on canvas
        if dpg.is_item_hovered("specula_editor"):
            # Check if we clicked on empty space (not a link or node)
            clicked_on_something = False
            
            # Check if we clicked on a link
            for link_id in self.link_registry:
                if dpg.is_item_hovered(link_id):
                    # This will be handled by the link click handler
                    clicked_on_something = True
                    break
            
            # If clicked on empty canvas, clear link selection
            if not clicked_on_something:
                self._clear_link_selection()
        
        # Original node selection logic
        selected = self.get_selected_nodes()
        
        if len(selected) == 1:
            uuid = selected[0]
            if uuid != self._last_selected_uuid:
                self._last_selected_uuid = uuid
                # Clear link selection when selecting a node
                self._clear_link_selection()
                # Update your panel tag (ensure 'property_panel' tag exists in your UI layout)
                self.update_property_panel(uuid, "property_panel")
        elif len(selected) == 0:
            # Clear panel if nothing selected
            dpg.delete_item("property_panel", children_only=True)
            self._last_selected_uuid = None

    # Update the setup_handlers method to add link click handlers:
    def setup_handlers(self):
        with dpg.handler_registry():
            # Listen for clicks to update the property panel
            dpg.add_mouse_click_handler(callback=self.on_click_editor)            
            dpg.add_key_press_handler(key=dpg.mvKey_D, callback=self.delete_selected_link)
            # Listen for Delete key
            dpg.add_key_press_handler(dpg.mvKey_Delete, callback=self.delete_selection)
            
            # Add double-click handler for links (to simulate selection)
            dpg.add_mouse_double_click_handler(callback=self._on_canvas_double_click)
            
            # Add mouse move handler for link hover
            dpg.add_mouse_move_handler(callback=self._on_mouse_move)


    def _on_canvas_double_click(self, sender, app_data):
        """Handle double-clicks on the canvas to select links."""
        if not dpg.is_item_hovered("specula_editor"):
            return
        
        # Check if we double-clicked on a link
        for link_id in self.link_registry:
            if dpg.is_item_hovered(link_id):
                self._on_link_click(sender, app_data, link_id)
                break

    # Update the delete_selected_link method:
    def delete_selected_link(self, sender, app_data):
        """Delete the currently selected link."""
        if not self._selected_link_id:
            print("[LINK] No link selected to delete")
            return

        link_id = self._selected_link_id
        print(f"[LINK] Deleting selected link: {link_id}")

        # Call your existing logic
        self.delink_callback(sender, link_id)

        # Clear selection
        self._selected_link_id = None

    def _on_mouse_move(self, sender, app_data):
        """Handle mouse move to show link hover state."""
        if not dpg.is_item_hovered("specula_editor"):
            return
        
        # Check if mouse is over any link
        for link_id in self.link_registry:
            if dpg.is_item_hovered(link_id) and link_id != self._selected_link_id:
                # Highlight on hover (light yellow)
                dpg.configure_item(link_id)
                break

    def _generate_unique_name(self, class_name):
        """
        Generate a unique instance name like:
        a<ClassName><counter>
        """
        if class_name not in self.class_name_counters:
            self.class_name_counters[class_name] = 0

        counter = self.class_name_counters[class_name]
        name = f"a{class_name}{counter}"

        self.class_name_counters[class_name] += 1
        return name

    def after_dpg_init(self):
        """Call this after DPG is fully initialized and the main loop is running."""
        print(f"[NODE_MANAGER] DPG initialized, setting up periodic tasks")
        
        # Start periodic tasks after a short delay
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 100, self.start_periodic_tasks)
        
        # Start monitor updater if needed
        #if self.active_monitors and not self.monitor_running:
        self._start_monitor_updater()


    def _log(self, message: str):
        """Log message if debug is enabled."""
        if self.debug:
            print(f"[NODE_MANAGER] {message}")


    def _safe_update_monitor_status(self, monitor_id, status):
        """Safely update monitor status via queue."""
        try:
            if self.status_update_queue.full():
                # Remove oldest if full
                try:
                    self.status_update_queue.get_nowait()
                except:
                    pass
            
            self.status_update_queue.put({
                'type': 'status_update',
                'monitor_id': monitor_id,
                'status': status,
                'timestamp': time.time()
            })
        except:
            pass
   
        
    def _setup_socketio_handlers(self):
        """Setup Socket.IO event handlers."""
        @self.sio.event
        def any_event(event, data):
            """Catch all events to see what's being received."""
            if event not in ['ping', 'pong']:  # Filter out ping/pong spam
                print(f"[SOCKET.IO] ⚡ ANY EVENT: {event} -> {type(data)}")
                if isinstance(data, dict):
                    print(f"    Keys: {list(data.keys())}")
        
        @self.sio.event
        def connect():
            self.socketio_connected = True
            print(f"[SOCKET.IO] ✓ Connected! SID: {self.sio.sid}")
            
            # Queue a status update
            self.monitor_data_queue.put({
                'type': 'status_update',
                'status': 'connected'
            })
            
            # DEBUG: Check if we can receive events
            print(f"[SOCKET.IO] Testing event reception...")
            
            # Send a test request for params
            try:
                self.sio.emit('get_params')
                print(f"[SOCKET.IO] Requested params via 'get_params'")
            except:
                # Fallback: the server should send params automatically on connect
                print(f"[SOCKET.IO] Server should auto-send params on connect")
       

        @self.sio.event
        def params(data):
            """Handle parameters event."""
            print(f"\n[SOCKET.IO] ⚡ PARAMS EVENT FIRED!")
            print(f"[SOCKET.IO] Received {len(data)} nodes")
            
            if not data:
                print(f"[SOCKET.IO] ❌ No data in params event!")
                return
            
            # Store server parameters
            self.server_params = data
            self.server_nodes = data
            
            print(f"[SOCKET.IO] First few nodes:")
            for i, (name, info) in enumerate(list(data.items())[:3]):
                node_class = info.get('class', 'Unknown')
                outputs = info.get('outputs', [])
                print(f"  {i+1}. {name} ({node_class}): {outputs}")
            
            # Try to map our UUIDs to server node names
            self._update_uuid_mapping()
            
            # Update monitor displays
            for monitor_id in self.active_monitors:
                self._safe_update_monitor_status(monitor_id, "connected")


        @self.sio.event
        def data_update(data):
            """Handle raw data updates from server."""
            print(f"\n[SOCKET.IO] ⚡ DATA_UPDATE EVENT FIRED!")
            print(f"[SOCKET.IO] Event received: {data.get('name', 'unknown')}")
            
            try:
                name = data.get('name')  # This is the server_output_name from server
                raw_data = data.get('data')
                
                if not name or raw_data is None:
                    print(f"[SOCKET.IO] Missing name or data in update")
                    return
                
                # Check queue size
                qsize = self.monitor_data_queue.qsize()
                print(f"[SOCKET.IO] Current queue size: {qsize}")
                
                if qsize >= 100:
                    print(f"[SOCKET.IO] Queue full (100), dropping data for {name}")
                    return
                
                # Use lock to safely access active_monitors
                with self.monitor_lock:
                    # Find ALL monitors that match this server output name
                    matching_monitors = []
                    for monitor_id, info in self.active_monitors.items():
                        if info.get('server_output_name') == name:
                            matching_monitors.append((monitor_id, info))
                    
                    if not matching_monitors:
                        print(f"[SOCKET.IO] ❌ No monitor found for {name}")
                        return
                    
                    print(f"[SOCKET.IO] Found {len(matching_monitors)} monitor(s) for {name}")
                    
                    # Add data to queue for each matching monitor
                    for monitor_id, info in matching_monitors:
                        self.monitor_data_queue.put({
                            'type': 'data_update',
                            'monitor_id': monitor_id,
                            'data': raw_data,
                            'timestamp': time.time()
                        })
                        print(f"[SOCKET.IO] ✅ Queued data for monitor {monitor_id}")
                        
            except Exception as e:
                print(f"[SOCKET.IO] ✗ Error in data_update handler: {e}")
                import traceback
                traceback.print_exc()
        
        @self.sio.event
        def connect_error(data):
            self.socketio_connected = False
            self._log(f"✗ Socket.IO connection error: {data}")
            print(f"[SOCKET.IO] Connection error: {data}")
            
            for monitor_id in self.active_monitors:
                self._safe_update_monitor_status(monitor_id, "disconnected")
        
        @self.sio.event
        def disconnect():
            self.socketio_connected = False
            self._log("✗ Socket.IO disconnected")
            print(f"[SOCKET.IO] Disconnected")
            
            for monitor_id in self.active_monitors:
                self._safe_update_monitor_status(monitor_id, "disconnected")
            
        @self.sio.event
        def speed_report(data):
            """Handle speed report events."""
            self._log(f"Speed report: {data}")
            print(f"[SOCKET.IO] Speed report: {data}")
        
        @self.sio.event
        def done(data):
            """Handle done events."""
            self._log(f"Frame done: {data}")
            print(f"[SOCKET.IO] Done event: {data}")
            
            # Request next data frame for all subscribed outputs
            if self.subscribed_outputs:
                print(f"[SOCKET.IO] Requesting next data frame for {len(self.subscribed_outputs)} outputs")
                self._request_next_frame()

    def _request_next_frame(self):
        """Request next data frame for subscribed outputs."""
        if not self.socketio_connected:
            print(f"[SOCKET.IO] Not connected, cannot request data")
            return
        
        with self.monitor_lock:
            if not self.subscribed_outputs:
                print(f"[SOCKET.IO] No outputs subscribed")
                return
            
            # Convert set to list for Socket.IO
            outputs_list = list(self.subscribed_outputs)
        
        print(f"[SOCKET.IO] Emitting 'newdata' for next frame: {outputs_list}")
        
        try:
            self.sio.emit('newdata', outputs_list)
            print(f"[SOCKET.IO] 'newdata' event emitted successfully")
        except Exception as e:
            self._log(f"✗ Error requesting data: {e}")
            print(f"[SOCKET.IO] Error emitting 'newdata': {e}")

    def _update_uuid_mapping(self):
        """Try to map our node UUIDs to server node names."""
        print(f"[MAPPING] Updating UUID to server name mapping")
        
        for node_uuid, node_data in self.graph.nodes.items():
            node_type = node_data.get('type', '')
            node_name = node_data.get('name', '')
            
            # Try to find matching server node
            for server_name, server_data in self.server_params.items():
                server_class = server_data.get('class', '')
                
                # Match by class name (most reliable)
                if node_type == server_class:
                    self.uuid_to_server_name[node_uuid] = server_name
                    print(f"[MAPPING] Mapped {node_uuid} ({node_type}) -> {server_name}")
                    break
                
                # Try by node name
                elif node_name.lower() == server_name.lower():
                    self.uuid_to_server_name[node_uuid] = server_name
                    print(f"[MAPPING] Mapped {node_uuid} (name: {node_name}) -> {server_name}")
                    break
        
        print(f"[MAPPING] Mapped {len(self.uuid_to_server_name)} nodes")
    
    def _get_server_output_name(self, node_uuid, output_name):
        """Get the server's output name format."""
        server_name = self.uuid_to_server_name.get(node_uuid)
        
        if not server_name:
            # Try to guess from node type
            node_data = self.graph.nodes.get(node_uuid, {})
            node_type = node_data.get('type', '')
            
            # Common mappings from your params - UPDATED with correct names
            type_to_name = {
                'PSF': 'psf',
                'AtmoPropagation': 'prop',
                'ModulatedPyramid': 'pyramid', 
                'ModalAnalysis': 'modal_analysis',
                'CCD': 'detector',
                'PyrSlopec': 'slopec',
                'Modalrec': 'rec',
                'Integrator': 'control',
                'DM': 'dm',
                'AtmoEvolution': 'atmo'
            }
            
            server_name = type_to_name.get(node_type)
            if server_name:
                self.uuid_to_server_name[node_uuid] = server_name
                print(f"[MAPPING] Guessed server name for {node_uuid} ({node_type}): {server_name}")
            else:
                # If we can't map, use the node type in lowercase as fallback
                server_name = node_type.lower() if node_type else f"node_{node_uuid[:4]}"
                self.uuid_to_server_name[node_uuid] = server_name
                print(f"[MAPPING] Using fallback server name for {node_uuid}: {server_name}")
        
        # Format: server_name.output_name
        if server_name and output_name:
            return f"{server_name}.{output_name}"
        else:
            # Return a safe default if we can't form a proper name
            return f"unknown.{output_name or 'output'}"

    def _update_data_info(self, info, data):
        """Update data information panel."""
        try:
            if data is None:
                return
                
            # Type
            data_type = type(data).__name__
            if isinstance(data, np.ndarray):
                data_type = f"ndarray ({data.dtype})"
            
            if dpg.does_item_exist(info.get('data_type_text_id', '')):
                dpg.set_value(info['data_type_text_id'], f"Type: {data_type}")
            
            # Shape
            if hasattr(data, 'shape'):
                shape_str = str(data.shape)
                size_str = f"{data.size} elements"
            else:
                shape_str = "scalar"
                size_str = "1 element"
            
            if dpg.does_item_exist(info.get('data_shape_text_id', '')):
                dpg.set_value(info['data_shape_text_id'], f"Shape: {shape_str}")
            
            if dpg.does_item_exist(info.get('data_size_text_id', '')):
                dpg.set_value(info['data_size_text_id'], f"Size: {size_str}")
            
            # Range
            if isinstance(data, np.ndarray) and data.size > 0 and np.issubdtype(data.dtype, np.number):
                range_str = f"[{data.min():.3g}, {data.max():.3g}]"
            elif isinstance(data, (int, float)):
                range_str = f"{data:.3g}"
            else:
                range_str = "N/A"
            
            if dpg.does_item_exist(info.get('data_range_text_id', '')):
                dpg.set_value(info['data_range_text_id'], f"Range: {range_str}")
                
        except Exception as e:
            print(f"[INFO] Error updating data info: {e}")

    def _update_monitor_status(self, monitor_id, status):
        """Update monitor status display."""
        if monitor_id not in self.active_monitors:
            return
        
        info = self.active_monitors[monitor_id]
        window_tag = info.get('window_id', '')
        
        if not window_tag or not dpg.does_item_exist(f"{window_tag}_status"):
            return
        
        colors = {
            'connected': [0, 255, 0],
            'disconnected': [255, 0, 0],
            'subscribed': [100, 255, 100],
            'unsubscribed': [255, 180, 100],
            'receiving': [0, 200, 255]
        }
        
        symbols = {
            'connected': '●',
            'disconnected': '○',
            'subscribed': '▶',
            'unsubscribed': '⏸',
            'receiving': '▼'
        }
        
        color = colors.get(status, [200, 200, 200])
        symbol = symbols.get(status, '○')
        
        dpg.set_value(f"{window_tag}_status", f"{symbol} {status.capitalize()}")
        dpg.configure_item(f"{window_tag}_status", color=color)


    def _open_output_monitor(self, sender, app_data, user_data):
        """Open a monitor window for an output with local plotting."""
        node_uuid, output_name = user_data
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data: 
            self._log(f"Monitor: Node {node_uuid} not found")
            return

        node_name = node_data.get('name', 'Unknown')
        
        # Get server output name
        server_output_name = self._get_server_output_name(node_uuid, output_name)
        
        if not server_output_name:
            # Fallback to a safe name
            server_output_name = f"node_{node_uuid[:4]}.{output_name}"
            print(f"[MONITOR] Using fallback server output name: {server_output_name}")
        
        # Create a unique monitor ID
        monitor_id = f"{node_uuid}_{output_name}_{int(time.time()*1000)}"
        window_tag = f"monitor_{monitor_id}"

        print(f"[MONITOR] Opening monitor for {server_output_name}, ID: {monitor_id}")

        # If window exists, focus it
        if dpg.does_item_exist(window_tag):
            dpg.focus_item(window_tag)
            return

        # Define the close callback
        def close_callback():
            print(f"[MONITOR] Close callback triggered for {monitor_id}")
            # Pass from_window_close=True since this is called from DPG window close
            self._close_monitor(monitor_id, from_window_close=True)

        # Create window with plot container
        with dpg.window(
            label=f"Monitor: {node_name}.{output_name}", 
            tag=window_tag, 
            width=800, 
            height=600, 
            pos=[300, 300],
            on_close=close_callback  # Use the named function instead of lambda
        ):
            # Connection Status Panel
            with dpg.collapsing_header(label="Connection Status", default_open=True):
                with dpg.group(tag=f"conn_{window_tag}"):
                    dpg.add_text("Server URL:", color=[200, 200, 200])
                    dpg.add_text(self.socketio_server, 
                            color=[100, 255, 255], tag=f"{window_tag}_url")
                    
                    dpg.add_text("Status:", color=[200, 200, 200])
                    status_text = dpg.add_text("○ Disconnected", 
                                            color=[255, 0, 0], 
                                            tag=f"{window_tag}_status")
                    
                    dpg.add_text("Server Output Name:", color=[200, 200, 200])
                    dpg.add_text(server_output_name, color=[100, 255, 100], tag=f"{window_tag}_output")
            
            # Plot Container - where DPGPlotter will render
            dpg.add_separator()
            dpg.add_text("Data Plot", color=[200, 200, 255])
            
            # Create plot container group
            plot_container = dpg.add_group(tag=f"{window_tag}_plot_container")
            
            # Add placeholder text
            placeholder = dpg.add_text("Waiting for data...", 
                                    color=[150, 150, 150], 
                                    parent=plot_container,
                                    tag=f"{window_tag}_placeholder")
            
            # Data info
            dpg.add_separator()
            with dpg.group(tag=f"info_{window_tag}"):
                dpg.add_text("Data Information:", color=[200, 200, 255])
                data_type_text = dpg.add_text("Type: Waiting for data...", 
                                            color=[200, 200, 200])
                data_shape_text = dpg.add_text("Shape: Unknown", 
                                            color=[200, 200, 200])
                data_size_text = dpg.add_text("Size: Unknown", 
                                            color=[200, 200, 200])
                data_range_text = dpg.add_text("Range: Unknown", 
                                            color=[200, 200, 200])
                update_time_text = dpg.add_text("Last Update: Never", 
                                            color=[200, 200, 200])
        
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Reconnect",
                    callback=lambda: self._connect_socketio(),
                    width=100
                )
                dpg.add_button(
                    label="Subscribe",
                    callback=lambda s, a, u=server_output_name: self._subscribe_output(u),
                    width=100
                )                        
                        

        # Register monitor
        self.active_monitors[monitor_id] = {
            'window_id': window_tag,
            'plot_container': plot_container,
            'placeholder_tag': placeholder,
            'data_type_text_id': data_type_text,
            'data_shape_text_id': data_shape_text,
            'data_size_text_id': data_size_text,
            'data_range_text_id': data_range_text,
            'update_time_text_id': update_time_text,
            'status_text_id': status_text,
            'node_uuid': node_uuid,
            'output_name': output_name,
            'server_output_name': server_output_name,  # This is what the server sends
            'node_name': node_name,
            'update_count': 0,
            'last_update': 0,
            'socketio_connected': False,
            'subscribed': False,
            'dpg_plotter': None,
            'min_update_interval': 0.1,
            'pending_data': None,
            'skipped_frames': 0
        }

        print(f"[MONITOR] Registered monitor ID: {monitor_id} for {server_output_name}")
        
        # Start monitor updater if not already running
        if not self.monitor_running:
            self._start_monitor_updater()
        
        # Try to connect and subscribe
        if not self.socketio_connected:
            self._connect_socketio()
        else:
            # Subscribe to this output
            self._subscribe_output(server_output_name)
       
        #if not self.monitor_running:
        self.monitor_running = True
        print(f"[MONITOR] Starting monitor updater")
        # Schedule first update in next frame
        dpg.set_frame_callback(dpg.get_frame_count() + 1, self._monitor_update_frame)


    def _close_monitor(self, monitor_id, from_window_close=True):
        """Cleanup when a monitor window is closed.
        
        Args:
            monitor_id: ID of the monitor to close
            from_window_close: If True, called from window close callback (don't delete window)
        """
        print(f"\n[CLOSE_MONITOR] Called for monitor_id: {monitor_id}")
        
        # Use lock to safely modify active_monitors
        with self.monitor_lock:
            if monitor_id not in self.active_monitors:
                print(f"[CLOSE_MONITOR] Monitor {monitor_id} not found in active_monitors")
                return
            
            info = self.active_monitors[monitor_id]
            server_output_name = info.get('server_output_name')
            window_tag = info.get('window_id', '')
            
            print(f"[CLOSE_MONITOR] Closing monitor {monitor_id} for {server_output_name}")
            print(f"[CLOSE_MONITOR] Node: {info.get('node_name', 'Unknown')}.{info.get('output_name', 'Unknown')}")
            
            # Clean up DPGPlotter
            plotter = info.get('dpg_plotter')
            if plotter:
                plotter.clear()
                print(f"[CLOSE_MONITOR] Cleared DPGPlotter")
            
            # Remove from active monitors BEFORE deleting DPG items
            del self.active_monitors[monitor_id]
            print(f"[CLOSE_MONITOR] Removed from active_monitors")
        
        # Only delete DPG window if we're NOT called from window close callback
        # (DPG will handle window deletion when user clicks close)
        if not from_window_close and window_tag and dpg.does_item_exist(window_tag):
            print(f"[CLOSE_MONITOR] Deleting DPG window: {window_tag}")
            dpg.delete_item(window_tag)
        
        # Clean up SimpleImageDisplay (after lock released)
        if monitor_id in self.simple_displays:
            self.simple_displays[monitor_id].cleanup()
            del self.simple_displays[monitor_id]
            print(f"[CLOSE_MONITOR] Cleaned up SimpleImageDisplay")
        
        # UNSUBSCRIBE FROM SERVER
        if server_output_name:
            print(f"[CLOSE_MONITOR] Calling _unsubscribe_output for {server_output_name}")
            self._unsubscribe_output(server_output_name)
        else:
            print(f"[CLOSE_MONITOR] No server_output_name found")
        
        # Clear any queued data for this monitor
        self._clear_queue_for_monitor(monitor_id)
        
        # Stop updater if no monitors left
        with self.monitor_lock:
            if not self.active_monitors and self.monitor_running:
                print(f"[CLOSE_MONITOR] No active monitors, stopping updater")
                self.monitor_running = False
        

    def _clear_queue_for_monitor(self, monitor_id):
        """Clear queued data for a specific monitor."""
        temp_queue = Queue()
        queue_items_removed = 0
        
        while not self.monitor_data_queue.empty():
            try:
                item = self.monitor_data_queue.get_nowait()
                if item.get('monitor_id') != monitor_id:
                    temp_queue.put(item)
                else:
                    queue_items_removed += 1
            except:
                break
        
        # Put back non-matching items
        while not temp_queue.empty():
            try:
                self.monitor_data_queue.put(temp_queue.get_nowait())
            except:
                break
        
        print(f"[CLOSE_MONITOR] Removed {queue_items_removed} items from queue")
        

    def _unsubscribe_output(self, server_output_name):
        """Unsubscribe from an output via Socket.IO."""
        print(f"[UNSUBSCRIPTION] Unsubscribing from {server_output_name}")
        
        if not server_output_name:
            print(f"[UNSUBSCRIPTION] Error: server_output_name is None!")
            return
        
        # Use lock to safely access active_monitors
        with self.monitor_lock:
            # Check if any other monitors are using this output
            monitors_using_output = [
                mid for mid, info in self.active_monitors.items()
                if info.get('server_output_name') == server_output_name
            ]
            
            # Only unsubscribe if no monitors are using this output
            if not monitors_using_output and server_output_name in self.subscribed_outputs:
                self.subscribed_outputs.remove(server_output_name)
                print(f"[UNSUBSCRIPTION] Removed {server_output_name} from subscribed set")
                
                # Tell server to stop sending this data
                if self.socketio_connected:
                    self._send_unsubscribe_request(server_output_name)
                
                # Update status for any remaining monitors (though there shouldn't be any)
                for monitor_id, info in self.active_monitors.items():
                    if info.get('server_output_name') == server_output_name:
                        info['subscribed'] = False
                        self._safe_update_monitor_status(monitor_id, "unsubscribed")
            else:
                print(f"[UNSUBSCRIPTION] {len(monitors_using_output)} monitor(s) still using {server_output_name}, keeping subscription")


    def _send_unsubscribe_request(self, server_output_name):
        """Send unsubscribe request to server."""
        if not self.socketio_connected:
            return
        
        try:
            print(f"[SOCKET.IO] Emitting 'unsubscribe' for {server_output_name}")
            self.sio.emit('unsubscribe', {'output': server_output_name})
        except Exception as e:
            print(f"[SOCKET.IO] Error sending unsubscribe: {e}")


    def _subscribe_output(self, server_output_name):
        """Subscribe to an output via Socket.IO."""
        print(f"[SUBSCRIPTION] Subscribing to {server_output_name}")
        
        if not server_output_name:
            print(f"[SUBSCRIPTION] Error: server_output_name is None!")
            return
        
        with self.monitor_lock:
            if server_output_name not in self.subscribed_outputs:
                self.subscribed_outputs.add(server_output_name)
                print(f"[SUBSCRIPTION] Added to subscribed set: {server_output_name}")
                
                # Update ALL monitors that use this server output
                for monitor_id, info in self.active_monitors.items():
                    if info.get('server_output_name') == server_output_name:
                        info['subscribed'] = True
                        self._safe_update_monitor_status(monitor_id, "subscribed")
                        print(f"[SUBSCRIPTION] Updated monitor {monitor_id}")
            else:
                print(f"[SUBSCRIPTION] Already subscribed to {server_output_name}")
        
        # Request data if connected
        if self.socketio_connected:
            print(f"[SUBSCRIPTION] Connected, requesting data")
            self._request_next_frame()
        else:
            print(f"[SUBSCRIPTION] Not connected, cannot request data")

   
    def _connect_socketio(self):
        """Connect to Socket.IO server."""
        if not self.socketio_enabled:
            return
        
        try:
            print(f"[SOCKET.IO] Connecting to {self.socketio_server}...")
            
            # Explicitly specify namespace
            self.sio.connect(self.socketio_server, namespaces=['/'])
            
            self.socketio_connected = True
            print(f"[SOCKET.IO] ✓ Connected! SID: {self.sio.sid}")
            
            # Test the connection immediately
            self.sio.emit('test_connection', {'client': 'node_editor'})
            
        except Exception as e:
            print(f"[SOCKET.IO] Connection failed: {e}")
            import traceback
            traceback.print_exc()
            self.socketio_connected = False


    def _process_and_plot_data_main_thread(self, monitor_id, raw_data, info):
        """Process raw data and plot locally."""

        try:
            # Double-check monitor still exists AND window exists
            with self.monitor_lock:
                if monitor_id not in self.active_monitors:
                    return False
            
            # Get window tag and check existence
            window_tag = info.get('window_id', '')
            if not window_tag or not dpg.does_item_exist(window_tag):
                print(f"[MAIN_PLOT] Window {window_tag} doesn't exist for monitor {monitor_id}")
                # Clean up
                with self.monitor_lock:
                    if monitor_id in self.active_monitors:
                        del self.active_monitors[monitor_id]
                return False
            
            # Get plot container
            plot_container = info.get('plot_container')
            if not plot_container or not dpg.does_item_exist(plot_container):
                print(f"[MAIN_PLOT] Plot container {plot_container} doesn't exist for monitor {monitor_id}")
                return False
            
            # Remove placeholder if it exists
            placeholder = info.get('placeholder_tag')
            if placeholder and dpg.does_item_exist(placeholder):
                dpg.delete_item(placeholder)
                info['placeholder_tag'] = None

            # Process data - raw_data should be the dict from server
            if not isinstance(raw_data, dict):
                print(f"[MAIN_PLOT] Raw data is not a dict: {type(raw_data)}")
                return False
            
            # Extract data based on server format
            data_type = raw_data.get('type')
            data_value = raw_data.get('data')
            
            if data_value is None:
                print(f"[MAIN_PLOT] No data in raw_data")
                return False
            
            # Convert to numpy array
            try:
                if isinstance(data_value, list):
                    data_array = np.array(data_value, dtype=np.float32)
                else:
                    # Might already be a numpy array or scalar
                    data_array = np.array([data_value], dtype=np.float32)
            except Exception as e:
                print(f"[MAIN_PLOT] Error converting data: {e}")
                return False
            
            # Reshape if shape is provided
            shape = raw_data.get('shape')
            if shape and len(shape) > 0 and np.prod(shape) == data_array.size:
                data_array = data_array.reshape(shape)
            
            print(f"[MAIN_PLOT] Processing data shape: {data_array.shape}")
            
            # Get or create DPGPlotter
            if info.get('dpg_plotter') is None:
                info['dpg_plotter'] = DPGPlotter(parent_tag=plot_container, width=780, height=400)
            
            plotter = info['dpg_plotter']
            
            # Plot based on shape
            success = False
            if data_array.ndim == 0 or (data_array.ndim == 1 and data_array.size == 1):
                # Scalar
                success = plotter.plot_history(float(data_array.item()))
                
            elif data_array.ndim == 1:
                # 1D Vector
                success = plotter.plot_vector(data_array)
                
            elif data_array.ndim == 2:
                # ADAPTIVE FRAME RATE: Adjust based on image size
                # Larger images need slower updates
                height, width = data_array.shape
                pixel_count = height * width
                
                if pixel_count > 1000000:  # > 1MP
                    info['min_update_interval'] = 0.5  # 2 FPS
                elif pixel_count > 250000:  # > 0.25MP
                    info['min_update_interval'] = 0.25  # 4 FPS
                elif pixel_count > 10000:   # > 10k pixels
                    info['min_update_interval'] = 0.1   # 10 FPS
                else:
                    info['min_update_interval'] = 0.05  # 20 FPS

                # 2D Matrix
                print(f"[MAIN_PLOT] Plotting 2D image {data_array.shape}")
                success = plotter.plot_2d_image_clean(data_array)
                
            elif data_array.ndim == 3:
                # 3D data - convert to 2D
                if data_array.shape[2] == 1:
                    data_2d = data_array[:, :, 0]
                else:
                    data_2d = np.mean(data_array[:, :, :3], axis=2)
                
                print(f"[MAIN_PLOT] Converting 3D to 2D: {data_2d.shape}")
                success = plotter.plot_2d_image_clean(data_2d)
            
            # Update info
            if success:
                self._update_data_info(info, data_array)
                current_time_str = time.strftime('%H:%M:%S')
                if dpg.does_item_exist(info.get('update_time_text_id', '')):
                    dpg.set_value(info['update_time_text_id'], f"Last Update: {current_time_str}")
                info['last_update'] = time.time()
                info['update_count'] = info.get('update_count', 0) + 1
                
                # self._update_monitor_status(monitor_id, "receiving")
                
                # Print update every 10 frames to reduce spam
                if info['update_count'] % 10 == 0:
                    print(f"[MAIN_PLOT] Updated {monitor_id} (#{info['update_count']})")
                
                return True
            else:
                print(f"[MAIN_PLOT] Failed to plot data for {monitor_id}")
                return False
                
        except Exception as e:
            print(f"[MAIN_PLOT] Error: {e}")
            import traceback
            traceback.print_exc()
            return False


    def _monitor_queue_health(self):
        """Monitor and maintain queue health."""
        try:
            qsize = self.monitor_data_queue.qsize()
            
            # If queue is getting too full, aggressively clear it
            if qsize > 50:
                print(f"[HEALTH] Queue overloaded ({qsize}), clearing old items...")
                
                # Keep only the latest item for each monitor
                latest_items = {}
                
                # Drain the queue
                temp_items = []
                while not self.monitor_data_queue.empty():
                    try:
                        item = self.monitor_data_queue.get_nowait()
                        if item.get('type') == 'data_update':
                            monitor_id = item.get('monitor_id')
                            # Keep only the latest for each monitor
                            latest_items[monitor_id] = item
                        else:
                            temp_items.append(item)  # Keep non-data items
                    except:
                        break
                
                # Put back latest items and non-data items
                for item in list(latest_items.values()) + temp_items:
                    try:
                        self.monitor_data_queue.put(item)
                    except:
                        break
                
                print(f"[HEALTH] Queue reduced from {qsize} to {self.monitor_data_queue.qsize()} items")
            
            # Schedule next health check
            next_frame = dpg.get_frame_count() + 60  # Check every second
            dpg.set_frame_callback(next_frame, self._monitor_queue_health)
            
        except Exception as e:
            print(f"[HEALTH] Error monitoring queue: {e}")
        

    def _find_and_close_monitor(self, monitor_info):
        """Find and close a monitor by node_uuid and output_name."""
        node_uuid, output_name = monitor_info
        
        # Find the monitor by node_uuid and output_name
        for monitor_id, info in list(self.active_monitors.items()):
            if info.get('node_uuid') == node_uuid and info.get('output_name') == output_name:
                self._close_monitor(monitor_id)
                break
            
    def start_periodic_tasks(self):
        """Start all periodic maintenance tasks."""
        current_frame = dpg.get_frame_count()
        print(f"[PERIODIC] Starting periodic tasks at frame {current_frame}")                
        dpg.set_frame_callback(current_frame + 100, self._periodic_cleanup)        
        dpg.set_frame_callback(current_frame + 100, self._check_memory_usage)                        
        dpg.set_frame_callback(current_frame + 100, self._monitor_queue_health)

        print(f"[PERIODIC] Periodic tasks scheduled")


    def _start_monitor_updater(self):
        """Start monitor update loop."""
        if not self.monitor_running:
            self.monitor_running = True
            print(f"[UPDATER] Starting monitor updater")
        
        # Schedule immediately and more frequently
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 1, self._monitor_update_frame)


    def _monitor_update_frame(self):
        """Simple update loop that processes ALL queued items every frame."""

        try:
            current_time = time.time()
            processed_count = 0
            skipped_count = 0
            
            # Get a snapshot of active monitors to avoid issues during iteration
            with self.monitor_lock:
                active_monitor_snapshot = set(self.active_monitors.keys())

            
            # Process status updates first
            while not self.status_update_queue.empty():
                try:
                    status_item = self.status_update_queue.get_nowait()
                    if status_item.get('type') == 'status_update':
                        monitor_id = status_item.get('monitor_id')
                        status = status_item.get('status')

                        if monitor_id not in active_monitor_snapshot:
                            print(f"[MONITOR] Skipping data for closed monitor {monitor_id}")
                            continue
                        
                        # Update without lock - UI thread only
                        if monitor_id in self.active_monitors:
                            info = self.active_monitors[monitor_id]
                            window_tag = info.get('window_id', '')
                            
                            if window_tag and dpg.does_item_exist(f"{window_tag}_status"):
                                colors = {
                                    'connected': [0, 255, 0],
                                    'disconnected': [255, 0, 0],
                                    'subscribed': [100, 255, 100],
                                    'unsubscribed': [255, 180, 100],
                                    'receiving': [0, 200, 255]
                                }
                                
                                symbols = {
                                    'connected': '●',
                                    'disconnected': '○',
                                    'subscribed': '▶',
                                    'unsubscribed': '⏸',
                                    'receiving': '▼'
                                }
                                
                                color = colors.get(status, [200, 200, 200])
                                symbol = symbols.get(status, '○')
                                
                                dpg.set_value(f"{window_tag}_status", f"{symbol} {status.capitalize()}")
                                dpg.configure_item(f"{window_tag}_status", color=color)
                except:
                    break
            
            # Process only a limited number of items per frame to prevent overload
            max_items_per_frame = 5
            

            for _ in range(max_items_per_frame):
                if self.monitor_data_queue.empty():
                    break
                    
                try:
                    item = self.monitor_data_queue.get_nowait()
                    
                    if item.get('type') == 'data_update':
                        monitor_id = item.get('monitor_id')
                        
                        # FIX: Use lock for both check and access
                        with self.monitor_lock:
                            if monitor_id not in self.active_monitors:
                                print(f"[MONITOR] Skipping data for closed monitor {monitor_id}")
                                continue
                            
                            info = self.active_monitors[monitor_id]
                        
                        # Get window_tag while still having valid info reference
                        window_tag = info.get('window_id', '')
                        
                        # Check if window still exists before processing
                        if not window_tag or not dpg.does_item_exist(window_tag):
                            print(f"[MONITOR] Window for {monitor_id} doesn't exist, skipping")
                            # Clean up from active_monitors
                            with self.monitor_lock:
                                if monitor_id in self.active_monitors:
                                    del self.active_monitors[monitor_id]
                            continue

                        
                        # Check if we should update based on time
                        time_since_update = current_time - info.get('last_update', 0)
                        min_interval = info.get('min_update_interval', 0.1)
                        
                        if time_since_update >= min_interval:
                            # Process immediately
                            raw_data = item.get('data', {})
                            success = self._process_and_plot_data_main_thread(
                                monitor_id, raw_data, info
                            )
                            
                            if success:
                                info['last_update'] = current_time
                                # Use safe status update
                                self._safe_update_monitor_status(monitor_id, "receiving")
                                processed_count += 1
                        else:
                            # Too soon, store the data for later or skip
                            info['pending_data'] = item
                            info['skipped_frames'] = info.get('skipped_frames', 0) + 1
                            skipped_count += 1
                            
                except Exception as e:
                    print(f"[MONITOR] Error processing queue item: {e}")
                    continue
            
            # Check pending data for monitors that haven't updated in a while
            with self.monitor_lock:
                monitor_ids = list(self.active_monitors.keys())
            
            for monitor_id in monitor_ids:
                with self.monitor_lock:
                    if monitor_id not in self.active_monitors:
                        continue
                    info = self.active_monitors[monitor_id]
                
                if info.get('pending_data'):
                    time_since_update = current_time - info.get('last_update', 0)
                    if time_since_update >= info.get('min_update_interval', 0.1):
                        item = info['pending_data']
                        raw_data = item.get('data', {})
                        success = self._process_and_plot_data_main_thread(
                            monitor_id, raw_data, info
                        )
                        
                        if success:
                            info['last_update'] = current_time
                            info['pending_data'] = None
                            # Use safe status update
                            self._safe_update_monitor_status(monitor_id, "receiving")
            
            # Request next frame if we processed data AND have capacity
            if processed_count > 0 and self.socketio_connected:
                # Only request next frame if queue is not too full
                if self.monitor_data_queue.qsize() < 20:
                    def delayed_request():
                        time.sleep(0.02)
                        if self.subscribed_outputs:
                            self._request_next_frame()
                    
                    threading.Thread(target=delayed_request, daemon=True).start()
            
            # Debug: Log queue status occasionally
            qsize = self.monitor_data_queue.qsize()
            if qsize > 10 and current_time - getattr(self, '_last_queue_log', 0) > 2.0:
                print(f"[QUEUE] Queue size: {qsize}, Processed: {processed_count}, Skipped: {skipped_count}")
                self._last_queue_log = current_time
                
        except Exception as e:
            print(f"[MONITOR] Critical error in update loop: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            # ALWAYS reschedule, but with adaptive timing
            try:
                # Adaptive scheduling: faster if queue has data, slower if empty
                qsize = self.monitor_data_queue.qsize()
                
                if qsize > 10:
                    next_frame = dpg.get_frame_count() + 1
                elif qsize > 0:
                    next_frame = dpg.get_frame_count() + 2
                else:
                    next_frame = dpg.get_frame_count() + 10
                
                dpg.set_frame_callback(next_frame, self._monitor_update_frame)
                
            except Exception as e:
                print(f"[MONITOR] Failed to reschedule: {e}")
                # Fallback: restart after delay
                threading.Timer(0.1, self._start_monitor_updater).start()


    def _periodic_cleanup(self):
        """Perform periodic cleanup of resources."""
        print(f"[CLEANUP] Performing periodic cleanup")
        
        # Don't clean up displays for active monitors
        monitors_to_remove = []
        for monitor_id, display in list(self.simple_displays.items()):
            if monitor_id not in self.active_monitors:
                print(f"[CLEANUP] Removing unused display for {monitor_id}")
                display.cleanup()
                monitors_to_remove.append(monitor_id)
        
        for monitor_id in monitors_to_remove:
            del self.simple_displays[monitor_id]
        
        # Check for dead monitors (windows that don't exist anymore)
        dead_monitors = []
        for monitor_id, info in list(self.active_monitors.items()):
            window_tag = info.get('window_id')
            if not window_tag or not dpg.does_item_exist(window_tag):
                print(f"[CLEANUP] Removing dead monitor: {monitor_id}")
                dead_monitors.append(monitor_id)
        
        for monitor_id in dead_monitors:
            if monitor_id in self.simple_displays:
                self.simple_displays[monitor_id].cleanup()
                del self.simple_displays[monitor_id]
            del self.active_monitors[monitor_id]
        
        # Force garbage collection
        gc.collect()
        
        print(f"[CLEANUP] Cleanup complete: {len(self.simple_displays)} displays, {len(self.active_monitors)} monitors")
        
        # Schedule next cleanup
        current_frame = dpg.get_frame_count()
        next_frame = current_frame + 100
        dpg.set_frame_callback(next_frame, self._periodic_cleanup)

                
    def _check_memory_usage(self):
        """Check and log memory usage."""
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        
        print(f"[MEMORY] RSS: {mem_info.rss / 1024 / 1024:.1f} MB, "
            f"VMS: {mem_info.vms / 1024 / 1024:.1f} MB")
        
        # Check DPG item count
        # Note: This is approximate
        print(f"[MEMORY] Active monitors: {len(self.active_monitors)}, "
            f"Simple displays: {len(self.simple_displays)}")
        
        current_frame = dpg.get_frame_count()
        next_frame = current_frame + 100 
        dpg.set_frame_callback(next_frame, self._check_memory_usage)


    def cleanup(self):
        """Clean up all resources before exit."""
        # Unsubscribe from all outputs
        for output_name in list(self.subscribed_outputs):
            if self.socketio_connected:
                try:
                    self.sio.emit('unsubscribe', {'output': output_name})
                except:
                    pass
        
        # Stop monitor updater
        self.monitor_running = False
        
        # Disconnect Socket.IO
        if self.socketio_connected:
            self.sio.disconnect()
        
        # Close all monitors
        for monitor_id in list(self.active_monitors.keys()):
            self._close_monitor(monitor_id)
        
        self._log("Cleanup complete")


    
    def is_data_class_type(self, type_name):
        if not type_name or type_name == "Any":
            return False
            
        # 1. Check if it's in the data object templates
        if hasattr(self, 'data_obj_templates') and type_name in self.data_obj_templates:
            return True
            
        # 2. Check if the type name suggests it's a Data Object (Specula naming convention)
        data_keywords = ["Matrix", "Vector", "Atmosphere", "Telescope", "Detector", "Field"]
        if any(k in type_name for k in data_keywords):
            return True

        return False


    def init_themes(self):
        self.data_theme = create_data_node_theme()
        self.proc_theme = create_proc_node_theme()

    def update_node_value(self, sender, app_data, user_data):
        node_uuid, param_name = user_data
        # Store exactly what the user typed/selected
        self.graph.nodes[node_uuid]["values"][param_name] = app_data

    def get_selected_nodes(self):
        """Returns a list of UUIDs for currently selected nodes in the editor."""
        # 'specula_editor' is assumed to be the tag of your dpg.node_editor
        selected_dpg_ids = dpg.get_selected_nodes("specula_editor")
        return [self.dpg_to_uuid[d_id] for d_id in selected_dpg_ids if d_id in self.dpg_to_uuid]

    def delete_selection(self):
        """Deletes all selected nodes and their associated links from graph and UI."""
        selected_uuids = self.get_selected_nodes()
        for node_uuid in selected_uuids:
            self.delete_node(node_uuid)

    def delete_node(self, node_uuid):
        """Fully removes a node, its links, and registry entries."""
        if node_uuid not in self.uuid_to_dpg:
            return

        dpg_id = self.uuid_to_dpg[node_uuid]
        
        # 1. Clean up Link Registry
        # We must find all links connected to this node
        links_to_remove = []
        for link_id, conn_data in self.link_registry.items():
            src_uuid, _, dst_uuid, _ = conn_data
            if src_uuid == node_uuid or dst_uuid == node_uuid:
                links_to_remove.append(link_id)
        
        for link_id in links_to_remove:
            # Remove from DPG
            if dpg.does_item_exist(link_id):
                dpg.delete_item(link_id)
            # Remove from Registry & Graph
            conn_data = self.link_registry.pop(link_id)
            self.graph.remove_connection(*conn_data)

        # 2. Clean up Attribute Registries
        # Find all attributes belonging to this node
        input_attrs = [k for k, v in self.input_attr_registry.items() if v[0] == node_uuid]
        output_attrs = [k for k, v in self.output_attr_registry.items() if v[0] == node_uuid]

        for attr in input_attrs:
            del self.input_attr_registry[attr]
        for attr in output_attrs:
            del self.output_attr_registry[attr]

        # 3. Remove Node from DPG
        if dpg.does_item_exist(dpg_id):
            dpg.delete_item(dpg_id)

        # 4. Remove from Node Registries
        del self.dpg_to_uuid[dpg_id]
        del self.uuid_to_dpg[node_uuid]
        
        # 5. Remove from Graph Model
        if node_uuid in self.graph.nodes:
            self.graph.remove_node(node_uuid)
            
        print(f"Deleted node: {node_uuid}")


    def create_node(self, node_type, pos=None, existing_uuid=None, name_override=None):
        node_uuid = existing_uuid if existing_uuid else str(uuid.uuid4())[:8]
        
        if node_uuid not in self.graph.nodes:
            self.graph.add_node(node_uuid, node_type)
        
        node_data = self.graph.nodes[node_uuid]
        template = self.all_templates.get(node_type, {})

        if name_override:
            node_name = name_override
        else:
            node_name = self._generate_unique_name(node_type)

        node_data['name'] = node_name
        node_label = node_name

        final_pos = pos if pos else [100, 100]

        with dpg.node(label=node_label, parent="specula_editor") as dpg_id:

            self.node_item_registry[node_uuid] = dpg_id

            dpg.set_item_pos(dpg_id, final_pos)
            self.dpg_to_uuid[dpg_id] = node_uuid
            self.uuid_to_dpg[node_uuid] = dpg_id
            
            # --- STATIC HEADER ---
            with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                dpg.add_text(f"Class: {node_type}", color=[130, 130, 130])
                dpg.add_spacer(width=200)

            # --- REFERENCE PARAMETER INPUTS (with _ref suffix) ---
            # Check template parameters for reference kind
            template_params = template.get("parameters", {})
            for param_name, param_meta in template_params.items():
                if isinstance(param_meta, dict) and param_meta.get("kind") == "reference":
                    # Create a square input pin for reference parameters with _ref suffix
                    display_name = f"{param_name}_ref"
                    with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Input, shape=REF_SHAPE) as attr_id:
                        dpg.add_text(display_name, color=[150, 255, 150])  # Green color for ref inputs
                        self.input_attr_registry[attr_id] = (node_uuid, display_name)

            # --- STANDARD INPUTS (non-reference) ---
            for in_attr, meta in node_data.get("inputs", {}).items():
                kind = meta.get("kind", "single")

                # Skip if this is already handled as a reference parameter above
                # Check if it's a reference (ends with _ref) or layer_list
                is_ref = in_attr.endswith("_ref") or in_attr == "layer_list"
                
                # Skip if it's a reference that was already created above
                if is_ref:
                    continue
                    
                pin_shape = DATA_SHAPE
                text_color = [255, 255, 255]

                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Input, shape=pin_shape) as attr_id:
                    label = f"{in_attr} [*]" if kind == "variadic" else in_attr
                    dpg.add_text(label, color=text_color)
                    self.input_attr_registry[attr_id] = (node_uuid, in_attr)
                        
            # --- OUTPUTS ---
            # SPECIAL HANDLING FOR AtmoPropagation
            if node_type == "AtmoPropagation":
                # Also add standard outputs from template
                all_outputs = list(node_data.get("outputs", []))
                if 'outputs_extra' in node_data:
                    all_outputs.extend(node_data['outputs_extra'])
                
                for out in all_outputs:
                    # Skip generic placeholder outputs
                    if out.startswith("out_' + ") and out.endswith(" + '_ef'"):
                        continue
                    if "name" in out and "+" in out and "'" in out:
                        continue
                        
                    display_label = out.replace(":", " [") + "]" if ":" in out else out
                    
                    with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Output, shape=DATA_SHAPE) as attr_id:
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(width=100) 
                            dpg.add_text(display_label)
                        self.output_attr_registry[attr_id] = (node_uuid, out)
            
            elif node_type == 'SimulParams':
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Output, shape=REF_SHAPE) as attr_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=100)
                        dpg.add_text("ref", color=[150, 150, 150])
                    self.output_attr_registry[attr_id] = (node_uuid, "ref")
            
            # Handle standard outputs for other nodes
            else:
                all_outputs = list(node_data.get("outputs", []))
                if 'outputs_extra' in node_data:
                    all_outputs.extend(node_data['outputs_extra'])

                for out in all_outputs:
                    # Skip placeholder outputs
                    if "name" in out and "+" in out and "'" in out:
                        continue
                        
                    display_label = out.replace(":", " [") + "]" if ":" in out else out
                    
                    with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Output, shape=DATA_SHAPE) as attr_id:
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(width=100) 
                            dpg.add_text(display_label)
                        self.output_attr_registry[attr_id] = (node_uuid, out)

            # --- SPECIFIC OBJECT OVERRIDES ---            
            if node_type in ["Source", "Pupilstop"]:
                # Ref shape (Square)
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Output, shape=REF_SHAPE) as attr_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=100)
                        dpg.add_text("ref", color=[100, 200, 255])
                    self.output_attr_registry[attr_id] = (node_uuid, "ref")

            # Apply themes based on template category
            category = template.get("bases", "")
            if "BaseDataObj" in category:
                dpg.bind_item_theme(dpg_id, self.data_theme)
            else:
                dpg.bind_item_theme(dpg_id, self.proc_theme)

        return node_uuid


    def _add_dynamic_atmo_output(self, in_node_uuid, source_name):
        """Helper method to add dynamic output to AtmoPropagation node."""
        print(f"[DYNAMIC DEBUG] Adding dynamic output for node {in_node_uuid}, source: {source_name}")
        print(f"[DYNAMIC DEBUG] node_item_registry keys: {list(self.node_item_registry.keys())}")
        print(f"[DYNAMIC DEBUG] uuid_to_dpg keys: {list(self.uuid_to_dpg.keys())}")
        
        # Try both registries
        dpg_id = self.uuid_to_dpg.get(in_node_uuid)
        if not dpg_id:
            dpg_id = self.node_item_registry.get(in_node_uuid)
            print(f"[DYNAMIC DEBUG] Got DPG ID from node_item_registry: {dpg_id}")
        
        if not dpg_id:
            print(f"[DYNAMIC DEBUG] ERROR: No DPG ID found for node {in_node_uuid}")
            print(f"[DYNAMIC DEBUG] Node exists in graph: {in_node_uuid in self.graph.nodes}")
            if in_node_uuid in self.graph.nodes:
                node_data = self.graph.nodes[in_node_uuid]
                print(f"[DYNAMIC DEBUG] Node type: {node_data.get('type')}")
                print(f"[DYNAMIC DEBUG] Node name: {node_data.get('name')}")
            return
        
        print(f"[DYNAMIC DEBUG] DPG ID found: {dpg_id}, exists: {dpg.does_item_exist(dpg_id)}")
        
        in_node_data = self.graph.nodes.get(in_node_uuid, {})
        
        if not in_node_data:
            print(f"[DYNAMIC] ERROR: Node {in_node_uuid} not found in graph")
            return
        
        # Create dynamic output name
        new_output = f"out_{source_name}_ef"
        
        print(f"[DYNAMIC] Attempting to add output '{new_output}' to node {in_node_uuid}")
        
        # Check if this output already exists
        if 'outputs_extra' not in in_node_data:
            in_node_data['outputs_extra'] = []
            print(f"[DYNAMIC] Created outputs_extra list")
        
        if new_output in in_node_data['outputs_extra']:
            print(f"[DYNAMIC] Output '{new_output}' already exists")
            return
        
        # Add to outputs_extra
        in_node_data['outputs_extra'].append(new_output)
        print(f"[DYNAMIC] Added '{new_output}' to outputs_extra")
        
        if not dpg.does_item_exist(dpg_id):
            print(f"[DYNAMIC] ERROR: DPG node {dpg_id} doesn't exist")
            return
        
        print(f"[DYNAMIC] Creating DPG output pin on node {dpg_id}")
        
        # Create the new output pin      
        with dpg.node_attribute(
            attribute_type=dpg.mvNode_Attr_Output, 
            shape=DATA_SHAPE,
            parent=dpg_id
        ) as attr_id:
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=100)
                dpg.add_text(new_output, color=[100, 255, 255])  # Cyan for dynamic outputs
            
            # Register the new output
            self.output_attr_registry[attr_id] = (in_node_uuid, new_output)
            print(f"[DYNAMIC] Successfully created output '{new_output}' with attr_id {attr_id}")
            print(f"[DYNAMIC] New output_attr_registry entry: {attr_id} -> ({in_node_uuid}, {new_output})")

    def link_callback(self, sender, app_data):
        out_attr_id, in_attr_id = app_data

        out_node_uuid, out_name = self.output_attr_registry.get(out_attr_id, (None, None))
        in_node_uuid, in_name = self.input_attr_registry.get(in_attr_id, (None, None))

        if not out_node_uuid or not in_node_uuid:
            return

        # Create visual link
        link_id = dpg.add_node_link(out_attr_id, in_attr_id, parent=sender)
        self.link_registry[link_id] = (out_node_uuid, out_name, in_node_uuid, in_name)

        # Update graph topology
        self.graph.add_connection(out_node_uuid, out_name, in_node_uuid, in_name)

        dst_node = self.graph.nodes.get(in_node_uuid, {})
        src_node = self.graph.nodes.get(out_node_uuid, {})

        if not dst_node or not src_node:
            return

        dst_node.setdefault("values", {})
        src_name = src_node.get("name", out_node_uuid)
        
        # Get template for checking parameter types
        dst_type = dst_node.get("type", "")
        template = self.all_templates.get(dst_type, {})
        template_params = template.get("parameters", {})

        # --- REF SEMANTICS ---
        # Check if this is a reference connection (ends with _ref or layer_list)
        is_ref_connection = in_name.endswith("_ref") or in_name == "layer_list"
        
        if is_ref_connection:
            # source_dict_ref → ALWAYS a list
            if in_name == "source_dict_ref":
                # Initialize as list if not exists
                if in_name not in dst_node["values"]:
                    dst_node["values"][in_name] = []
                
                # Add source name to list if not already present
                if src_name not in dst_node["values"][in_name]:
                    dst_node["values"][in_name].append(src_name)

                if dst_node.get("type") == "AtmoPropagation":
                    print(f"[LINK] Triggering dynamic output for {src_name} on {dst_node.get('name')}")
                    self._add_dynamic_atmo_output(in_node_uuid, src_name)
            
            # layer_list → always a list
            elif in_name == "layer_list":
                # Initialize as list if not exists
                if in_name not in dst_node["values"]:
                    dst_node["values"][in_name] = []
                
                # Add to list if not already present
                if src_name not in dst_node["values"][in_name]:
                    dst_node["values"][in_name].append(src_name)
            
            # Other reference parameters (single references)
            else:
                # Get base parameter name (without _ref) for template lookup
                base_param_name = in_name[:-4]  # Remove '_ref'
                
                # Store the connected node name
                dst_node["values"][in_name] = src_name
                print(f"[LINK] Set reference parameter {in_name} = {src_name}")

        # refresh UI if needed
        if self._last_selected_uuid == in_node_uuid:
            self.update_property_panel(in_node_uuid, "property_panel")

    def delink_callback(self, sender, app_data):
        # app_data is the link_id
        link_id = app_data

        if link_id not in self.link_registry:
            return

        src_uuid, src_attr, dst_uuid, dst_attr = self.link_registry.pop(link_id)

        # 1️⃣ Remove from graph topology
        self.graph.remove_connection(src_uuid, src_attr, dst_uuid, dst_attr)

        dst_node = self.graph.nodes.get(dst_uuid, {})
        src_node = self.graph.nodes.get(src_uuid, {})

        if not dst_node or not src_node:
            if dpg.does_item_exist(link_id):
                dpg.delete_item(link_id)
            return

        src_name = src_node.get("name", src_uuid)

        # 2️⃣ UPDATE REFERENCE VALUES
        values = dst_node.get("values", {})

        # --- SPECIAL CASE: source_dict_ref (always a list) ---
        if dst_attr == "source_dict_ref":
            lst = values.get("source_dict_ref", [])

            if src_name in lst:
                lst.remove(src_name)
                print(f"[REF] Removed {src_name} from source_dict_ref")

            if not lst:
                values.pop("source_dict_ref", None)

            # 3️⃣ REMOVE DYNAMIC OUTPUT IF NEEDED
            if dst_node.get("type") == "AtmoPropagation":
                dynamic_output = f"out_{src_name}_ef"

                # Only remove if source is no longer referenced
                if src_name not in values.get("source_dict_ref", []):
                    if dynamic_output in dst_node.get("outputs_extra", []):
                        dst_node["outputs_extra"].remove(dynamic_output)

                        # Remove DPG pin
                        attr_to_remove = None
                        for attr_id, (uuid, name) in self.output_attr_registry.items():
                            if uuid == dst_uuid and name == dynamic_output:
                                attr_to_remove = attr_id
                                break

                        if attr_to_remove:
                            del self.output_attr_registry[attr_to_remove]
                            if dpg.does_item_exist(attr_to_remove):
                                dpg.delete_item(attr_to_remove)

                        print(f"[DYNAMIC] Removed output '{dynamic_output}'")

        # --- layer_list (always a list) ---
        elif dst_attr == "layer_list":
            lst = values.get("layer_list", [])
            if src_name in lst:
                lst.remove(src_name)
                if not lst:
                    values.pop("layer_list", None)
        
        # --- Other reference parameters (single references) ---
        elif dst_attr.endswith("_ref"):
            if values.get(dst_attr) == src_name:
                values.pop(dst_attr, None)
                print(f"[REF] Cleared {dst_attr}")

        # 4️⃣ Refresh property panel if needed
        if self._last_selected_uuid == dst_uuid:
            self.update_property_panel(dst_uuid, "property_panel")

        # 5️⃣ Remove visual link
        if dpg.does_item_exist(link_id):
            dpg.delete_item(link_id)

            
    def add_dynamic_io(self, node_uuid):
        parent = self.uuid_to_dpg[node_uuid]
        # Logic for source_dict_ref and output pins
        with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Input, parent=parent, shape=REF_SHAPE) as attr_id:
            dpg.add_text("Sources (Ref)", color=[150, 255, 150])
            self.input_attr_registry[attr_id] = (node_uuid, "source_dict_ref")
        
        with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Output, parent=parent) as attr_id:
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=100)
                dpg.add_text("output", color=[255, 200, 100])
            self.output_attr_registry[attr_id] = (node_uuid, "output")


    def add_data_output(self, node_uuid):
        parent = self.uuid_to_dpg[node_uuid]
        with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Output, parent=parent) as attr_id:
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=100)
                dpg.add_text("Output: ref")
            self.output_attr_registry[attr_id] = (node_uuid, "ref")


    def clear_all(self):
        self.dpg_to_uuid.clear()
        self.uuid_to_dpg.clear()
        self.input_attr_registry.clear()
        self.output_attr_registry.clear()
        self.link_registry.clear()
        dpg.delete_item("specula_editor", children_only=True)


    def manual_link(self, src_uuid, src_attr, dst_uuid, dst_attr):
        """Robustly links nodes, creating pins for indices like 'out_layer:-1'."""
        src_id = None
        dst_id = None

        # --- 1. FIND/CREATE SOURCE PIN ---
        # Look for exact match first
        for d_id, (u_id, name) in self.output_attr_registry.items():
            if u_id == src_uuid and name == src_attr:
                src_id = d_id
                break
        
        # If not found, create it (handles dm.out_layer:-1)
        if src_id is None:
            parent = self.uuid_to_dpg.get(src_uuid)
            if parent:
                # Determine if this link is a reference link
                is_ref_link = dst_attr.endswith("_ref") or "params" in dst_attr.lower()
                shape = REF_SHAPE if is_ref_link else DATA_SHAPE
                color = [150, 150, 150] if is_ref_link else [255, 255, 255]

                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Output, parent=parent, shape=shape) as new_id:
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=100)
                        dpg.add_text(f"{src_attr}", color=color)
                    self.output_attr_registry[new_id] = (src_uuid, src_attr)
                    src_id = new_id


        # --- 2. FIND/CREATE DESTINATION PIN ---
        for d_id, (u_id, name) in self.input_attr_registry.items():
            if u_id == dst_uuid and name == dst_attr:
                dst_id = d_id
                break
        
        if dst_id is None:
            parent = self.uuid_to_dpg.get(dst_uuid)
            if parent:
                is_ref = dst_attr.endswith("_ref") or dst_attr == "layer_list"
                pin_shape = REF_SHAPE if is_ref else DATA_SHAPE
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Input, parent=parent, shape=pin_shape) as new_id:
                    dpg.add_text(dst_attr, color=[150, 255, 150])
                    self.input_attr_registry[new_id] = (dst_uuid, dst_attr)
                    dst_id = new_id


        # --- 3. LINK ---
        if src_id and dst_id:
            link_id = dpg.add_node_link(src_id, dst_id, parent="specula_editor")
            
            # STYLING:
            # Check for feedback link (:-1)
            if ":-" in str(src_attr):                
                apply_feedback_link_style(link_id)
            # Check for reference link
            elif dst_attr.endswith("_ref") or "params" in dst_attr.lower():
                apply_ref_link_style(link_id)

            self.link_registry[link_id] = (src_uuid, src_attr, dst_uuid, dst_attr)
            self.graph.add_connection(src_uuid, src_attr, dst_uuid, dst_attr)            

    def update_property_panel(self, node_uuid, panel_tag):
        """Updates property panel with an editable Name field at the top."""
        dpg.delete_item(panel_tag, children_only=True)
        
        if node_uuid not in self.graph.nodes:
            return

        node_data = self.graph.nodes[node_uuid]
        node_type = node_data["type"]
        node_name = node_data.get("name", node_type)

        template = self.all_templates.get(node_type, {})
        template_params = template.get('parameters', {})
        current_values = node_data.get('values', {})
        suffixes = node_data.get('suffixes', set())

        # --- 1. EDITABLE NAME FIELD ---
        dpg.add_text("Node Configuration", color=[100, 200, 255], parent=panel_tag)
        with dpg.group(horizontal=True, parent=panel_tag):
            dpg.add_text("Instance Name:", color=[255, 255, 255])
            dpg.add_input_text(
                default_value=node_name,
                width=150,
                callback=self._update_node_name,
                user_data=node_uuid
            )
        dpg.add_text(f"Class: {node_type}", color=[150, 150, 150], parent=panel_tag)
        dpg.add_separator(parent=panel_tag)
        
        rendered_params = set()

        # --- 2. PARAMETERS SECTION ---
        if "parameters" in template and isinstance(template["parameters"], dict):
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Parameters", color=[100, 255, 100], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)

            params_dict = template["parameters"]
            for param_name, meta in params_dict.items():
                # Check if this is a reference parameter
                is_ref_param = isinstance(meta, dict) and meta.get("kind") == "reference"
                
                if is_ref_param:
                    # For reference parameters, always use _ref suffix
                    display_name = f"{param_name}_ref"
                    
                    # Get the connected value (stored with _ref suffix)
                    connected_value = current_values.get(display_name)
                    
                    if connected_value:
                        # Show connected reference with _ref suffix
                        with dpg.group(horizontal=True, parent=panel_tag):
                            dpg.add_text(f"{display_name}:", color=[150, 255, 150])
                            dpg.add_text(f" → {connected_value}", color=[100, 255, 100])
                            
                            # Add disconnect button
                            dpg.add_button(
                                label="X",
                                callback=self._disconnect_reference,
                                user_data=(node_uuid, display_name, connected_value),
                                width=20,
                                height=20
                            )
                    else:
                        # Not connected - show REQUIRED with _ref suffix
                        with dpg.group(horizontal=True, parent=panel_tag):
                            dpg.add_text(f"{display_name}:", color=[255, 200, 150])
                            dpg.add_text("REQUIRED (connect via link)", color=[255, 100, 100])
                    rendered_params.add(param_name)
                    continue  # Skip widget rendering for reference params
                
                # Get the value (try both names)
                val = current_values.get(param_name)
                if val is None and param_name in suffixes:
                    # Check if stored with object suffix
                    val = current_values.get(f"{param_name}_object")
                        
                if val is None and isinstance(meta, dict):
                    val = meta.get("default")
                    if val is not None:
                        current_values[param_name] = val
                
                # Get type hint with proper fallback
                type_hint = meta.get("type", "str") if isinstance(meta, dict) else "str"
                if type_hint is None:
                    type_hint = "str"
                    
                default_val = meta.get("default") if isinstance(meta, dict) else None

                self._render_single_widget(
                    panel_tag,
                    node_uuid,
                    param_name,
                    val,
                    type_hint,
                    default_val
                )                
                
                # Mark as rendered
                rendered_params.add(param_name)

        # --- 3. CONNECTIONS SECTION ---
        # Get connections for this node
        incoming, outgoing = self.get_connections_for_node(node_uuid)
        
        # Separate reference connections from regular input connections
        regular_inputs = []
        reference_inputs = []
        
        for conn in incoming:
            dst_attr = conn['dst_attr']
            # Check if this is a reference connection
            is_ref = dst_attr.endswith("_ref") or dst_attr == "layer_list"
                
            if is_ref:
                reference_inputs.append(conn)
            else:
                regular_inputs.append(conn)

        # Show regular inputs
        if regular_inputs:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Input Connections", color=[200, 150, 255], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)
            
            dpg.add_text("Data Inputs:", color=[255, 200, 100], parent=panel_tag)
            for conn in regular_inputs:
                src_name = conn['src_name']
                src_attr = conn['src_attr']
                dst_attr = conn['dst_attr']
                
                # Special handling for DataStore input_list
                if dst_attr == "input_list":
                    # Get filename for this connection
                    filename = self.get_connection_filename(node_uuid, conn['src_node'], src_attr)
                    
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(f"  • {dst_attr}: ", color=[200, 200, 200])
                        dpg.add_text(f"{filename}-{src_name}.{src_attr}", color=[150, 255, 150])
                    
                    # Add editable filename field for DataStore
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text("    Filename: ", color=[200, 200, 200])
                        dpg.add_input_text(
                            default_value=filename,
                            width=100,
                            callback=self._update_connection_filename,
                            user_data=(node_uuid, conn['src_node'], src_attr)
                        )
                else:
                    # Regular connection - display only
                    with dpg.group(horizontal=True, parent=panel_tag):
                        dpg.add_text(f"  • {dst_attr}: ", color=[200, 200, 200])
                        dpg.add_text(f"{src_name}.{src_attr}", color=[150, 255, 150])

        # Show reference connections separately
        if reference_inputs:
            if not regular_inputs:  # Add spacing only if there were no regular inputs
                dpg.add_spacer(height=10, parent=panel_tag)
                dpg.add_text("Connections", color=[200, 150, 255], parent=panel_tag)
                dpg.add_separator(parent=panel_tag)
            
            dpg.add_text("Reference Connections:", color=[255, 200, 100], parent=panel_tag)
            for conn in reference_inputs:
                src_name = conn['src_name']
                src_attr = conn['src_attr']
                dst_attr = conn['dst_attr']
                
                # Display reference connection (usually src_attr is "ref")
                with dpg.group(horizontal=True, parent=panel_tag):
                    dpg.add_text(f"  • {dst_attr}: ", color=[200, 200, 200])
                    if src_attr == "ref":
                        dpg.add_text(f"{src_name}", color=[100, 255, 100])
                    else:
                        dpg.add_text(f"{src_name}.{src_attr}", color=[100, 255, 100])
        
        # Show outgoing connections
        if outgoing:
            if not regular_inputs and not reference_inputs:
                dpg.add_spacer(height=10, parent=panel_tag)
                dpg.add_text("Connections", color=[200, 150, 255], parent=panel_tag)
                dpg.add_separator(parent=panel_tag)
            
            dpg.add_text("Outputs:", color=[255, 200, 100], parent=panel_tag)
            for conn in outgoing:
                dst_name = conn['dst_name']
                src_attr = conn['src_attr']
                dst_attr = conn['dst_attr']
                
                with dpg.group(horizontal=True, parent=panel_tag):
                    dpg.add_text(f"  • {src_attr} → ", color=[200, 200, 200])
                    dpg.add_text(f"{dst_name}.{dst_attr}", color=[150, 255, 150])
        
        if not incoming and not outgoing:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Connections", color=[200, 150, 255], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)
            dpg.add_text("No connections", color=[150, 150, 150], parent=panel_tag)
        
        dpg.add_spacer(height=10, parent=panel_tag)
        
        # --- 4. OUTPUT MONITORS SECTION ---
        # Get all outputs for this node
        all_outputs = []
        
        # Get outputs from template
        template_outputs = template.get('outputs', [])
        for out in template_outputs:
            if isinstance(out, str) and out not in all_outputs:
                all_outputs.append(out)
        
        # Get extra outputs
        extra_outputs = node_data.get('outputs_extra', [])
        for out in extra_outputs:
            if isinstance(out, str) and out not in all_outputs:
                all_outputs.append(out)
        
        # Also check output_attr_registry for this node
        for attr_id, (uuid, name) in self.output_attr_registry.items():
            if uuid == node_uuid and name not in all_outputs:
                all_outputs.append(name)
        
        if all_outputs:
            dpg.add_spacer(height=10, parent=panel_tag)
            dpg.add_text("Output Monitors", color=[255, 150, 100], parent=panel_tag)
            dpg.add_separator(parent=panel_tag)
            
            for output_name in sorted(all_outputs):
                # Check if this monitor is open
                is_open = False
                for monitor_id, info in self.active_monitors.items():
                    if info.get('node_uuid') == node_uuid and info.get('output_name') == output_name:
                        is_open = True
                        break
                
                with dpg.group(horizontal=True, parent=panel_tag):
                    dpg.add_text(f"  • {output_name}: ", color=[200, 200, 200])
                    
                    if not is_open:
                        dpg.add_button(
                            label="Open Monitor",
                            callback=self._open_output_monitor,
                            user_data=(node_uuid, output_name),
                            width=120
                        )
                        dpg.add_text("○ Inactive", color=[150, 150, 150])
                    else:
                        # Find the monitor ID
                        monitor_id = None
                        for mid, info in self.active_monitors.items():
                            if info.get('node_uuid') == node_uuid and info.get('output_name') == output_name:
                                monitor_id = mid
                                break
                        
                        if monitor_id:
                            def close_callback_wrapper(sender, app_data, user_data):
                                self._close_monitor(user_data, from_window_close=False)
                            
                            dpg.add_button(
                                label="Close Monitor",
                                callback=close_callback_wrapper,
                                user_data=monitor_id,
                                width=120
                            )
                            dpg.add_text("● Active", color=[0, 255, 0])

            dpg.add_spacer(height=5, parent=panel_tag)
        
        dpg.add_spacer(height=10, parent=panel_tag)


    def _disconnect_reference(self, sender, app_data, user_data):
        """Disconnect a reference connection."""
        node_uuid, param_name, connected_node_name = user_data
        
        # Find the link to disconnect
        link_to_remove = None
        
        for link_id, (src_uuid, src_attr, dst_uuid, dst_attr) in self.link_registry.items():
            if dst_uuid == node_uuid and dst_attr == param_name:
                # Get the source node name
                src_node = self.graph.nodes.get(src_uuid, {})
                src_node_name = src_node.get("name", "")
                if src_node_name == connected_node_name:
                    link_to_remove = link_id
                    break
        
        if link_to_remove:
            # Trigger the delink callback
            self.delink_callback(None, link_to_remove)

    def _render_single_widget(self, parent, node_uuid, param_name, val, type_hint, default_val=None):
        """Helper to render one row in the property panel."""
        def _values_equal(a, b):
            try:
                return a == b
            except Exception:
                return False

        # Get the parameter metadata to check kind
        node_data = self.graph.nodes.get(node_uuid, {})
        template = self.all_templates.get(node_data.get("type", ""), {})
        template_params = template.get("parameters", {})
        param_meta = template_params.get(param_name, {})
        param_kind = param_meta.get("kind", "value")
        
        # Check if this is a connected reference parameter
        is_connected_ref = param_kind == "reference" and val is not None
        
        if is_connected_ref:
            # Show connected reference as read-only
            with dpg.group(horizontal=True, parent=parent):
                dpg.add_text(f"{param_name}:", color=[150, 255, 150])
                dpg.add_text(f": {val}", color=[100, 255, 100])              
            return
        
        # Determine if this is a data object parameter
        is_data_object = (
            param_kind == "object" or 
            param_name in node_data.get('suffixes', set()) or 
            self.is_data_class_type(type_hint)
        )
        
        is_default = default_val is not None and _values_equal(val, default_val)
        
        # Color coding
        if is_data_object:
            label_color = [150, 200, 255]  # Blue-ish for data objects
        elif param_kind == "reference":
            label_color = [255, 200, 150]  # Orange-ish for references
        elif is_default:
            label_color = DEFAULT_PARAM_COLOR
        else:
            label_color = MODIFIED_PARAM_COLOR

        # Update user_data to include the target type: (uuid, name, type)
        user_data = (node_uuid, param_name, type_hint)

        with dpg.group(horizontal=True, parent=parent):        
            dpg.add_text(f"{param_name}:", color=label_color)

            # Widget Logic
            if type_hint in ['bool', 'boolean']:
                if val is None: 
                    val = False
                dpg.add_checkbox(default_value=bool(val), 
                                callback=self._update_param, 
                                user_data=user_data)
            
            elif type_hint in ['int', 'integer']:
                if val is None: 
                    val = 0
                dpg.add_input_int(default_value=int(val), 
                                width=150,
                                step=1,
                                callback=self._update_param, 
                                user_data=user_data)

            elif type_hint in ['float', 'double', 'number']:
                if val is None or val == 'inf': 
                    val = 0.0
                dpg.add_input_float(default_value=float(val), 
                                    width=150,
                                    step=0.1,
                                    callback=self._update_param, 
                                    user_data=user_data)
            
            elif isinstance(val, list) or type_hint == 'list':
                if val is None: 
                    val = []
                dpg.add_input_text(default_value=str(val), 
                                width=150,
                                callback=self._update_param, 
                                user_data=user_data)
            else:
                # String / Fallback
                if val is None: 
                    val = ""
                dpg.add_input_text(default_value=str(val), 
                                width=150,
                                callback=self._update_param, 
                                user_data=user_data)
            

    def _update_node_name(self, sender, app_data, user_data):
        """Updates the instance name in the graph and refreshes the node UI label."""
        node_uuid = user_data
        new_name = app_data
        
        if node_uuid in self.graph.nodes:
            # 1. Update the internal model
            self.graph.nodes[node_uuid]["name"] = new_name
            
            # 2. Update the visual node label in the editor
            dpg_id = self.uuid_to_dpg.get(node_uuid)
            if dpg_id:
                # Update the label shown on top of the node
                dpg.set_item_label(dpg_id, f"{new_name} ({self.graph.nodes[node_uuid]['type']})")
            
            print(f"Renamed node {node_uuid} to '{new_name}'")

    def _update_param(self, sender, app_data, user_data):
        """Callback to save UI changes to the Graph, with STRICT type enforcement."""
        # Unpack the new 3-element tuple
        node_uuid, param_name, target_type = user_data
        
        if node_uuid not in self.graph.nodes:
            return
            
        values_dict = self.graph.nodes[node_uuid]["values"]
        final_val = app_data

        try:
            # 1. Handle List / Complex parsing from Text Inputs
            if target_type == 'list' or (isinstance(app_data, str) and app_data.startswith("[")):
                import ast
                try:
                    # Safely convert "[1, 2]" -> [1, 2]
                    final_val = ast.literal_eval(app_data)
                except (ValueError, SyntaxError):
                    # If invalid syntax (e.g. user is still typing), don't save yet or save as string
                    print(f"Warning: Invalid list syntax for {param_name}")
                    return

            # 2. Handle Numeric Casting (Redundancy check)
            elif target_type in ['int', 'integer']:
                final_val = int(app_data)
            
            elif target_type in ['float', 'double', 'number']:
                final_val = float(app_data)

            # 3. Handle Booleans
            elif target_type in ['bool', 'boolean']:
                final_val = bool(app_data)

            # 4. Save to Graph
            values_dict[param_name] = final_val
            
            # Debug log
            print(f"Updated {node_uuid} [{param_name}] -> {final_val} ({type(final_val).__name__})")

        except Exception as e:
            print(f"Error updating parameter {param_name}: {e}")

    def manual_link_with_filename(self, src_uuid, src_attr, dst_uuid, dst_attr, filename):
        """Link nodes with filename for DataStore connections."""
        # First create the regular link
        self.manual_link(src_uuid, src_attr, dst_uuid, dst_attr)
        
        # Store filename info in the graph
        if 'filename_map' not in self.graph.nodes[dst_uuid]:
            self.graph.nodes[dst_uuid]['filename_map'] = {}
        
        # Create a key for this connection
        conn_key = f"{src_uuid}.{src_attr}"
        self.graph.nodes[dst_uuid]['filename_map'][conn_key] = filename


    def get_connections_for_node(self, node_uuid):
        """Get all incoming and outgoing connections for a node."""
        incoming = []
        outgoing = []
        
        for (src_u, src_at, dst_u, dst_at) in self.graph.connections:
            if dst_u == node_uuid:
                incoming.append({
                    'src_node': src_u,
                    'src_attr': src_at,
                    'dst_attr': dst_at,
                    'src_name': self.graph.nodes[src_u].get('name', 'unknown'),
                    'dst_name': self.graph.nodes[node_uuid].get('name', 'unknown'),
                    'type': 'input'
                })
            
            if src_u == node_uuid:
                outgoing.append({
                    'dst_node': dst_u,
                    'src_attr': src_at,
                    'dst_attr': dst_at,
                    'src_name': self.graph.nodes[node_uuid].get('name', 'unknown'),
                    'dst_name': self.graph.nodes[dst_u].get('name', 'unknown'),
                    'type': 'output'
                })
        
        return incoming, outgoing

    def update_connection_filename(self, node_uuid, src_uuid, src_attr, new_filename):
        """Update filename for a DataStore connection."""
        if 'filename_map' not in self.graph.nodes[node_uuid]:
            self.graph.nodes[node_uuid]['filename_map'] = {}
        
        conn_key = f"{src_uuid}.{src_attr}"
        self.graph.nodes[node_uuid]['filename_map'][conn_key] = new_filename

    def get_connection_filename(self, node_uuid, src_uuid, src_attr):
        """Get filename for a DataStore connection."""
        if 'filename_map' not in self.graph.nodes[node_uuid]:
            return "data"  # Default
        
        conn_key = f"{src_uuid}.{src_attr}"
        return self.graph.nodes[node_uuid]['filename_map'].get(conn_key, "data")
    

    def _update_connection_filename(self, sender, app_data, user_data):
        """Callback to update filename for a DataStore connection."""
        node_uuid, src_uuid, src_attr = user_data
        new_filename = app_data
        
        # Update the filename in the graph
        self.update_connection_filename(node_uuid, src_uuid, src_attr, new_filename)
        
        print(f"Updated filename for connection {src_uuid}.{src_attr} -> {node_uuid}: {new_filename}")