#!/usr/bin/env python3
"""
monitor_window.py
=================
Standalone monitor window — runs as a completely independent subprocess.

Each instance owns:
  • its own DearPyGui context and viewport (separate OS window)
  • its own Socket.IO client connected directly to specula's display server
  • its own render loop with no coupling to the main GUI process

URL discovery
-------------
The display-server port is not known at spawn time (specula assigns it
dynamically).  The monitor resolves the actual URL by:

  1. Checking the coordination file (--server-url-file) written by the main
     process as soon as specula announces its port.
  2. Falling back to --server-url (the best-known URL at spawn time).

On every reconnect attempt the coordination file is re-read so that if the
simulation is restarted with a new port the monitor will pick it up
automatically.
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
from queue import Empty, Queue

import numpy as np
import socketio as sio_module
import dearpygui.dearpygui as dpg

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from dpg_plotting import DPGPlotter
from constants import (
    FONT_SIZE,
    MAX_QUEUE_ITEMS_PER_FRAME,
    MONITOR_QUEUE_SIZE,
)

try:
    import matplotlib
    _FONT_PATH = os.path.join(
        matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSerif.ttf"
    )
except Exception:
    _FONT_PATH = None


# =============================================================================
# StandaloneMonitor
# =============================================================================

class StandaloneMonitor:
    """
    Self-contained monitor window.

    The DPG render loop runs on the main thread.
    The Socket.IO client runs its own background thread.
    Data arriving on the sio thread is placed in *data_queue* and consumed on
    the main thread, so DPG is never touched from a worker thread.
    """

    _TAG_WINDOW    = "monitor_main"
    _TAG_STATUS    = "status_text"
    _TAG_PLOT_GRP  = "plot_container"
    _TAG_PHOLDER   = "placeholder_text"
    _TAG_INFO_TYPE = "info_type"
    _TAG_INFO_SHP  = "info_shape"
    _TAG_INFO_RNG  = "info_range"
    _TAG_INFO_TIME = "info_time"
    _TAG_URL_TXT   = "url_text"

    def __init__(
        self,
        server_url: str,
        server_url_file: str | None,
        server_output_name: str,
        node_name: str,
        output_name: str,
    ):
        self.server_url          = server_url
        self.server_url_file     = server_url_file
        self.server_output_name  = server_output_name
        self.node_name           = node_name
        self.output_name         = output_name

        # Thread-safe data queue (sio thread → main thread)
        # Each item is {"data": <inner-payload-dict>, "timestamp": float}
        # where inner-payload = {"type": "...", "data": [...], "shape": [...]}
        self.data_queue: Queue = Queue(maxsize=MONITOR_QUEUE_SIZE)

        # Socket.IO state
        self.sio       = None
        self.connected = False
        self._sio_lock = threading.Lock()

        # Render state
        self.dpg_plotter: DPGPlotter | None = None
        self.last_update: float   = 0.0
        self.update_count: int    = 0
        self.min_update_interval: float = 0.05

        # Pending status/URL label (set from sio thread, applied on main thread)
        self._pending_status: str | None = None
        self._pending_url: str | None    = None
        self._status_lock = threading.Lock()

        # Stop flag for the connection loop
        self._stop_flag = threading.Event()

    # =========================================================================
    # URL resolution
    # =========================================================================

    def _resolve_server_url(self) -> str:
        """
        Return the most up-to-date server URL.
        Always re-reads the coordination file so we pick up any port change
        written by SimulationControl after specula announces its port.
        """
        if self.server_url_file and os.path.exists(self.server_url_file):
            try:
                with open(self.server_url_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                url = data.get("url", "").strip()
                if url:
                    if url != self.server_url:
                        print(f"[MONITOR] URL updated from file: {self.server_url} → {url}")
                        self.server_url = url
                        with self._status_lock:
                            self._pending_url = url
                    return url
            except Exception as e:
                print(f"[MONITOR] Could not read URL file: {e}")
        return self.server_url

    # =========================================================================
    # Socket.IO
    # =========================================================================

    def _build_sio_client(self):
        if os.name == "nt":
            client = sio_module.Client(
                logger=False,
                engineio_logger=False,
                reconnection=False,   # we handle reconnect ourselves
            )
        else:
            client = sio_module.Client(logger=False, engineio_logger=False)

        @client.event
        def connect():
            self.connected = True
            print(f"[MONITOR] Connected to {self.server_url}")
            self._set_status("connected")
            # Subscribe to our output immediately after connecting
            try:
                client.emit("newdata", [self.server_output_name])
                self._set_status("subscribed")
                print(f"[MONITOR] Subscribed to {self.server_output_name}")
            except Exception as e:
                print(f"[MONITOR] Subscription error: {e}")

        @client.event
        def disconnect():
            self.connected = False
            print("[MONITOR] Disconnected")
            self._set_status("disconnected")

        @client.event
        def connect_error(data):
            self.connected = False
            msg = str(data)
            if len(msg) > 120:
                msg = msg[:120] + "…"
            print(f"[MONITOR] Connection error: {msg}")
            self._set_status("retrying")

        @client.event
        def data_update(data):
            """
            Called on the sio background thread — only enqueue, never touch DPG.

            The server sends:
                {"name": "node.output", "data": {"type": "...", "data": [...], "shape": [...]}}

            We extract the inner payload ("data" key) so that _raw_to_numpy
            receives {"type": ..., "data": ..., "shape": ...} directly —
            matching the structure expected by the old MonitorManager.
            """
            name         = data.get("name")
            inner_payload = data.get("data")   # ← extract inner payload here

            if name != self.server_output_name:
                return
            if inner_payload is None:
                print(f"[MONITOR] data_update: missing inner payload for {name}")
                return

            if self.data_queue.full():
                try:
                    self.data_queue.get_nowait()   # drop oldest frame
                except Empty:
                    pass
            # Queue the inner payload, not the outer envelope
            self.data_queue.put({"data": inner_payload, "timestamp": time.time()})

        @client.event
        def done(data):
            """
            Specula signals end-of-step — request the next frame so we keep
            receiving updates as the simulation steps forward.
            """
            if self.connected:
                try:
                    client.emit("newdata", [self.server_output_name])
                except Exception:
                    pass

        return client

    def _connection_loop(self):
        """
        Background thread: keep trying to connect, re-reading the URL file on
        every attempt so we pick up the correct port as soon as specula starts.
        """
        retry_delay = 1.0
        max_delay   = 10.0

        while not self._stop_flag.is_set():
            if self.connected:
                time.sleep(2.0)
                continue

            url = self._resolve_server_url()

            try:
                print(f"[MONITOR] Connecting to {url} …")
                with self._sio_lock:
                    if self.sio:
                        try:
                            self.sio.disconnect()
                        except Exception:
                            pass
                    self.sio = self._build_sio_client()
                self.sio.connect(url, namespaces=["/"])
                retry_delay = 1.0   # reset back-off on success
            except Exception as e:
                err = str(e)
                if len(err) > 120:
                    err = err[:120] + "…"
                print(f"[MONITOR] connection failed, new attempt in {retry_delay:.2g} s")
                self._set_status("retrying")
                self._stop_flag.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 1.5, max_delay)

    # =========================================================================
    # DPG setup
    # =========================================================================

    def _build_ui(self):
        dpg.create_context()

        if _FONT_PATH and os.path.exists(_FONT_PATH):
            with dpg.font_registry():
                dpg.bind_font(dpg.add_font(_FONT_PATH, FONT_SIZE))

        title = f"Monitor: {self.node_name}.{self.output_name}"

        with dpg.window(label=title, tag=self._TAG_WINDOW):

            with dpg.collapsing_header(label="Connection", default_open=False):
                dpg.add_text(
                    f"Server:  {self.server_url}",
                    color=[150, 150, 150],
                    tag=self._TAG_URL_TXT,
                )
                dpg.add_text(
                    f"Output:  {self.server_output_name}", color=[100, 255, 100]
                )
                dpg.add_text(
                    "Status:  Connecting …",
                    color=[255, 200, 0],
                    tag=self._TAG_STATUS,
                )
                dpg.add_button(
                    label="Reconnect",
                    callback=lambda: threading.Thread(
                        target=self._do_reconnect, daemon=True
                    ).start(),
                )

            dpg.add_separator()
            dpg.add_text(
                "Waiting for data …", color=[150, 150, 150], tag=self._TAG_PHOLDER
            )
            dpg.add_group(tag=self._TAG_PLOT_GRP)
            dpg.add_separator()
            dpg.add_text("Type:    —", color=[200, 200, 200], tag=self._TAG_INFO_TYPE)
            dpg.add_text("Shape:   —", color=[200, 200, 200], tag=self._TAG_INFO_SHP)
            dpg.add_text("Range:   —", color=[200, 200, 200], tag=self._TAG_INFO_RNG)
            dpg.add_text("Updated: never", color=[200, 200, 200], tag=self._TAG_INFO_TIME)

        dpg.create_viewport(title=title, width=920, height=720)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window(self._TAG_WINDOW, True)

    def _do_reconnect(self):
        """Force a reconnect (called from background thread via button)."""
        self.connected = False
        # The connection loop will wake up and retry on the next cycle.

    # =========================================================================
    # Data conversion + plotting (main thread only)
    # =========================================================================

    def _raw_to_numpy(self, inner_payload: dict):
        """
        Convert the inner payload dict to a float32 numpy array.

        Expected structure (matches what specula's display server sends):
            {
                "type":  "1d_array" | "2d_array" | "scalar" | "nd_array" | "multi_data",
                "data":  <list or nested list>,
                "shape": <list of ints>   (optional)
            }
        """
        data_type  = inner_payload.get("type")
        data_value = inner_payload.get("data")
        shape      = inner_payload.get("shape")

        if data_value is None:
            print(f"[MONITOR] _raw_to_numpy: no 'data' key in payload, keys={list(inner_payload.keys())}")
            return None

        if data_type is None:
            print(f"[MONITOR] _raw_to_numpy: no 'type' key in payload, keys={list(inner_payload.keys())}")

        try:
            if data_type in ("1d_array", "2d_array", "scalar", "nd_array") or data_type is None:
                if isinstance(data_value, list):
                    arr = np.array(data_value, dtype=np.float32)
                elif isinstance(data_value, np.ndarray):
                    arr = data_value.astype(np.float32)
                else:
                    arr = np.array([float(data_value)], dtype=np.float32)

                if shape is not None and data_type != "scalar":
                    try:
                        tshape = tuple(shape) if isinstance(shape, list) else shape
                        if np.prod(tshape) == arr.size:
                            arr = arr.reshape(tshape)
                    except Exception:
                        pass
                return arr

            if data_type == "multi_data":
                if isinstance(data_value, list) and data_value:
                    first = data_value[0]
                    arr = (
                        np.array(first, dtype=np.float32)
                        if isinstance(first, list)
                        else first.astype(np.float32)
                    )
                    shapes = inner_payload.get("shapes")
                    if shapes and np.prod(shapes[0]) == arr.size:
                        arr = arr.reshape(tuple(shapes[0]))
                    return arr

        except Exception as e:
            print(f"[MONITOR] Data conversion error: {e}")
            traceback.print_exc()

        print(f"[MONITOR] _raw_to_numpy: unhandled type '{data_type}'")
        return None

    def _plot(self, arr: np.ndarray) -> bool:
        if dpg.does_item_exist(self._TAG_PHOLDER):
            dpg.delete_item(self._TAG_PHOLDER)

        if self.dpg_plotter is None:
            self.dpg_plotter = DPGPlotter(
                parent_tag=self._TAG_PLOT_GRP, width=880, height=500
            )

        p    = self.dpg_plotter
        ndim = arr.ndim
        size = arr.size

        try:
            if ndim == 0 or (ndim == 1 and size == 1):
                return p.plot_history(float(arr.item()))

            if ndim == 1:
                ok = p.plot_vector(arr)
                return ok if ok else p.plot_scatter(arr)

            if ndim == 2:
                h, w = arr.shape
                px = h * w
                self.min_update_interval = (
                    0.5  if px > 1_000_000 else
                    0.25 if px > 250_000   else
                    0.1  if px > 10_000    else 0.05
                )
                return p.plot_2d_image_clean(arr)

            if ndim >= 3:
                if arr.shape[-1] <= 3:
                    reduced = np.mean(arr, axis=tuple(range(arr.ndim - 2)))
                else:
                    reduced = arr.reshape(-1, arr.shape[-1])[:1000]
                if reduced.ndim == 2:
                    return p.plot_2d_image_clean(reduced)

        except Exception as e:
            print(f"[MONITOR] Plot error: {e}")
            traceback.print_exc()

        return False

    def _update_info_labels(self, arr: np.ndarray):
        dtype_str = (
            f"ndarray ({arr.dtype})" if isinstance(arr, np.ndarray) else type(arr).__name__
        )
        shape_str = str(arr.shape) if hasattr(arr, "shape") else "scalar"
        if (
            isinstance(arr, np.ndarray)
            and arr.size > 0
            and np.issubdtype(arr.dtype, np.number)
        ):
            range_str = f"[{arr.min():.4g}, {arr.max():.4g}]"
        else:
            range_str = "N/A"
        ts = time.strftime("%H:%M:%S")
        for tag, text in (
            (self._TAG_INFO_TYPE, f"Type:    {dtype_str}"),
            (self._TAG_INFO_SHP,  f"Shape:   {shape_str}"),
            (self._TAG_INFO_RNG,  f"Range:   {range_str}"),
            (self._TAG_INFO_TIME, f"Updated: {ts}  (#{self.update_count})"),
        ):
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, text)

    # =========================================================================
    # Per-frame work (main thread)
    # =========================================================================

    _STATUS_COLORS = {
        "connected":    [0, 255, 0],
        "subscribed":   [100, 255, 100],
        "receiving":    [0, 200, 255],
        "disconnected": [255, 80, 80],
        "error":        [255, 80, 80],
        "retrying":     [255, 180, 0],
    }
    _STATUS_LABELS = {
        "connected":    "+ Connected",
        "subscribed":   "> Subscribed",
        "receiving":    "<> Receiving",
        "disconnected": "- Disconnected",
        "error":        "! Error",
        "retrying":     "~ Retrying …",
    }

    def _set_status(self, status: str):
        with self._status_lock:
            self._pending_status = status

    def _apply_pending_status(self):
        with self._status_lock:
            status  = self._pending_status
            new_url = self._pending_url
            self._pending_status = None
            self._pending_url    = None

        if status and dpg.does_item_exist(self._TAG_STATUS):
            label = self._STATUS_LABELS.get(status, status.capitalize())
            color = self._STATUS_COLORS.get(status, [200, 200, 200])
            dpg.set_value(self._TAG_STATUS, f"Status:  {label}")
            dpg.configure_item(self._TAG_STATUS, color=color)

        if new_url and dpg.does_item_exist(self._TAG_URL_TXT):
            dpg.set_value(self._TAG_URL_TXT, f"Server:  {new_url}")

    def _drain_queue(self):
        now = time.time()
        for _ in range(MAX_QUEUE_ITEMS_PER_FRAME):
            try:
                item = self.data_queue.get_nowait()
            except Empty:
                break

            if now - self.last_update < self.min_update_interval:
                continue

            arr = self._raw_to_numpy(item["data"])
            if arr is None:
                continue

            if self._plot(arr):
                self._update_info_labels(arr)
                self.last_update  = now
                self.update_count += 1
                self._set_status("receiving")

    # =========================================================================
    # Main entry point
    # =========================================================================

    def run(self):
        self._build_ui()

        # Start connection loop in a daemon thread
        threading.Thread(target=self._connection_loop, daemon=True).start()

        # Render loop
        while dpg.is_dearpygui_running():
            self._apply_pending_status()
            self._drain_queue()
            dpg.render_dearpygui_frame()

        # Cleanup
        self._stop_flag.set()
        with self._sio_lock:
            if self.sio and self.connected:
                try:
                    self.sio.disconnect()
                except Exception:
                    pass
        dpg.destroy_context()


# =============================================================================
# CLI entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Specula standalone monitor window")
    parser.add_argument(
        "--server-url", default="http://127.0.0.1:5000",
        help="Initial server URL (may be overridden by --server-url-file)",
    )
    parser.add_argument(
        "--server-url-file", default=None,
        help="Path to JSON coordination file written by SimulationControl",
    )
    parser.add_argument(
        "--server-output-name", required=True,
        help="Fully-qualified server output name (e.g. 'my_node.out_slopes')",
    )
    parser.add_argument("--node-name",   required=True)
    parser.add_argument("--output-name", required=True)
    args = parser.parse_args()

    monitor = StandaloneMonitor(
        server_url         = args.server_url,
        server_url_file    = args.server_url_file,
        server_output_name = args.server_output_name,
        node_name          = args.node_name,
        output_name        = args.output_name,
    )
    monitor.run()


if __name__ == "__main__":
    main()