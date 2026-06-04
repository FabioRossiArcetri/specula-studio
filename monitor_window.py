"""
monitor_window.py
=================
Standalone monitor window — runs as an independent subprocess.

Changes vs. previous version
-----------------------------
* Accepts ``--session-id`` CLI argument (Issue 9).  Every reconnect attempt
  reads the coordination file and compares its ``session_id`` field against the
  value we were launched with.  If they differ the file belongs to a newer run
  and this process exits cleanly instead of hijacking the new server.
* ``_drain_queue`` applies the same back-pressure policy as InProcessMonitor:
  when the minimum display interval has not elapsed, drain at most one item and
  skip rendering rather than consuming CPU dequeuing frames we won't show (Issue 6).
* Drop counter: old-style ``get_nowait()`` + ``put()`` replaced by
  ``put_nowait`` + per-window drop counter logged periodically (Issue 6).
* ``done`` handler no longer re-emits ``newdata`` directly; the stall watchdog
  in the connection loop handles re-arming so a single missed ``done`` cannot
  permanently stop the stream (Issue 2).
* Envelope format follows the canonical two-layer structure:
  outer ``{"name": ..., "data": <inner_payload>}``; inner payload has
  ``type/data/shape`` fields — identical to what InProcessMonitor receives
  from MonitorBus (Issue 3).
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
from queue import Empty, Full, Queue

import numpy as np
import socketio as sio_module
import dearpygui.dearpygui as dpg

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from dpg_plotting import DPGPlotter
from constants import (
    MAX_QUEUE_ITEMS_PER_FRAME,
    MONITOR_QUEUE_SIZE,
    STREAM_STALL_TIMEOUT,
    MONITOR_DROP_LOG_INTERVAL,
)

FONT_SIZE = 18

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
    Data is placed in a thread-safe Queue and consumed on the main thread.
    """

    _TAG_VEC_MODE       = "vector_display_mode"
    _TAG_WINDOW         = "monitor_main"
    _TAG_STATUS         = "status_text"
    _TAG_PLOT_CONTAINER = "plot_container"
    _TAG_PHOLDER        = "placeholder_text"
    _TAG_INFO_TYPE      = "info_type"
    _TAG_INFO_SHP       = "info_shape"
    _TAG_INFO_RNG       = "info_range"
    _TAG_INFO_TIME      = "info_time"
    _TAG_URL_TXT        = "url_text"

    def __init__(
        self,
        server_url: str,
        server_url_file: str | None,
        server_output_name: str,
        node_name: str,
        output_name: str,
        session_id: str = "",
    ):
        self.server_url         = server_url
        self.server_url_file    = server_url_file
        self.server_output_name = server_output_name
        self.node_name          = node_name
        self.output_name        = output_name
        self.session_id         = session_id   # Issue 9

        # Thread-safe data queue (sio thread → main thread)
        self.data_queue: Queue = Queue(maxsize=MONITOR_QUEUE_SIZE)

        # Drop counter (Issue 6)
        self._drop_count: int = 0

        # Socket.IO state
        self.sio       = None
        self.connected = False
        self._sio_lock = threading.Lock()

        # Stall watchdog (Issue 2)
        self._stall_timer: threading.Timer | None = None
        self._stall_lock = threading.Lock()

        # Render state
        self.dpg_plotter: DPGPlotter | None = None
        self.last_update: float  = 0.0
        self.update_count: int   = 0
        self.min_update_interval = 0.05

        # Window dimensions
        self.window_width        = 920
        self.window_height       = 720
        self.plot_width          = 880
        self.plot_height         = 500
        self.last_container_width  = 0
        self.last_container_height = 0

        # Pending UI updates (set from sio thread, applied on main thread)
        self._pending_status: str | None = None
        self._pending_url: str | None    = None
        self._status_lock = threading.Lock()

        # Stop flag
        self._stop_flag = threading.Event()

        # Mouse state
        self.mouse_down = False

    # =========================================================================
    # Coordination file / URL resolution  (Issue 9)
    # =========================================================================

    def _resolve_server_url(self) -> tuple[str, bool]:
        """
        Return (url, session_ok).

        Reads the coordination file if available.  If the stored session_id
        does not match ours, ``session_ok`` is False and the caller should
        stop reconnecting and exit.
        """
        if self.server_url_file and os.path.exists(self.server_url_file):
            try:
                with open(self.server_url_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                file_session = data.get("session_id", "")
                # If we were launched with a session_id and the file now contains
                # a *different* non-empty session_id, a new run has started.
                if (
                    self.session_id
                    and file_session
                    and file_session != self.session_id
                ):
                    print(
                        f"[MONITOR] Coordination file session changed "
                        f"({self.session_id!r} → {file_session!r}). "
                        "Exiting — this monitor belongs to the previous run."
                    )
                    return self.server_url, False

                url = data.get("url", "").strip()
                if url and url != self.server_url:
                    print(f"[MONITOR] URL updated from file: {self.server_url} → {url}")
                    self.server_url = url
                    with self._status_lock:
                        self._pending_url = url
                if url:
                    return url, True
            except Exception as e:
                print(f"[MONITOR] Could not read URL file: {e}")
        return self.server_url, True

    # =========================================================================
    # Stall watchdog  (Issue 2)
    # =========================================================================

    def _arm_stall_watchdog(self):
        with self._stall_lock:
            if self._stall_timer is not None:
                self._stall_timer.cancel()
            self._stall_timer = threading.Timer(
                STREAM_STALL_TIMEOUT, self._on_stall_detected
            )
            self._stall_timer.daemon = True
            self._stall_timer.start()

    def _disarm_stall_watchdog(self):
        with self._stall_lock:
            if self._stall_timer is not None:
                self._stall_timer.cancel()
                self._stall_timer = None

    def _on_stall_detected(self):
        if not self.connected:
            return
        print(
            f"[MONITOR] Stream stall detected (no 'done' in {STREAM_STALL_TIMEOUT}s). "
            "Re-arming pull cycle."
        )
        self._set_status("retrying")
        with self._sio_lock:
            sio = self.sio
        if sio and self.connected:
            try:
                sio.emit("newdata", [self.server_output_name])
                self._arm_stall_watchdog()
            except Exception:
                pass

    # =========================================================================
    # Socket.IO
    # =========================================================================

    def _build_sio_client(self):
        if os.name == "nt":
            client = sio_module.Client(
                logger=False, engineio_logger=False, reconnection=False
            )
        else:
            client = sio_module.Client(logger=False, engineio_logger=False)

        @client.event
        def connect():
            self.connected = True
            print(f"[MONITOR] Connected to {self.server_url}")
            self._set_status("connected")
            try:
                client.emit("newdata", [self.server_output_name])
                self._arm_stall_watchdog()
                self._set_status("subscribed")
                print(f"[MONITOR] Subscribed to {self.server_output_name}")
            except Exception as e:
                print(f"[MONITOR] Subscription error: {e}")

        @client.event
        def disconnect():
            self.connected = False
            self._disarm_stall_watchdog()
            print("[MONITOR] Disconnected")
            self._set_status("disconnected")

        @client.event
        def connect_error(data):
            self.connected = False
            self._disarm_stall_watchdog()
            msg = str(data)[:120]
            print(f"[MONITOR] Connection error: {msg}")
            self._set_status("retrying")

        @client.event
        def data_update(data):
            """Called on the sio background thread — enqueue only, never touch DPG."""
            # Canonical envelope: {"name": str, "data": <inner_payload>}
            name    = data.get("name")
            payload = data.get("data")

            if name != self.server_output_name:
                return
            if payload is None:
                return

            try:
                self.data_queue.put_nowait({"payload": payload, "timestamp": time.time()})
            except Full:
                # Drop oldest to make room  (Issue 6)
                try:
                    self.data_queue.get_nowait()
                except Empty:
                    pass
                try:
                    self.data_queue.put_nowait({"payload": payload, "timestamp": time.time()})
                except Full:
                    pass
                self._drop_count += 1
                if self._drop_count % MONITOR_DROP_LOG_INTERVAL == 0:
                    print(
                        f"[MONITOR] '{self.server_output_name}': "
                        f"{self._drop_count} frames dropped (queue full)"
                    )

        @client.event
        def done(data):
            """
            End-of-step signal.  Disarm the watchdog for this cycle and
            re-arm for the next one before emitting newdata.
            """
            self._disarm_stall_watchdog()
            if self.connected:
                try:
                    client.emit("newdata", [self.server_output_name])
                    self._arm_stall_watchdog()
                except Exception:
                    pass

        @client.event
        def heartbeat(data):
            """Server keepalive — verify we are still talking to the right run."""
            server_sid = data.get("session_id") if isinstance(data, dict) else None
            if server_sid and self.session_id and server_sid != self.session_id:
                print(
                    f"[MONITOR] Heartbeat session mismatch "
                    f"(mine={self.session_id!r}, server={server_sid!r}). Exiting."
                )
                self._stop_flag.set()

        return client

    def _connection_loop(self):
        """Background thread: keep trying to connect with exponential back-off."""
        retry_delay = 1.0
        max_delay   = 10.0

        while not self._stop_flag.is_set():
            if self.connected:
                time.sleep(2.0)
                continue

            url, session_ok = self._resolve_server_url()
            if not session_ok:
                self._stop_flag.set()
                break

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
                retry_delay = 1.0
            except Exception as e:
                err = str(e)[:120]
                print(f"[MONITOR] Connection failed ({err}), retry in {retry_delay:.1f}s")
                self._set_status("retrying")
                self._stop_flag.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 1.5, max_delay)

    # =========================================================================
    # DPG setup and input handling
    # =========================================================================

    def _on_mouse_move(self, sender, app_data):
        try:
            if self.dpg_plotter and self.dpg_plotter.image_viewer:
                self.dpg_plotter.image_viewer.handle_mouse_move(app_data[0], app_data[1])
        except Exception:
            pass

    def _on_mouse_scroll(self, sender, app_data):
        try:
            if self.dpg_plotter and self.dpg_plotter.image_viewer:
                self.dpg_plotter.image_viewer.handle_mouse_scroll(app_data)
        except Exception:
            pass

    def _on_mouse_down(self, sender, app_data):
        try:
            if self.dpg_plotter and self.dpg_plotter.image_viewer:
                self.dpg_plotter.image_viewer.start_drag(app_data[0], app_data[1])
                self.mouse_down = True
        except Exception:
            pass

    def _on_mouse_up(self, sender, app_data):
        try:
            if self.dpg_plotter and self.dpg_plotter.image_viewer:
                self.dpg_plotter.image_viewer.end_drag()
                self.mouse_down = False
        except Exception:
            pass

    def _build_ui(self):
        dpg.create_context()

        if _FONT_PATH and os.path.exists(_FONT_PATH):
            with dpg.font_registry():
                dpg.bind_font(dpg.add_font(_FONT_PATH, FONT_SIZE))

        title = f"Monitor: {self.node_name}.{self.output_name}"

        with dpg.window(
            label=title,
            tag=self._TAG_WINDOW,
            no_close=False,
            width=self.window_width,
            height=self.window_height,
            on_close=lambda: self._stop_flag.set(),
        ):
            with dpg.collapsing_header(label="Settings", default_open=True):
                with dpg.group(horizontal=True):
                    dpg.add_text("Vector Mode:")
                    dpg.add_radio_button(
                        items=["Snapshot", "Time Series"],
                        default_value="Snapshot",
                        horizontal=True,
                        tag=self._TAG_VEC_MODE,
                        callback=lambda s, a: (
                            self.dpg_plotter.set_vector_mode(
                                "history" if "Time" in a else "snapshot"
                            ) if self.dpg_plotter else None
                        ),
                    )

            dpg.add_separator()

            with dpg.collapsing_header(label="Connection", default_open=False):
                dpg.add_text(
                    f"Server:  {self.server_url}",
                    color=[150, 150, 150],
                    tag=self._TAG_URL_TXT,
                )
                dpg.add_text(f"Output:  {self.server_output_name}", color=[100, 255, 100])
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

            with dpg.child_window(
                border=True, width=-1, height=-115, tag=self._TAG_PLOT_CONTAINER
            ):
                dpg.add_text(
                    "Waiting for data …",
                    color=[150, 150, 150],
                    tag=self._TAG_PHOLDER,
                )

            dpg.add_separator()

            with dpg.group(horizontal=False):
                dpg.add_text("Type:    —", color=[200, 200, 200], tag=self._TAG_INFO_TYPE)
                dpg.add_text("Shape:   —", color=[200, 200, 200], tag=self._TAG_INFO_SHP)
                dpg.add_text("Range:   —", color=[200, 200, 200], tag=self._TAG_INFO_RNG)
                dpg.add_text("Updated: never", color=[200, 200, 200], tag=self._TAG_INFO_TIME)

        with dpg.handler_registry():
            dpg.add_mouse_move_handler(callback=self._on_mouse_move)
            dpg.add_mouse_wheel_handler(callback=self._on_mouse_scroll)
            dpg.add_mouse_click_handler(callback=self._on_mouse_down)
            dpg.add_mouse_release_handler(callback=self._on_mouse_up)

        dpg.create_viewport(title=title, width=self.window_width, height=self.window_height)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window(self._TAG_WINDOW, True)

    def _do_reconnect(self):
        self.connected = False

    def _update_responsive_layout(self):
        try:
            if not dpg.does_item_exist(self._TAG_PLOT_CONTAINER):
                return
            cw = dpg.get_item_width(self._TAG_PLOT_CONTAINER)
            ch = dpg.get_item_height(self._TAG_PLOT_CONTAINER)
            if (
                abs(cw - self.last_container_width) > 5
                or abs(ch - self.last_container_height) > 5
            ):
                self.plot_width            = cw
                self.plot_height           = ch
                self.last_container_width  = cw
                self.last_container_height = ch
                if self.dpg_plotter is not None:
                    self.dpg_plotter.update_size(cw, ch)
        except Exception:
            pass

    # =========================================================================
    # Data conversion + plotting  (Issue 3 — canonical payload format)
    # =========================================================================

    def _raw_to_numpy(self, payload: dict) -> np.ndarray | None:
        """
        Convert the inner payload dict to a float32 numpy array.

        Canonical inner payload (shared by both monitor paths):
            {
                "type":  "1d_array" | "2d_array" | "scalar" | "nd_array" | "multi_data",
                "data":  <list or numpy array>,
                "shape": <list of ints>   (optional),
            }
        """
        data_type  = payload.get("type")
        data_value = payload.get("data")
        shape      = payload.get("shape")

        if data_value is None:
            return None

        try:
            if data_type in ("1d_array", "2d_array", "scalar", "nd_array") or data_type is None:
                if isinstance(data_value, list):
                    arr = np.array(data_value, dtype=np.float32)
                elif isinstance(data_value, np.ndarray):
                    arr = data_value.astype(np.float32, copy=False)
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
                    arr   = (
                        np.array(first, dtype=np.float32)
                        if isinstance(first, list)
                        else np.asarray(first, dtype=np.float32)
                    )
                    shapes = payload.get("shapes")
                    if shapes and np.prod(shapes[0]) == arr.size:
                        arr = arr.reshape(tuple(shapes[0]))
                    return arr

        except Exception as exc:
            print(f"[MONITOR] Data conversion error: {exc}")
            traceback.print_exc()

        print(f"[MONITOR] Unhandled payload type: '{data_type}'")
        return None

    def _plot(self, arr: np.ndarray) -> bool:
        if dpg.does_item_exist(self._TAG_PHOLDER):
            dpg.delete_item(self._TAG_PHOLDER)

        if self.dpg_plotter is None:
            self.dpg_plotter = DPGPlotter(
                parent_tag=self._TAG_PLOT_CONTAINER,
                width=self.plot_width,
                height=self.plot_height,
            )
            mode_val = dpg.get_value(self._TAG_VEC_MODE)
            self.dpg_plotter.set_vector_mode("history" if "Time" in mode_val else "snapshot")

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
                px   = h * w
                self.min_update_interval = (
                    0.5  if px > 1_000_000 else
                    0.25 if px > 250_000   else
                    0.1  if px > 10_000    else 0.05
                )
                return p.plot_2d_image_clean(arr)
            if ndim >= 3:
                reduced = (
                    np.mean(arr, axis=tuple(range(arr.ndim - 2)))
                    if arr.shape[-1] <= 3
                    else arr.reshape(-1, arr.shape[-1])[:1000]
                )
                if reduced.ndim == 2:
                    return p.plot_2d_image_clean(reduced)
        except Exception as exc:
            print(f"[MONITOR] Plot error: {exc}")
            traceback.print_exc()

        return False

    def _update_info_labels(self, arr: np.ndarray):
        dtype_str = f"ndarray ({arr.dtype})" if isinstance(arr, np.ndarray) else type(arr).__name__
        shape_str = str(arr.shape) if hasattr(arr, "shape") else "scalar"
        range_str = (
            f"[{arr.min():.4g}, {arr.max():.4g}]"
            if isinstance(arr, np.ndarray) and arr.size > 0 and np.issubdtype(arr.dtype, np.number)
            else "N/A"
        )
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
    # Per-frame work (main thread)  — back-pressure policy  (Issue 6)
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
        now      = time.time()
        can_draw = (now - self.last_update) >= self.min_update_interval
        # Back-pressure: if we can't render yet, drain at most 1 item to
        # prevent the queue growing, but skip rendering.
        items_to_drain = MAX_QUEUE_ITEMS_PER_FRAME if can_draw else 1

        for _ in range(items_to_drain):
            try:
                item = self.data_queue.get_nowait()
            except Empty:
                break

            if not can_draw:
                continue

            arr = self._raw_to_numpy(item["payload"])
            if arr is None:
                continue

            if self._plot(arr):
                self._update_info_labels(arr)
                self.last_update   = now
                self.update_count += 1
                self._set_status("receiving")

    # =========================================================================
    # Main entry point
    # =========================================================================

    def run(self):
        self._build_ui()
        threading.Thread(target=self._connection_loop, daemon=True).start()

        while dpg.is_dearpygui_running():
            if self._stop_flag.is_set():
                break
            self._apply_pending_status()
            self._update_responsive_layout()
            self._drain_queue()
            dpg.render_dearpygui_frame()

        self._stop_flag.set()
        self._disarm_stall_watchdog()
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
    parser.add_argument(
        "--session-id", default="",
        help="Session ID of the simulation run that spawned this monitor (Issue 9)",
    )
    args = parser.parse_args()

    monitor = StandaloneMonitor(
        server_url         = args.server_url,
        server_url_file    = args.server_url_file,
        server_output_name = args.server_output_name,
        node_name          = args.node_name,
        output_name        = args.output_name,
        session_id         = args.session_id,
    )
    monitor.run()


if __name__ == "__main__":
    main()