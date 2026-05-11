"""
inprocess_monitor.py
====================
In-process monitor window rendered inside the main DPG context.

Unlike the subprocess-based ``StandaloneMonitor`` (monitor_window.py),
``InProcessMonitor`` opens a regular DPG window inside the already-running
editor viewport.  This avoids the overhead of spawning an OS subprocess per
monitored output while keeping the same ``DPGPlotter``-based visualisation.

Data flow (probe-based in-process mode)
----------------------------------------
1. A ``MonitorProbeObj`` (injected into the specula ``LoopControl`` by
   ``InProcessBackend._run_direct``) runs on the simulation thread after
   every step in which the watched output is updated.
2. ``MonitorProbeObj.trigger()`` extracts a CPU float32 numpy array and
   calls ``MonitorBus.push(topic, payload)``.
3. ``MonitorBus`` calls ``InProcessMonitor._on_data(payload)`` on the
   simulation thread; ``_on_data`` enqueues the payload in a thread-safe
   ``Queue``.
4. ``MonitorManager`` calls ``render_frame()`` on every DPG frame (main
   thread) via a recurring frame-callback.  ``render_frame()`` drains the
   queue and updates the ``DPGPlotter``.

Thread safety
-------------
Only ``_on_data`` (enqueue) is called from the simulation thread.
All DPG operations happen exclusively in ``render_frame()`` (main thread).
"""

from __future__ import annotations

import time
import traceback
from queue import Empty, Queue
from typing import TYPE_CHECKING

import dearpygui.dearpygui as dpg
import numpy as np

from constants import MAX_QUEUE_ITEMS_PER_FRAME, MONITOR_QUEUE_SIZE
from dpg_plotting import DPGPlotter

if TYPE_CHECKING:
    from simulation_backend import MonitorProbeObj


class InProcessMonitor:
    """An in-process, DPG-native monitor window.

    Parameters
    ----------
    monitor_id         : Unique identifier string used to build DPG tags.
    node_uuid          : UUID of the source graph node.
    node_name          : Human-readable node name (displayed in the title).
    output_name        : Short output name, e.g. ``"out_slopes"``.
    server_output_name : Fully-qualified topic, e.g. ``"my_wfs.out_slopes"``.
    monitor_bus        : ``MonitorBus`` instance to subscribe to.
    """

    def __init__(
        self,
        monitor_id: str,
        node_uuid: str,
        node_name: str,
        output_name: str,
        server_output_name: str,
        monitor_bus,
    ) -> None:
        self.monitor_id          = monitor_id
        self.node_uuid           = node_uuid
        self.node_name           = node_name
        self.output_name         = output_name
        self.server_output_name  = server_output_name

        self._bus = monitor_bus
        self._data_queue: Queue = Queue(maxsize=MONITOR_QUEUE_SIZE)

        # Reference to the MonitorProbeObj that feeds this monitor.
        # Set by MonitorManager after probe injection; may be None if the
        # probe has not been created yet (simulation not started) or in
        # legacy socket.io mode.
        self._probe: MonitorProbeObj | None = None

        # DPG tag namespace — unique per monitor instance
        self._win_tag      = f"ipm_win_{monitor_id}"
        self._plot_grp_tag = f"ipm_plot_{monitor_id}"
        self._pholder_tag  = f"ipm_ph_{monitor_id}"
        self._output_tag   = f"ipm_output_{monitor_id}"
        self._type_tag     = f"ipm_type_{monitor_id}"
        self._shp_tag      = f"ipm_shp_{monitor_id}"
        self._rng_tag      = f"ipm_rng_{monitor_id}"
        self._time_tag     = f"ipm_time_{monitor_id}"

        self._plotter: DPGPlotter | None = None
        self.is_open        = False
        self.update_count   = 0
        self.last_update    = 0.0
        self.min_update_interval = 0.05

        # Subscribe to the MonitorBus so we receive payloads from any
        # MonitorProbeObj (or, in legacy mode, from the Socket.IO path)
        # that pushes to this topic.
        monitor_bus.subscribe(server_output_name, self._on_data)

    # ------------------------------------------------------------------
    # Bus callback (simulation / socket.io background thread)
    # ------------------------------------------------------------------

    def _on_data(self, raw_data) -> None:
        """Enqueue *raw_data* from the producer thread."""
        if self._data_queue.full():
            try:
                self._data_queue.get_nowait()   # drop oldest
            except Empty:
                pass
        self._data_queue.put(raw_data)

    # ------------------------------------------------------------------
    # DPG window lifecycle (main thread only)
    # ------------------------------------------------------------------

    def focus(self) -> None:
        """Bring the monitor window to the foreground.  Main thread only."""
        if dpg.does_item_exist(self._win_tag):
            dpg.focus_item(self._win_tag)

    def open(self) -> None:
        """Create and show the DPG window.  Must be called on the main thread."""
        if self.is_open and dpg.does_item_exist(self._win_tag):
            dpg.focus_item(self._win_tag)
            return

        title = f"Monitor: {self.node_name}.{self.output_name}"

        with dpg.window(
            label=title,
            tag=self._win_tag,
            width=920,
            height=720,
            on_close=self._on_dpg_close,
        ):
            dpg.add_text(
                f"Output:  {self.server_output_name}",
                color=[100, 255, 100],
                tag=self._output_tag,
            )
            dpg.add_text(
                "Status:  Waiting for data …",
                color=[255, 200, 0],
                tag=f"ipm_status_{self.monitor_id}",
            )
            dpg.add_separator()
            dpg.add_text(
                "Waiting for data …",
                color=[150, 150, 150],
                tag=self._pholder_tag,
            )
            dpg.add_group(tag=self._plot_grp_tag)
            dpg.add_separator()
            dpg.add_text("Type:    —", color=[200, 200, 200], tag=self._type_tag)
            dpg.add_text("Shape:   —", color=[200, 200, 200], tag=self._shp_tag)
            dpg.add_text("Range:   —", color=[200, 200, 200], tag=self._rng_tag)
            dpg.add_text("Updated: never", color=[200, 200, 200], tag=self._time_tag)

        self.is_open = True

    def _on_dpg_close(self) -> None:
        """Called by DPG when the user closes the window."""
        self.is_open = False
        self._bus.unsubscribe(self.server_output_name, self._on_data)

    def close(self) -> None:
        """Programmatically close and clean up the monitor.  Main thread only."""
        self._bus.unsubscribe(self.server_output_name, self._on_data)
        if dpg.does_item_exist(self._win_tag):
            dpg.delete_item(self._win_tag)
        self.is_open = False
        self._probe = None

    def retarget_server_output(self, new_server_output_name: str) -> bool:
        """Rebind this monitor to a different fully-qualified server output.

        Updates the bus subscription and the displayed output label.
        The ``_probe`` reference is cleared here; the caller (MonitorManager)
        is responsible for detaching the old probe and attaching a new one.
        """
        if not new_server_output_name or new_server_output_name == self.server_output_name:
            return False
        old = self.server_output_name
        try:
            self._bus.unsubscribe(old, self._on_data)
        except Exception as exc:
            print(f"[IPMonitor] unsubscribe failed for '{old}': {exc}")
        self.server_output_name = new_server_output_name
        # Clear the probe reference — caller must attach a new probe
        self._probe = None
        try:
            self._bus.subscribe(self.server_output_name, self._on_data)
        except Exception as exc:
            print(f"[IPMonitor] subscribe failed for '{self.server_output_name}': {exc}")
            return False
        if dpg.does_item_exist(self._output_tag):
            dpg.set_value(self._output_tag, f"Output:  {self.server_output_name}")
        self._set_status("subscribed")
        return True

    # ------------------------------------------------------------------
    # Per-frame rendering (main thread only)
    # ------------------------------------------------------------------

    def render_frame(self) -> bool:
        """Drain the queue and update the plot.

        Returns
        -------
        bool
            ``True`` if the window is still open and should continue to be
            ticked, ``False`` if the window has been closed.
        """
        # print(f"[RENDER-DBG] render_frame called, is_open={self.is_open}, win_exists={dpg.does_item_exist(self._win_tag)}, qsize={self._data_queue.qsize()}")  # TEMP
        if not self.is_open or not dpg.does_item_exist(self._win_tag):
            return False

        now = time.time()
        for _ in range(MAX_QUEUE_ITEMS_PER_FRAME):
            try:
                raw_data = self._data_queue.get_nowait()
            except Empty:
                break

            if now - self.last_update < self.min_update_interval:
                continue

            arr = self._raw_to_numpy(raw_data)
            if arr is None:
                continue

            if self._plot(arr):
                self._update_info_labels(arr)
                self.last_update   = now
                self.update_count += 1
                self._set_status("receiving")

        return True

    # ------------------------------------------------------------------
    # Data conversion
    # ------------------------------------------------------------------

    def _raw_to_numpy(self, inner_payload: dict) -> np.ndarray | None:
        """Convert the inner payload dict to a float32 numpy array.

        In probe-based mode the ``data`` field is already a CPU numpy array,
        so conversion is a cheap astype call.  In legacy (socket.io) mode
        ``data`` may be a list, which is handled identically to before.
        """
        data_type  = inner_payload.get("type")
        data_value = inner_payload.get("data")
        shape      = inner_payload.get("shape")

        if data_value is None:
            return None

        try:
            if data_type in ("1d_array", "2d_array", "scalar", "nd_array") or data_type is None:
                if isinstance(data_value, np.ndarray):
                    # Probe-based path — already a CPU array, cheap cast
                    arr = data_value.astype(np.float32, copy=False)
                elif isinstance(data_value, list):
                    arr = np.array(data_value, dtype=np.float32)
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

        except Exception as exc:
            print(f"[IPMonitor] Data conversion error: {exc}")
            traceback.print_exc()

        return None

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _plot(self, arr: np.ndarray) -> bool:
        if dpg.does_item_exist(self._pholder_tag):
            dpg.delete_item(self._pholder_tag)

        if self._plotter is None:
            self._plotter = DPGPlotter(
                parent_tag=self._plot_grp_tag, width=880, height=500
            )

        p    = self._plotter
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
            print(f"[IPMonitor] Plot error: {exc}")
            traceback.print_exc()

        return False

    # ------------------------------------------------------------------
    # Status / info helpers
    # ------------------------------------------------------------------

    _STATUS_COLORS = {
        "receiving":  [0, 200, 255],
        "subscribed": [100, 255, 100],
        "error":      [255, 80, 80],
    }

    def _set_status(self, status: str) -> None:
        tag = f"ipm_status_{self.monitor_id}"
        if dpg.does_item_exist(tag):
            color = self._STATUS_COLORS.get(status, [200, 200, 200])
            dpg.set_value(tag, f"Status:  {status.capitalize()}")
            dpg.configure_item(tag, color=color)

    def _update_info_labels(self, arr: np.ndarray) -> None:
        dtype_str = (
            f"ndarray ({arr.dtype})"
            if isinstance(arr, np.ndarray)
            else type(arr).__name__
        )
        shape_str = str(arr.shape) if hasattr(arr, "shape") else "scalar"
        range_str = (
            f"[{arr.min():.4g}, {arr.max():.4g}]"
            if isinstance(arr, np.ndarray)
            and arr.size > 0
            and np.issubdtype(arr.dtype, np.number)
            else "N/A"
        )
        ts = time.strftime("%H:%M:%S")
        for tag, text in (
            (self._type_tag, f"Type:    {dtype_str}"),
            (self._shp_tag,  f"Shape:   {shape_str}"),
            (self._rng_tag,  f"Range:   {range_str}"),
            (self._time_tag, f"Updated: {ts}  (#{self.update_count})"),
        ):
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, text)