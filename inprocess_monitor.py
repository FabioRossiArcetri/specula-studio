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
Changes vs. previous version
-----------------------------
* Drop counter: ``_on_data`` raises ``_DropFrame`` (caught by MonitorBus.push)
  instead of silently discarding the oldest item locally, so the bus can
  aggregate drop counts across all monitors and log them periodically (Issue 6).
* ``render_frame`` throttles rendering but no longer wastes CPU dequeuing-then-
  discarding items faster than the display rate: if the minimum update interval
  has not elapsed, it drains at most one additional item from the queue and
  returns immediately (Issue 6).
* ``_on_data`` uses a non-blocking ``put`` with the ``_DropFrame`` signal so
  the simulation thread is never blocked on a full queue (Issue 6).
"""

from __future__ import annotations

import time
import traceback
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING

import dearpygui.dearpygui as dpg
import numpy as np

from constants import MAX_QUEUE_ITEMS_PER_FRAME, MONITOR_QUEUE_SIZE
from dpg_plotting import DPGPlotter
from monitor_bus import _DropFrame
from theme_manager import ThemeManager

if TYPE_CHECKING:
    from simulation_backend import MonitorProbeObj


class InProcessMonitor:
    """An in-process, DPG-native monitor window."""

    def __init__(
        self,
        monitor_id: str,
        node_uuid: str,
        node_name: str,
        output_name: str,
        server_output_name: str,
        monitor_bus,
        is_direct_mode: bool = False,
    ) -> None:
        self.monitor_id         = monitor_id
        self.node_uuid          = node_uuid
        self.node_name          = node_name
        self.output_name        = output_name
        self.server_output_name = server_output_name
        self.is_direct_mode     = is_direct_mode
        
        # FIX: Store the canonical topic separately for direct mode.
        # In direct mode, this is the ONLY topic the monitor subscribes to,
        # and it should NEVER change (probes are attached here, not elsewhere).
        self._canonical_topic = server_output_name if is_direct_mode else None

        self._bus        = monitor_bus
        self._data_queue: Queue = Queue(maxsize=MONITOR_QUEUE_SIZE)

        self._probe: MonitorProbeObj | None = None

        self._win_tag      = f"ipm_win_{monitor_id}"
        self._plot_grp_tag = f"ipm_plot_{monitor_id}"
        self._pholder_tag  = f"ipm_ph_{monitor_id}"
        self._output_tag   = f"ipm_output_{monitor_id}"
        self._type_tag     = f"ipm_type_{monitor_id}"
        self._shp_tag      = f"ipm_shp_{monitor_id}"
        self._rng_tag      = f"ipm_rng_{monitor_id}"
        self._time_tag     = f"ipm_time_{monitor_id}"

        self._plotter: DPGPlotter | None = None
        self.is_open             = False
        self.update_count        = 0
        self.last_update         = 0.0
        self.min_update_interval = 0.05

        # Subscribe to the monitor bus with this monitor's callback
        monitor_bus.subscribe(server_output_name, self._on_data)

    # ── Bus callback ────────────────────────────────────────────────────────────

    def _on_data(self, raw_data) -> None:
        """Enqueue *raw_data* from the producer thread.

        Raises ``_DropFrame`` if the queue is full instead of blocking or silently
        discarding, so the bus can track drop counts centrally.
        """
        try:
            self._data_queue.put_nowait(raw_data)
        except Full:
            raise _DropFrame()

    # ── DPG window lifecycle ───────────────────────────────────────────────────

    def focus(self) -> None:
        if dpg.does_item_exist(self._win_tag):
            dpg.focus_item(self._win_tag)

    def open(self) -> None:
        if self.is_open and dpg.does_item_exist(self._win_tag):
            dpg.focus_item(self._win_tag)
            return

        title = f"Monitor: {self.node_name}.{self.output_name}"
        tm = ThemeManager()

        with dpg.window(
            label=title,
            tag=self._win_tag,
            width=920,
            height=720,
            on_close=self._on_dpg_close,
        ):
            dpg.add_text(
                f"Output:  {self.server_output_name}",
                color=tm.get_color("monitor_output_label"),
                tag=self._output_tag,
            )
            dpg.add_text(
                "Status:  Waiting for data …",
                color=tm.get_color("monitor_status_waiting"),
                tag=f"ipm_status_{self.monitor_id}",
            )
            dpg.add_separator()
            dpg.add_text(
                "Waiting for data …",
                color=tm.get_color("text_hint"),
                tag=self._pholder_tag,
            )
            dpg.add_group(tag=self._plot_grp_tag)
            dpg.add_separator()
            dpg.add_text(
                "Type:    —",
                color=tm.get_color("text_secondary"),
                tag=self._type_tag,
            )
            dpg.add_text(
                "Shape:   —",
                color=tm.get_color("text_secondary"),
                tag=self._shp_tag,
            )
            dpg.add_text(
                "Range:   —",
                color=tm.get_color("text_secondary"),
                tag=self._rng_tag,
            )
            dpg.add_text(
                "Updated: never",
                color=tm.get_color("text_secondary"),
                tag=self._time_tag,
            )

        self.is_open = True

    def _on_dpg_close(self) -> None:
        self.is_open = False
        self._bus.unsubscribe(self.server_output_name, self._on_data)

    def close(self) -> None:
        self._bus.unsubscribe(self.server_output_name, self._on_data)
        if dpg.does_item_exist(self._win_tag):
            dpg.delete_item(self._win_tag)
        self.is_open  = False
        self._probe   = None

    def retarget_server_output(self, new_server_output_name: str) -> bool:
        """
        Retarget this monitor to a different server output.
        
        FIX: In direct mode, this is a NO-OP. Direct monitors are pinned to their
        canonical topic (where probes are injected) and cannot be retargeted by
        Socket.IO rebinding logic.
        """
        # In direct mode, NEVER retarget. The canonical topic is authoritative.
        #if self.is_direct_mode:
        #    return False
        
        if not new_server_output_name or new_server_output_name == self.server_output_name:
            return False
        old = self.server_output_name
        try:
            self._bus.unsubscribe(old, self._on_data)
        except Exception as exc:
            print(f"[IPMonitor] unsubscribe failed for '{old}': {exc}")
        self.server_output_name = new_server_output_name
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

    # ── Per-frame rendering ────────────────────────────────────────────────────

    def render_frame(self) -> bool:
        """Drain the queue and update the plot.

        Back-pressure policy: if the minimum update interval has not elapsed,
        drain at most one extra item (to keep the queue from growing unbounded)
        then return — no point in dequeuing dozens of frames we won't render.
        """
        if not self.is_open or not dpg.does_item_exist(self._win_tag):
            return False

        now      = time.time()
        can_draw = (now - self.last_update) >= self.min_update_interval

        items_to_drain = MAX_QUEUE_ITEMS_PER_FRAME if can_draw else 1

        for _ in range(items_to_drain):
            try:
                raw_data = self._data_queue.get_nowait()
            except Empty:
                break

            if not can_draw:
                # queue is backing up — absorb the item but don't render
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

    # ── Data conversion ────────────────────────────────────────────────────────

    def _raw_to_numpy(self, inner_payload: dict) -> np.ndarray | None:
        data_type  = inner_payload.get("type")
        data_value = inner_payload.get("data")
        shape      = inner_payload.get("shape")

        if data_value is None:
            return None

        try:
            if data_type in ("1d_array", "2d_array", "scalar", "nd_array") or data_type is None:
                if isinstance(data_value, np.ndarray):
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

    # ── Plotting ───────────────────────────────────────────────────────────────

    def _plot(self, arr: np.ndarray) -> bool:
        if dpg.does_item_exist(self._pholder_tag):
            dpg.delete_item(self._pholder_tag)

        if self._plotter is None:
            self._plotter = DPGPlotter(parent_tag=self._plot_grp_tag, width=880, height=500)

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

    # ── Status / info helpers ──────────────────────────────────────────────────

    _STATUS_COLORS = {
        "receiving":  "monitor_status_receiving",
        "subscribed": "monitor_status_subscribed",
        "error":      "monitor_status_error",
    }

    def _set_status(self, status: str) -> None:
        tag = f"ipm_status_{self.monitor_id}"
        if dpg.does_item_exist(tag):
            tm = ThemeManager()
            color = tm.get_color(self._STATUS_COLORS.get(status, "text_secondary"))
            dpg.set_value(tag, f"Status:  {status.capitalize()}")
            dpg.configure_item(tag, color=color)

    def _update_info_labels(self, arr: np.ndarray) -> None:
        tm = ThemeManager()
        dtype_str = (
            f"ndarray ({arr.dtype})"
            if isinstance(arr, np.ndarray)
            else type(arr).__name__
        )
        shape_str = str(arr.shape) if hasattr(arr, "shape") else "scalar"
        range_str = (
            f"[{arr.min():.4g}, {arr.max():.4g}]"
            if isinstance(arr, np.ndarray) and arr.size > 0
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
                dpg.configure_item(tag, color=tm.get_color("text_secondary"))