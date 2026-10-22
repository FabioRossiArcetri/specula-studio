"""
inprocess_monitor.py
====================
Monitor window inside the editor, used for in-process runs.

Data flow: a ``MonitorProbeObj`` in SPECULA's loop (simulation thread) pushes
payloads to the ``MonitorBus``; :meth:`InProcessMonitor._on_data` only
enqueues them; :meth:`render_frame` (GUI thread, every frame) drains the
queue and draws with a ``DPGPlotter``.  A full queue drops frames instead of
blocking the simulation.
"""

from __future__ import annotations

import time
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING

import dearpygui.dearpygui as dpg
import numpy as np

from .constants import MAX_QUEUE_ITEMS_PER_FRAME, MONITOR_QUEUE_SIZE
from .dpg_plotting import DPGPlotter
from .monitor_data import describe_array, min_update_interval, payload_to_array, plot_array
from .monitor_bus import _DropFrame
from .theme_manager import ThemeManager
import logging

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from .simulation_backend import MonitorProbeObj


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
    ) -> None:
        self.monitor_id         = monitor_id
        self.node_uuid          = node_uuid
        self.node_name          = node_name
        self.output_name        = output_name
        self.server_output_name = server_output_name
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
        self._vector_mode = "snapshot"
        self._vector_style = "line"
        self._image_filtering = False
        self.is_open            = False
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
            raise _DropFrame() from None

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
            with dpg.group(horizontal=True):
                dpg.add_combo(
                    ["Vector: Snapshot", "Vector: Time History"],
                    default_value="Vector: Snapshot", width=220,
                    callback=self._on_vector_mode,
                )
                dpg.add_combo(
                    ["Snapshot: Line", "Snapshot: Histogram"],
                    default_value="Snapshot: Line", width=200,
                    callback=self._on_vector_style,
                )
                dpg.add_checkbox(
                    label="Image filtering", default_value=self._image_filtering,
                    callback=self._on_image_filtering,
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

    # ── Plotting ───────────────────────────────────────────────────────────────

    def _raw_to_numpy(self, payload: dict) -> np.ndarray | None:
        return payload_to_array(payload)

    def _plot(self, arr: np.ndarray) -> bool:
        if dpg.does_item_exist(self._pholder_tag):
            dpg.delete_item(self._pholder_tag)
        if self._plotter is None:
            self._plotter = DPGPlotter(parent_tag=self._plot_grp_tag, width=880, height=500)
            self._plotter.set_vector_mode(self._vector_mode)
            self._plotter.set_vector_style(self._vector_style)
            self._plotter.set_image_filtering(self._image_filtering)
        self.min_update_interval = min_update_interval(arr)
        try:
            return plot_array(self._plotter, arr)
        except Exception:
            log.exception("Monitor plot error")
            return False

    def _on_vector_mode(self, sender, app_data):
        self._vector_mode = "history" if "Time" in app_data else "snapshot"
        if self._plotter is not None:
            self._plotter.set_vector_mode(self._vector_mode)

    def _on_vector_style(self, sender, app_data):
        self._vector_style = "histogram" if "Histogram" in app_data else "line"
        if self._plotter is not None:
            self._plotter.set_vector_style(self._vector_style)

    def _on_image_filtering(self, sender, app_data):
        self._image_filtering = bool(app_data)
        if self._plotter is not None:
            self._plotter.set_image_filtering(self._image_filtering)

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
        dtype_str, shape_str, range_str = describe_array(arr)
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
