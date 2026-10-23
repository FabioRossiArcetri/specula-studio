"""
Standalone monitor window — runs as an independent subprocess, one per
monitored output of a run with a display server.

* ``--session-id``: every reconnect attempt reads the coordination file and
  compares its ``session_id`` with the one we were launched with.  If they
  differ the file belongs to a newer run and this process exits instead of
  attaching to the new server.
* Back-pressure: when the minimum display interval has not elapsed,
  ``_drain_queue`` drains at most one item and skips rendering; a full queue
  drops its oldest frame and the drops are logged periodically.
* A stall watchdog re-arms the ``newdata`` pull cycle when no ``done`` arrives,
  so a single missed ``done`` cannot stop the stream.
* Envelope: outer ``{"name": ..., "data": <payload>}``; the payload has
  ``type/data/shape`` fields — the same that InProcessMonitor receives from
  MonitorBus.
"""

import argparse
import json
import os
import threading
import time
from queue import Empty, Full, Queue

import numpy as np
import socketio as sio_module
import dearpygui.dearpygui as dpg

from .dpg_plotting import DPGPlotter
from .monitor_data import describe_array, min_update_interval, payload_to_array, plot_array
from .constants import (
    MAX_QUEUE_ITEMS_PER_FRAME,
    MONITOR_QUEUE_SIZE,
    STREAM_STALL_TIMEOUT,
    STREAM_STALL_MAX_TIMEOUT,
    MONITOR_DROP_LOG_INTERVAL,
    MONITOR_UNREACHABLE_ATTEMPTS,
)
from .theme_manager import ThemeManager
import logging

log = logging.getLogger(__name__)


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
    _TAG_VEC_STYLE      = "vector_snapshot_style"
    _TAG_IMG_FILTER     = "image_filtering"
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
        self.session_id         = session_id

        # Thread-safe data queue (sio thread → main thread)
        self.data_queue: Queue = Queue(maxsize=MONITOR_QUEUE_SIZE)

        # Drop counter
        self._drop_count: int = 0

        # Socket.IO state
        self.sio       = None
        self.connected = False
        self._sio_lock = threading.Lock()

        # Stall watchdog
        self._stall_timer: threading.Timer | None = None
        self._stall_lock = threading.Lock()
        self._stall_delay = STREAM_STALL_TIMEOUT

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

        # Theme manager
        self.theme_manager = ThemeManager()

    # =========================================================================
    # Coordination file / URL resolution
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
                    log.info(
                        f"[MONITOR] Coordination file session changed "
                        f"({self.session_id!r} → {file_session!r}). "
                        "Exiting — this monitor belongs to the previous run."
                    )
                    return self.server_url, False

                url = data.get("url", "").strip()
                if url and url != self.server_url:
                    log.info(f"[MONITOR] URL updated from file: {self.server_url} → {url}")
                    self.server_url = url
                    with self._status_lock:
                        self._pending_url = url
                if url:
                    return url, True
            except Exception as e:
                log.warning(f"[MONITOR] Could not read URL file: {e}")
        return self.server_url, True

    # =========================================================================
    # Stall watchdog
    # =========================================================================

    def _arm_stall_watchdog(self, reset: bool = False):
        with self._stall_lock:
            if reset:
                self._stall_delay = STREAM_STALL_TIMEOUT
            if self._stall_timer is not None:
                self._stall_timer.cancel()
            self._stall_timer = threading.Timer(
                self._stall_delay, self._on_stall_detected
            )
            self._stall_timer.daemon = True
            self._stall_timer.start()

    def _disarm_stall_watchdog(self):
        with self._stall_lock:
            if self._stall_timer is not None:
                self._stall_timer.cancel()
                self._stall_timer = None

    def _on_stall_detected(self):
        """No 'done' in time: usually a paused simulation (stepping mode),
        which answers at its next step; a lost 'done' is covered by asking
        again, less and less often so requests do not pile up on the server."""
        if not self.connected:
            return
        log.info(
            f"[MONITOR] No 'done' in {self._stall_delay:.0f}s "
            "(simulation paused?). Re-arming pull cycle."
        )
        self._set_status("waiting")
        with self._sio_lock:
            sio = self.sio
        if sio and self.connected:
            try:
                sio.emit("newdata", [self.server_output_name])
                with self._stall_lock:
                    self._stall_delay = min(self._stall_delay * 2, STREAM_STALL_MAX_TIMEOUT)
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
            log.info(f"[MONITOR] Connected to {self.server_url}")
            self._set_status("connected")
            try:
                client.emit("newdata", [self.server_output_name])
                self._arm_stall_watchdog(reset=True)
                self._set_status("subscribed")
                log.info(f"[MONITOR] Subscribed to {self.server_output_name}")
            except Exception as e:
                log.error(f"[MONITOR] Subscription error: {e}")

        @client.event
        def disconnect():
            self.connected = False
            self._disarm_stall_watchdog()
            log.info("[MONITOR] Disconnected")
            self._set_status("disconnected")

        @client.event
        def connect_error(data):
            self.connected = False
            self._disarm_stall_watchdog()
            msg = str(data)[:120]
            log.error(f"[MONITOR] Connection error: {msg}")
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
                # Drop oldest to make room
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
                    log.info(
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
                    self._arm_stall_watchdog(reset=True)
                except Exception:
                    pass

        @client.event
        def heartbeat(data):
            """Server keepalive — verify we are still talking to the right run."""
            server_sid = data.get("session_id") if isinstance(data, dict) else None
            if server_sid and self.session_id and server_sid != self.session_id:
                log.info(
                    f"[MONITOR] Heartbeat session mismatch "
                    f"(mine={self.session_id!r}, server={server_sid!r}). Exiting."
                )
                self._stop_flag.set()

        return client

    def _connection_loop(self):
        """Background thread: keep trying to connect with exponential back-off."""
        retry_delay = 1.0
        max_delay   = 10.0
        failures    = 0

        while not self._stop_flag.is_set():
            if self.connected:
                time.sleep(2.0)
                continue

            url, session_ok = self._resolve_server_url()
            if not session_ok:
                self._stop_flag.set()
                break

            try:
                log.info(f"[MONITOR] Connecting to {url} …")
                with self._sio_lock:
                    if self.sio:
                        try:
                            self.sio.disconnect()
                        except Exception:
                            pass
                    self.sio = self._build_sio_client()
                self.sio.connect(url, namespaces=["/"])
                retry_delay = 1.0
                failures    = 0
            except Exception as e:
                err = str(e)[:120]
                failures += 1
                log.warning(f"[MONITOR] Connection failed ({err}), retry in {retry_delay:.1f}s")
                self._set_status("unreachable" if failures >= MONITOR_UNREACHABLE_ATTEMPTS
                                 else "retrying")
                self._stop_flag.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 1.5, max_delay)

    # =========================================================================
    # DPG setup
    # =========================================================================

    def _build_ui(self):
        dpg.create_context()

        # Apply global theme colors to the entire context
        tm = self.theme_manager

        # Set global style
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                # Window colors
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, tm.get_color("window_bg"))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, tm.get_color("titlebar"))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, tm.get_color("titlebar_active"))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, tm.get_color("titlebar"))
                dpg.add_theme_color(dpg.mvThemeCol_MenuBarBg, tm.get_color("menubar"))

                # Borders
                dpg.add_theme_color(dpg.mvThemeCol_Border, tm.get_color("border"))
                dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, tm.get_color("border"))

                # Child windows and frames
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, tm.get_color("window_bg"))
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, tm.get_color("window_bg"))

                # Text colors
                dpg.add_theme_color(dpg.mvThemeCol_Text, tm.get_color("text_default"))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, tm.get_color("text_hint"))
                dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, tm.get_color("select"))

                # Headers and collapsing headers
                dpg.add_theme_color(dpg.mvThemeCol_Header, tm.get_color("select"))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, tm.get_color("select_hover"))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, tm.get_color("select_active"))

                # Buttons
                dpg.add_theme_color(dpg.mvThemeCol_Button, tm.get_color("button"))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, tm.get_color("button_hover"))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, tm.get_color("button_active"))

                # Frame backgrounds (for various widgets)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, tm.get_color("frame_bg"))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, tm.get_color("frame_bg_hover"))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, tm.get_color("frame_bg_active"))

                # Scrollbars
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, tm.get_color("window_bg"))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, tm.get_color("border"))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, tm.get_color("select_hover"))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, tm.get_color("select_active"))

                # Sliders and progress bars
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, tm.get_color("select"))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, tm.get_color("select_active"))

                # Checkboxes and radio buttons
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, tm.get_color("select"))

                # Tabs
                dpg.add_theme_color(dpg.mvThemeCol_Tab, tm.get_color("button"))
                dpg.add_theme_color(dpg.mvThemeCol_TabHovered, tm.get_color("button_hover"))
                dpg.add_theme_color(dpg.mvThemeCol_TabActive, tm.get_color("button_active"))
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocused, tm.get_color("frame_bg"))
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocusedActive, tm.get_color("frame_bg_active"))

                # Separators
                dpg.add_theme_color(dpg.mvThemeCol_Separator, tm.get_color("border"))
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorHovered, tm.get_color("select_hover"))
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorActive, tm.get_color("select_active"))

                # Resize grips
                dpg.add_theme_color(dpg.mvThemeCol_ResizeGrip, tm.get_color("border"))
                dpg.add_theme_color(dpg.mvThemeCol_ResizeGripHovered, tm.get_color("select_hover"))
                dpg.add_theme_color(dpg.mvThemeCol_ResizeGripActive, tm.get_color("select_active"))

                # Drag and drop
                dpg.add_theme_color(dpg.mvThemeCol_DragDropTarget, tm.get_color("select"))

                # Navigation highlight
                dpg.add_theme_color(dpg.mvThemeCol_NavHighlight, tm.get_color("select"))
                dpg.add_theme_color(dpg.mvThemeCol_NavWindowingHighlight, tm.get_color("select"))
                dpg.add_theme_color(dpg.mvThemeCol_NavWindowingDimBg, tm.get_color("window_bg"))

                # Modal window dimming
                dpg.add_theme_color(dpg.mvThemeCol_ModalWindowDimBg, tm.get_color("window_bg"))

        dpg.bind_theme(global_theme)

        if _FONT_PATH and os.path.exists(_FONT_PATH):
            with dpg.font_registry():
                dpg.bind_font(dpg.add_font(_FONT_PATH, FONT_SIZE))

        title = f"Monitor: {self.node_name}.{self.output_name}"
        tm = self.theme_manager

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
                with dpg.group(horizontal=True):
                    dpg.add_text("Snapshot as:")
                    dpg.add_radio_button(
                        items=["Line", "Histogram"],
                        default_value="Line",
                        horizontal=True,
                        tag=self._TAG_VEC_STYLE,
                        callback=lambda s, a: (
                            self.dpg_plotter.set_vector_style(a.lower())
                            if self.dpg_plotter else None
                        ),
                    )
                dpg.add_checkbox(
                    label="Image filtering",
                    default_value=False,
                    tag=self._TAG_IMG_FILTER,
                    callback=lambda s, a: (
                        self.dpg_plotter.set_image_filtering(bool(a))
                        if self.dpg_plotter else None
                    ),
                )

            dpg.add_separator()

            with dpg.collapsing_header(label="Connection", default_open=False):
                dpg.add_text(
                    f"Server:  {self.server_url}",
                    color=tm.get_color("text_hint"),
                    tag=self._TAG_URL_TXT,
                )
                dpg.add_text(
                    f"Output:  {self.server_output_name}",
                    color=tm.get_color("monitor_output_label"),
                )
                dpg.add_text(
                    "Status:  Connecting …",
                    color=tm.get_color("monitor_status_waiting"),
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
                    color=tm.get_color("text_hint"),
                    tag=self._TAG_PHOLDER,
                )

            dpg.add_separator()

            with dpg.group(horizontal=False):
                dpg.add_text(
                    "Type:    —",
                    color=tm.get_color("text_secondary"),
                    tag=self._TAG_INFO_TYPE,
                )
                dpg.add_text(
                    "Shape:   —",
                    color=tm.get_color("text_secondary"),
                    tag=self._TAG_INFO_SHP,
                )
                dpg.add_text(
                    "Range:   —",
                    color=tm.get_color("text_secondary"),
                    tag=self._TAG_INFO_RNG,
                )
                dpg.add_text(
                    "Updated: never",
                    color=tm.get_color("text_secondary"),
                    tag=self._TAG_INFO_TIME,
                )

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
    # Data conversion + plotting
    # =========================================================================

    def _raw_to_numpy(self, payload: dict) -> np.ndarray | None:
        return payload_to_array(payload)

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
            self.dpg_plotter.set_vector_style(dpg.get_value(self._TAG_VEC_STYLE).lower())
            self.dpg_plotter.set_image_filtering(bool(dpg.get_value(self._TAG_IMG_FILTER)))

        self.min_update_interval = min_update_interval(arr)
        try:
            return plot_array(self.dpg_plotter, arr)
        except Exception:
            log.exception("Monitor plot error")
            return False

    def _update_info_labels(self, arr: np.ndarray):
        dtype_str, shape_str, range_str = describe_array(arr)
        ts = time.strftime("%H:%M:%S")
        tm = self.theme_manager
        for tag, text in (
            (self._TAG_INFO_TYPE, f"Type:    {dtype_str}"),
            (self._TAG_INFO_SHP,  f"Shape:   {shape_str}"),
            (self._TAG_INFO_RNG,  f"Range:   {range_str}"),
            (self._TAG_INFO_TIME, f"Updated: {ts}  (#{self.update_count})"),
        ):
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, text)
                dpg.configure_item(tag, color=tm.get_color("text_secondary"))

    # =========================================================================
    # Per-frame work (main thread) — back-pressure policy
    # =========================================================================

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
            tm = self.theme_manager
            # Map status to color keys
            color_keys = {
                "connected":    "monitor_status_subscribed",   # actually green
                "subscribed":   "monitor_status_subscribed",
                "receiving":    "monitor_status_receiving",
                "disconnected": "monitor_status_error",
                "error":        "monitor_status_error",
                "retrying":     "monitor_status_waiting",
                "waiting":      "monitor_status_waiting",
                "unreachable":  "monitor_status_error",
            }
            label_map = {
                "connected":    "+ Connected",
                "subscribed":   "> Subscribed",
                "receiving":    "<> Receiving",
                "disconnected": "- Disconnected",
                "error":        "! Error",
                "retrying":     "~ Retrying …",
                "waiting":      "~ Waiting for the simulation (paused?) …",
                "unreachable":  "! Display server not reachable (stopped?), retrying …",
            }
            label = label_map.get(status, status.capitalize())
            color = tm.get_color(color_keys.get(status, "text_secondary"))
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
        help="Session ID of the simulation run that spawned this monitor",
    )
    args = parser.parse_args()

    from .logs import setup_logging
    setup_logging(os.environ.get("SPECULA_STUDIO_LOG_LEVEL", "INFO"))

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
