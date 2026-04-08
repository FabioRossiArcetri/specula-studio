"""
monitor_manager.py
==================
Manages the full lifecycle of live-data monitor windows.

Responsibilities
----------------
- Open / close monitor windows (DearPyGui).
- Maintain the ``active_monitors`` dict and its thread-safety lock.
- Receive raw data updates (from SocketIOClient callbacks) via
  ``on_data_update``, queue them, and plot them on the DPG main thread.
- Manage subscribe / unsubscribe calls to SocketIOClient.
- Run periodic maintenance (queue health, cleanup, memory check).

Dependencies
------------
- ``SocketIOClient``   – for subscribe / unsubscribe / reconnect / server_url.
- ``GraphManager``     – read-only access to node names and types.
- ``DPGPlotter``       – renders plots inside the monitor windows.
"""

import gc
import os
import time
import threading
import traceback
from queue import Queue

import numpy as np
import psutil
import dearpygui.dearpygui as dpg

from dpg_plotting import DPGPlotter
from constants import (
    STATUS_QUEUE_SIZE,
    MONITOR_QUEUE_SIZE,
    MAX_QUEUE_ITEMS_PER_FRAME,
)


class MonitorManager:
    """Manages live-data monitor windows for Specula node outputs."""

    def __init__(self, sio_client, graph, debug: bool = True):
        """
        Parameters
        ----------
        sio_client : SocketIOClient
            Used for subscribe/unsubscribe/reconnect and to read ``server_url``
            and ``connected`` state.
        graph : GraphManager
            Read-only access to node data (names, types).
        debug : bool
            Enable verbose logging.
        """
        self.sio_client = sio_client
        self.graph = graph
        self.debug = debug

        # Monitor state --------------------------------------------------------
        self.active_monitors: dict = {}   # monitor_id -> info dict
        self.monitor_lock = threading.RLock()
        self.simple_displays: dict = {}

        # Queues ---------------------------------------------------------------
        self.status_update_queue: Queue = Queue(maxsize=STATUS_QUEUE_SIZE)
        self.monitor_data_queue: Queue = Queue(maxsize=MONITOR_QUEUE_SIZE)

        # Update loop control --------------------------------------------------
        self.monitor_running: bool = False
        self._update_loop_active: bool = False
        self._last_queue_log: float = 0.0

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, message: str):
        if self.debug:
            print(f"[MONITOR_MGR] {message}")

    # ------------------------------------------------------------------
    # Server event callbacks (called by NodeManager)
    # ------------------------------------------------------------------

    def on_server_connect(self):
        """Notify all open monitors that the server is connected."""
        for monitor_id in self.active_monitors:
            self._safe_update_monitor_status(monitor_id, "connected")

    def on_server_disconnect(self):
        """Notify all open monitors that the server disconnected."""
        for monitor_id in self.active_monitors:
            self._safe_update_monitor_status(monitor_id, "disconnected")

    def on_server_connect_error(self, data):
        """Notify all open monitors of a connection error."""
        self._log(f"Connection error: {data}")
        for monitor_id in self.active_monitors:
            self._safe_update_monitor_status(monitor_id, "disconnected")

    def on_data_update(self, name: str, raw_data):
        """
        Receive a data frame from the server.
        Called from the Socket.IO background thread; only enqueues the data.
        """
        qsize = self.monitor_data_queue.qsize()
        print(f"[SOCKET.IO] Current queue size: {qsize}")
        if qsize >= MONITOR_QUEUE_SIZE:
            print(f"[SOCKET.IO] Queue full, dropping data for {name}")
            return

        with self.monitor_lock:
            matching = [
                (mid, info)
                for mid, info in self.active_monitors.items()
                if info.get("server_output_name") == name
            ]
            if not matching:
                print(f"[SOCKET.IO] No monitor found for {name}")
                return
            for monitor_id, info in matching:
                self.monitor_data_queue.put(
                    {
                        "type": "data_update",
                        "monitor_id": monitor_id,
                        "data": raw_data,
                        "timestamp": time.time(),
                    }
                )
                print(f"[SOCKET.IO] Queued data for monitor {monitor_id}")

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def _safe_update_monitor_status(self, monitor_id: str, status: str):
        """Thread-safe: enqueue a status update for the main thread."""
        try:
            if self.status_update_queue.full():
                try:
                    self.status_update_queue.get_nowait()
                except Exception:
                    pass
            self.status_update_queue.put(
                {
                    "type": "status_update",
                    "monitor_id": monitor_id,
                    "status": status,
                    "timestamp": time.time(),
                }
            )
        except Exception as e:
            print(f"[MONITOR] Error updating monitor status: {e}")

    def _update_monitor_status(self, monitor_id: str, status: str):
        """Direct DPG update (main thread only)."""
        if monitor_id not in self.active_monitors:
            return
        info = self.active_monitors[monitor_id]
        window_tag = info.get("window_id", "")
        if not window_tag or not dpg.does_item_exist(f"{window_tag}_status"):
            return

        colors = {
            "connected": [0, 255, 0],
            "disconnected": [255, 0, 0],
            "subscribed": [100, 255, 100],
            "unsubscribed": [255, 180, 100],
            "receiving": [0, 200, 255],
        }
        symbols = {
            "connected": "+",
            "disconnected": "-",
            "subscribed": ">",
            "unsubscribed": "=",
            "receiving": "<>",
        }
        color = colors.get(status, [200, 200, 200])
        symbol = symbols.get(status, "-")
        dpg.set_value(f"{window_tag}_status", f"{symbol} {status.capitalize()}")
        dpg.configure_item(f"{window_tag}_status", color=color)

    # ------------------------------------------------------------------
    # Open / close monitors
    # ------------------------------------------------------------------

    def open_monitor(self, sender, app_data, user_data):
        """
        Open a monitor window for a node output.  Intended as a DPG callback.
        ``user_data`` must be ``(node_uuid, output_name)``.
        """
        node_uuid, output_name = user_data
        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            self._log(f"Monitor: Node {node_uuid} not found")
            return

        node_name = node_data.get("name", "Unknown")
        node_type = node_data.get("type", "Unknown")

        print(f"\n[MONITOR] Opening monitor: {node_name}.{output_name}")
        print(f"  UUID: {node_uuid}, Type: {node_type}")

        # Resolve server output name -------------------------------------------
        try:
            server_output_name = self.sio_client.get_server_output_name(
                node_uuid, output_name, self.graph.nodes
            )
        except Exception as e:
            print(f"[MONITOR] Error getting server output name: {e}")
            server_output_name = f"{node_name}.{output_name}"
            print(f"[MONITOR] Using fallback: {server_output_name}")

        monitor_id = f"{node_uuid}_{output_name}_{int(time.time() * 1000)}"
        window_tag = f"monitor_{monitor_id}"

        if dpg.does_item_exist(window_tag):
            dpg.focus_item(window_tag)
            return

        # Window close callback ------------------------------------------------
        def close_callback():
            self.close_monitor(monitor_id, from_window_close=True)

        # Build monitor window -------------------------------------------------
        with dpg.window(
            label=f"Monitor: {node_name}.{output_name}",
            tag=window_tag,
            width=800,
            height=600,
            pos=[300, 300],
            on_close=close_callback,
        ):
            with dpg.collapsing_header(label="Connection Status", default_open=True):
                with dpg.group(tag=f"conn_{window_tag}"):
                    dpg.add_text("Server URL:", color=[200, 200, 200])
                    dpg.add_text(
                        self.sio_client.server_url,
                        color=[100, 255, 255],
                        tag=f"{window_tag}_url",
                    )
                    dpg.add_text("Status:", color=[200, 200, 200])
                    status_text = dpg.add_text(
                        "- Disconnected",
                        color=[255, 0, 0],
                        tag=f"{window_tag}_status",
                    )
                    dpg.add_text("Server Output Name:", color=[200, 200, 200])
                    dpg.add_text(
                        server_output_name,
                        color=[100, 255, 100],
                        tag=f"{window_tag}_output",
                    )

            dpg.add_separator()
            dpg.add_text("Data Plot", color=[200, 200, 255])
            plot_container = dpg.add_group(tag=f"{window_tag}_plot_container")
            placeholder = dpg.add_text(
                "Waiting for data...",
                color=[150, 150, 150],
                parent=plot_container,
                tag=f"{window_tag}_placeholder",
            )

            dpg.add_separator()
            with dpg.group(tag=f"info_{window_tag}"):
                dpg.add_text("Data Information:", color=[200, 200, 255])
                data_type_text = dpg.add_text(
                    "Type: Waiting for data...", color=[200, 200, 200]
                )
                data_shape_text = dpg.add_text("Shape: Unknown", color=[200, 200, 200])
                data_size_text = dpg.add_text("Size: Unknown", color=[200, 200, 200])
                data_range_text = dpg.add_text("Range: Unknown", color=[200, 200, 200])
                update_time_text = dpg.add_text(
                    "Last Update: Never", color=[200, 200, 200]
                )

            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Reconnect",
                    callback=lambda: self.sio_client.reconnect(),
                    width=100,
                )
                dpg.add_button(
                    label="Subscribe",
                    callback=lambda s, a, u=server_output_name: self._subscribe_output(u),
                    width=100,
                )

        # Register monitor -----------------------------------------------------
        self.active_monitors[monitor_id] = {
            "window_id": window_tag,
            "plot_container": plot_container,
            "placeholder_tag": placeholder,
            "data_type_text_id": data_type_text,
            "data_shape_text_id": data_shape_text,
            "data_size_text_id": data_size_text,
            "data_range_text_id": data_range_text,
            "update_time_text_id": update_time_text,
            "status_text_id": status_text,
            "node_uuid": node_uuid,
            "output_name": output_name,
            "server_output_name": server_output_name,
            "node_name": node_name,
            "update_count": 0,
            "last_update": 0,
            "socketio_connected": False,
            "subscribed": False,
            "dpg_plotter": None,
            "min_update_interval": 0.1,
            "pending_data": None,
            "skipped_frames": 0,
        }
        print(f"[MONITOR] Registered monitor {monitor_id} for {server_output_name}")

        if not self.monitor_running:
            self._start_monitor_updater()

        if not self.sio_client.connected:
            self.sio_client.reconnect()
        else:
            self._subscribe_output(server_output_name)

        self.monitor_running = True
        dpg.set_frame_callback(
            dpg.get_frame_count() + 1, self._monitor_update_frame
        )

    def close_monitor(self, monitor_id: str, from_window_close: bool = True):
        """
        Clean up a monitor window.

        Parameters
        ----------
        monitor_id       : ID of the monitor to close.
        from_window_close: If True, called from the DPG window-close callback
                           (do not delete the DPG item again).
        """
        print(f"\n[CLOSE_MONITOR] Called for {monitor_id}")

        with self.monitor_lock:
            if monitor_id not in self.active_monitors:
                print(f"[CLOSE_MONITOR] {monitor_id} not found")
                return
            info = self.active_monitors[monitor_id].copy()
            del self.active_monitors[monitor_id]
            print("[CLOSE_MONITOR] Removed from active_monitors")

        server_output_name = info.get("server_output_name")
        window_tag = info.get("window_id", "")
        plotter = info.get("dpg_plotter")

        if plotter:
            try:
                plotter.clear()
            except Exception as e:
                print(f"[CLOSE_MONITOR] Error clearing plotter: {e}")

        if not from_window_close and window_tag:
            try:
                if dpg.does_item_exist(window_tag):
                    dpg.delete_item(window_tag)
            except Exception as e:
                print(f"[CLOSE_MONITOR] Error deleting window: {e}")

        if monitor_id in self.simple_displays:
            try:
                self.simple_displays[monitor_id].cleanup()
                del self.simple_displays[monitor_id]
            except Exception as e:
                print(f"[CLOSE_MONITOR] Error cleaning up display: {e}")

        if server_output_name:
            self._unsubscribe_output(server_output_name)

        self._clear_queue_for_monitor(monitor_id)

        with self.monitor_lock:
            if not self.active_monitors and self.monitor_running:
                print("[CLOSE_MONITOR] No active monitors, stopping updater")
                self.monitor_running = False
                self._update_loop_active = False

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe helpers
    # ------------------------------------------------------------------

    def _subscribe_output(self, server_output_name: str):
        """Subscribe to a server output and notify affected monitors."""
        if not server_output_name:
            return
        with self.monitor_lock:
            if server_output_name not in self.sio_client.subscribed_outputs:
                self.sio_client.subscribed_outputs.add(server_output_name)
                for monitor_id, info in self.active_monitors.items():
                    if info.get("server_output_name") == server_output_name:
                        info["subscribed"] = True
                        self._safe_update_monitor_status(monitor_id, "subscribed")
            else:
                print(f"[SUBSCRIPTION] Already subscribed to {server_output_name}")

        if self.sio_client.connected:
            self.sio_client.request_next_frame()

    def _unsubscribe_output(self, server_output_name: str):
        """Unsubscribe from a server output if no monitors still need it."""
        if not server_output_name:
            return
        with self.monitor_lock:
            still_used = any(
                info.get("server_output_name") == server_output_name
                for info in self.active_monitors.values()
            )
            if not still_used:
                self.sio_client.subscribed_outputs.discard(server_output_name)
                self.sio_client.unsubscribe(server_output_name)
            else:
                print(
                    f"[UNSUBSCRIPTION] Other monitors still using "
                    f"{server_output_name}, keeping subscription"
                )

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------

    def _clear_queue_for_monitor(self, monitor_id: str):
        """Remove all queued items for a specific monitor."""
        temp_queue: Queue = Queue()
        removed = 0
        while not self.monitor_data_queue.empty():
            try:
                item = self.monitor_data_queue.get_nowait()
                if item.get("monitor_id") != monitor_id:
                    temp_queue.put(item)
                else:
                    removed += 1
            except Exception as e:
                print(f"[CLOSE_MONITOR] Error draining queue: {e}")
                break
        while not temp_queue.empty():
            try:
                self.monitor_data_queue.put(temp_queue.get_nowait())
            except Exception as e:
                print(f"[CLOSE_MONITOR] Error returning item to queue: {e}")
                break
        print(f"[CLOSE_MONITOR] Removed {removed} items from queue")

    # ------------------------------------------------------------------
    # Data info update (main thread)
    # ------------------------------------------------------------------

    def _update_data_info(self, info: dict, data):
        """Update the data info labels inside a monitor window."""
        try:
            if data is None:
                return
            data_type = type(data).__name__
            if isinstance(data, np.ndarray):
                data_type = f"ndarray ({data.dtype})"
            if dpg.does_item_exist(info.get("data_type_text_id", "")):
                dpg.set_value(info["data_type_text_id"], f"Type: {data_type}")

            if hasattr(data, "shape"):
                shape_str = str(data.shape)
                size_str = f"{data.size} elements"
            else:
                shape_str = "scalar"
                size_str = "1 element"

            if dpg.does_item_exist(info.get("data_shape_text_id", "")):
                dpg.set_value(info["data_shape_text_id"], f"Shape: {shape_str}")
            if dpg.does_item_exist(info.get("data_size_text_id", "")):
                dpg.set_value(info["data_size_text_id"], f"Size: {size_str}")

            if (
                isinstance(data, np.ndarray)
                and data.size > 0
                and np.issubdtype(data.dtype, np.number)
            ):
                range_str = f"[{data.min():.3g}, {data.max():.3g}]"
            elif isinstance(data, (int, float)):
                range_str = f"{data:.3g}"
            else:
                range_str = "N/A"

            if dpg.does_item_exist(info.get("data_range_text_id", "")):
                dpg.set_value(info["data_range_text_id"], f"Range: {range_str}")
        except Exception as e:
            print(f"[INFO] Error updating data info: {e}")

    # ------------------------------------------------------------------
    # Data processing and plotting (main thread)
    # ------------------------------------------------------------------

    def _process_and_plot_data_main_thread(
        self, monitor_id: str, raw_data, info: dict
    ) -> bool:
        """Convert raw server data to numpy and plot it via DPGPlotter."""
        try:
            with self.monitor_lock:
                if monitor_id not in self.active_monitors:
                    return False

            window_tag = info.get("window_id", "")
            if not window_tag or not dpg.does_item_exist(window_tag):
                with self.monitor_lock:
                    if monitor_id in self.active_monitors:
                        del self.active_monitors[monitor_id]
                return False

            plot_container = info.get("plot_container")
            if not plot_container or not dpg.does_item_exist(plot_container):
                return False

            placeholder = info.get("placeholder_tag")
            if placeholder and dpg.does_item_exist(placeholder):
                dpg.delete_item(placeholder)
                info["placeholder_tag"] = None

            if not isinstance(raw_data, dict):
                print(f"[MAIN_PLOT] Raw data is not a dict: {type(raw_data)}")
                return False

            data_type = raw_data.get("type")
            data_value = raw_data.get("data")
            shape = raw_data.get("shape")

            if data_value is None:
                return False

            # Convert to numpy -------------------------------------------------
            try:
                if data_type in ["1d_array", "2d_array", "scalar", "nd_array"]:
                    if isinstance(data_value, list):
                        data_array = np.array(data_value, dtype=np.float32)
                    elif isinstance(data_value, np.ndarray):
                        data_array = data_value.astype(np.float32)
                    else:
                        try:
                            data_array = np.array(
                                [float(data_value)], dtype=np.float32
                            )
                        except Exception as e:
                            print(f"[MAIN_PLOT] Error converting scalar: {e}")
                            data_array = np.array([0.0], dtype=np.float32)

                    if shape is not None and data_type != "scalar":
                        try:
                            if isinstance(shape, list):
                                shape = tuple(shape)
                            if np.prod(shape) == data_array.size:
                                data_array = data_array.reshape(shape)
                        except Exception as e:
                            print(f"[MAIN_PLOT] Error reshaping: {e}")

                elif data_type == "multi_data":
                    if isinstance(data_value, list) and data_value:
                        first = data_value[0]
                        data_array = (
                            np.array(first, dtype=np.float32)
                            if isinstance(first, list)
                            else first.astype(np.float32)
                        )
                        shapes = raw_data.get("shapes")
                        if shapes and np.prod(shapes[0]) == data_array.size:
                            try:
                                data_array = data_array.reshape(shapes[0])
                            except Exception:
                                pass
                    else:
                        return False
                else:
                    print(f"[MAIN_PLOT] Unknown data type: {data_type}")
                    return False
            except Exception as e:
                print(f"[MAIN_PLOT] Error converting data: {e}")
                traceback.print_exc()
                return False

            print(
                f"[MAIN_PLOT] shape={data_array.shape}, "
                f"type={data_type}, dtype={data_array.dtype}"
            )

            # Get or create plotter --------------------------------------------
            if info.get("dpg_plotter") is None:
                info["dpg_plotter"] = DPGPlotter(
                    parent_tag=plot_container, width=780, height=400
                )
            plotter = info["dpg_plotter"]

            # Plot -------------------------------------------------------------
            success = False
            ndim = data_array.ndim
            size = data_array.size

            if data_type == "scalar" or ndim == 0 or (ndim == 1 and size == 1):
                try:
                    success = plotter.plot_history(float(data_array.item()))
                except Exception as e:
                    print(f"[MAIN_PLOT] Error plotting scalar: {e}")

            elif ndim == 1:
                try:
                    success = plotter.plot_vector(data_array)
                    if not success:
                        success = plotter.plot_scatter(data_array)
                    if not success:
                        if dpg.does_item_exist(plot_container):
                            dpg.add_text(
                                f"Failed to plot 1D data. Length: {size}",
                                color=[255, 100, 100],
                                parent=plot_container,
                            )
                except Exception as e:
                    print(f"[MAIN_PLOT] Error plotting 1D vector: {e}")
                    traceback.print_exc()

            elif ndim == 2:
                h, w = data_array.shape
                pixel_count = h * w
                if pixel_count > 1_000_000:
                    info["min_update_interval"] = 0.5
                elif pixel_count > 250_000:
                    info["min_update_interval"] = 0.25
                elif pixel_count > 10_000:
                    info["min_update_interval"] = 0.1
                else:
                    info["min_update_interval"] = 0.05
                try:
                    success = plotter.plot_2d_image_clean(data_array)
                except Exception as e:
                    print(f"[MAIN_PLOT] Error plotting 2D image: {e}")

            elif ndim >= 3:
                try:
                    if data_array.shape[-1] <= 3:
                        reduced = np.mean(
                            data_array, axis=tuple(range(data_array.ndim - 2))
                        )
                    else:
                        reduced = data_array.reshape(-1, data_array.shape[-1])
                        if reduced.shape[0] > 1000:
                            reduced = reduced[:1000, :]
                    if reduced.ndim == 2:
                        success = plotter.plot_2d_image_clean(reduced)
                except Exception as e:
                    print(f"[MAIN_PLOT] Error plotting high-dim data: {e}")

            # Update info labels -----------------------------------------------
            if success:
                self._update_data_info(info, data_array)
                current_time_str = time.strftime("%H:%M:%S")
                if dpg.does_item_exist(info.get("update_time_text_id", "")):
                    dpg.set_value(
                        info["update_time_text_id"],
                        f"Last Update: {current_time_str}",
                    )
                info["last_update"] = time.time()
                info["update_count"] = info.get("update_count", 0) + 1
                if info["update_count"] % 10 == 0:
                    print(
                        f"[MAIN_PLOT] Updated {monitor_id} (#{info['update_count']})"
                    )
                return True
            else:
                print(f"[MAIN_PLOT] Failed to plot data for {monitor_id}")
                if dpg.does_item_exist(plot_container):
                    dpg.add_text(
                        f"Failed to plot. Shape: {data_array.shape}, Type: {data_type}",
                        color=[255, 100, 100],
                        parent=plot_container,
                    )
                return False

        except Exception as e:
            print(f"[MAIN_PLOT] Error: {e}")
            traceback.print_exc()
            return False

    # ------------------------------------------------------------------
    # Update loop (scheduled via DPG frame callbacks)
    # ------------------------------------------------------------------

    def _start_monitor_updater(self):
        """Start (or restart) the per-frame monitor update loop."""
        if not self.monitor_running:
            self.monitor_running = True
            print("[UPDATER] Starting monitor updater")
        current_frame = dpg.get_frame_count()
        dpg.set_frame_callback(current_frame + 1, self._monitor_update_frame)

    def _monitor_update_frame(self):
        """Per-frame callback: dequeue and apply all pending status/data items."""
        try:
            current_time = time.time()
            processed_count = 0
            skipped_count = 0

            with self.monitor_lock:
                active_snapshot = set(self.active_monitors.keys())

            # Status updates ---------------------------------------------------
            while not self.status_update_queue.empty():
                try:
                    item = self.status_update_queue.get_nowait()
                    if item.get("type") != "status_update":
                        continue
                    monitor_id = item.get("monitor_id")
                    status = item.get("status")
                    if monitor_id not in active_snapshot:
                        continue
                    if monitor_id not in self.active_monitors:
                        continue
                    info = self.active_monitors[monitor_id]
                    window_tag = info.get("window_id", "")
                    if not window_tag or not dpg.does_item_exist(
                        f"{window_tag}_status"
                    ):
                        continue
                    colors = {
                        "connected": [0, 255, 0],
                        "disconnected": [255, 0, 0],
                        "subscribed": [100, 255, 100],
                        "unsubscribed": [255, 180, 100],
                        "receiving": [0, 200, 255],
                    }
                    symbols = {
                        "connected": "+",
                        "disconnected": "-",
                        "subscribed": ">",
                        "unsubscribed": "=",
                        "receiving": "<>",
                    }
                    color = colors.get(status, [200, 200, 200])
                    symbol = symbols.get(status, "-")
                    dpg.set_value(
                        f"{window_tag}_status", f"{symbol} {status.capitalize()}"
                    )
                    dpg.configure_item(f"{window_tag}_status", color=color)
                except Exception as e:
                    print(f"[MONITOR] Error processing status update: {e}")
                    break

            # Data items -------------------------------------------------------
            for _ in range(MAX_QUEUE_ITEMS_PER_FRAME):
                if self.monitor_data_queue.empty():
                    break
                try:
                    item = self.monitor_data_queue.get_nowait()
                    if item.get("type") != "data_update":
                        continue
                    monitor_id = item.get("monitor_id")

                    with self.monitor_lock:
                        if monitor_id not in self.active_monitors:
                            continue
                        info = self.active_monitors[monitor_id]

                    window_tag = info.get("window_id", "")
                    if not window_tag or not dpg.does_item_exist(window_tag):
                        with self.monitor_lock:
                            if monitor_id in self.active_monitors:
                                del self.active_monitors[monitor_id]
                        continue

                    time_since = current_time - info.get("last_update", 0)
                    min_interval = info.get("min_update_interval", 0.1)

                    if time_since >= min_interval:
                        raw_data = item.get("data", {})
                        if self._process_and_plot_data_main_thread(
                            monitor_id, raw_data, info
                        ):
                            info["last_update"] = current_time
                            self._safe_update_monitor_status(monitor_id, "receiving")
                            processed_count += 1
                    else:
                        info["pending_data"] = item
                        info["skipped_frames"] = info.get("skipped_frames", 0) + 1
                        skipped_count += 1

                except Exception as e:
                    print(f"[MONITOR] Error processing queue item: {e}")
                    continue

            # Process pending data for monitors that were throttled -----------
            with self.monitor_lock:
                monitor_ids = list(self.active_monitors.keys())

            for monitor_id in monitor_ids:
                with self.monitor_lock:
                    if monitor_id not in self.active_monitors:
                        continue
                    info = self.active_monitors[monitor_id]
                if info.get("pending_data"):
                    time_since = current_time - info.get("last_update", 0)
                    if time_since >= info.get("min_update_interval", 0.1):
                        raw_data = info["pending_data"].get("data", {})
                        if self._process_and_plot_data_main_thread(
                            monitor_id, raw_data, info
                        ):
                            info["last_update"] = current_time
                            info["pending_data"] = None
                            self._safe_update_monitor_status(monitor_id, "receiving")

            # Request next frame if we processed data --------------------------
            if processed_count > 0 and self.sio_client.connected:
                if self.monitor_data_queue.qsize() < 20:
                    def _delayed_request():
                        time.sleep(0.02)
                        if self.sio_client.subscribed_outputs:
                            self.sio_client.request_next_frame()
                    threading.Thread(target=_delayed_request, daemon=True).start()

            # Debug queue status -----------------------------------------------
            qsize = self.monitor_data_queue.qsize()
            if qsize > 10 and current_time - self._last_queue_log > 2.0:
                print(
                    f"[QUEUE] Queue size: {qsize}, "
                    f"Processed: {processed_count}, Skipped: {skipped_count}"
                )
                self._last_queue_log = current_time

        except Exception as e:
            print(f"[MONITOR] Critical error in update loop: {e}")
            traceback.print_exc()

        finally:
            try:
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
                threading.Timer(0.1, self._start_monitor_updater).start()

    # ------------------------------------------------------------------
    # Periodic maintenance
    # ------------------------------------------------------------------

    def _monitor_queue_health(self):
        """Keep the monitor data queue from overflowing."""
        try:
            qsize = self.monitor_data_queue.qsize()
            from constants import STATUS_QUEUE_SIZE as _SQS
            if qsize > _SQS:
                print(f"[HEALTH] Queue overloaded ({qsize}), trimming...")
                latest: dict = {}
                non_data = []
                while not self.monitor_data_queue.empty():
                    try:
                        item = self.monitor_data_queue.get_nowait()
                        if item.get("type") == "data_update":
                            latest[item.get("monitor_id")] = item
                        else:
                            non_data.append(item)
                    except Exception:
                        break
                for item in list(latest.values()) + non_data:
                    try:
                        self.monitor_data_queue.put(item)
                    except Exception:
                        break
                print(
                    f"[HEALTH] Queue reduced to {self.monitor_data_queue.qsize()}"
                )
            next_frame = dpg.get_frame_count() + 60
            dpg.set_frame_callback(next_frame, self._monitor_queue_health)
        except Exception as e:
            print(f"[HEALTH] Error: {e}")

    def _periodic_cleanup(self):
        """Remove stale displays and dead monitor windows."""
        print("[CLEANUP] Performing periodic cleanup")

        # Remove simple_displays for closed monitors ---------------------------
        for monitor_id in list(self.simple_displays.keys()):
            if monitor_id not in self.active_monitors:
                print(f"[CLEANUP] Removing unused display for {monitor_id}")
                self.simple_displays[monitor_id].cleanup()
                del self.simple_displays[monitor_id]

        # Detect dead monitors (DPG window gone without close callback) --------
        dead = []
        for monitor_id, info in list(self.active_monitors.items()):
            window_tag = info.get("window_id")
            if not window_tag or not dpg.does_item_exist(window_tag):
                print(f"[CLEANUP] Removing dead monitor: {monitor_id}")
                dead.append(monitor_id)

        with self.monitor_lock:
            for monitor_id in dead:
                if monitor_id in self.simple_displays:
                    self.simple_displays[monitor_id].cleanup()
                    del self.simple_displays[monitor_id]
                del self.active_monitors[monitor_id]

        gc.collect()
        print(
            f"[CLEANUP] Done: {len(self.simple_displays)} displays, "
            f"{len(self.active_monitors)} monitors"
        )
        next_frame = dpg.get_frame_count() + 100
        dpg.set_frame_callback(next_frame, self._periodic_cleanup)

    def _check_memory_usage(self):
        """Log memory usage."""
        process = psutil.Process(os.getpid())
        mem = process.memory_info()
        print(
            f"[MEMORY] RSS: {mem.rss / 1024 / 1024:.1f} MB, "
            f"VMS: {mem.vms / 1024 / 1024:.1f} MB | "
            f"Monitors: {len(self.active_monitors)}, "
            f"Displays: {len(self.simple_displays)}"
        )
        dpg.set_frame_callback(dpg.get_frame_count() + 100, self._check_memory_usage)

    def _find_and_close_monitor(self, monitor_info):
        """Close a monitor identified by (node_uuid, output_name)."""
        node_uuid, output_name = monitor_info
        for monitor_id, info in list(self.active_monitors.items()):
            if (
                info.get("node_uuid") == node_uuid
                and info.get("output_name") == output_name
            ):
                self.close_monitor(monitor_id)
                break

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    def after_dpg_init(self):
        """Call once after DPG is fully initialised."""
        print("[MONITOR_MGR] DPG initialised, starting monitor updater")
        self._start_monitor_updater()

    def start_periodic_tasks(self):
        """Schedule all periodic maintenance callbacks."""
        current_frame = dpg.get_frame_count()
        print(f"[PERIODIC] Starting periodic tasks at frame {current_frame}")
        dpg.set_frame_callback(current_frame + 100, self._periodic_cleanup)
        dpg.set_frame_callback(current_frame + 100, self._check_memory_usage)
        dpg.set_frame_callback(current_frame + 100, self._monitor_queue_health)
        print("[PERIODIC] Periodic tasks scheduled")

    def cleanup(self):
        """Clean up all resources before application exit."""
        for output_name in list(self.sio_client.subscribed_outputs):
            self.sio_client.unsubscribe(output_name)
        self.monitor_running = False
        self.sio_client.disconnect()
        for monitor_id in list(self.active_monitors.keys()):
            self.close_monitor(monitor_id)
        self._log("Cleanup complete")
