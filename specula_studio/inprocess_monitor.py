"""
inprocess_monitor.py
====================
Monitors of the runs inside the editor (Local Run, or no run yet), shown in
their own OS windows like the monitors of Remote runs.

Data flow: a ``MonitorProbeObj`` in SPECULA's loop (simulation thread) pushes
payloads to the ``MonitorBus``; :meth:`InProcessMonitor._on_data` keeps only
the newest; a sender thread passes it, at most every ``min_update_interval``,
over a :mod:`.control_link` to the monitor window process (``python -m
specula_studio.monitor_window --link host:port``), which draws it with the
same plots and settings as the monitors of Remote runs.  Older frames are
dropped, never queued: neither the simulation nor the editor waits for a
window.  Closing the window closes the monitor.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from .control_link import ControlServer, WindowProcess
from .monitor_data import min_update_interval

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .simulation_backend import MonitorProbeObj


class InProcessMonitor:
    """The editor side of a Local Run monitor window."""

    # False in tests: the link is made, the window process is not started
    launch_windows = True

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
        self._bus = monitor_bus

        self._probe: MonitorProbeObj | None = None
        self._link: ControlServer | None = None
        self._window: WindowProcess | None = None

        self._lock = threading.Lock()
        self._latest = None                  # newest payload not sent yet
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._sender: threading.Thread | None = None

        self.is_open      = False
        self.update_count = 0                # payloads received from the probe
        self.sent_count   = 0                # payloads sent to the window
        self.min_update_interval = 0.05

        monitor_bus.subscribe(server_output_name, self._on_data)

    # ── bus callback (simulation thread) ──────────────────────────────────────

    def _on_data(self, payload) -> None:
        """Keep the newest payload; the sender thread passes it on."""
        with self._lock:
            self._latest = payload
            self.update_count += 1
        self._wake.set()

    # ── window lifecycle (GUI thread) ─────────────────────────────────────────

    def open(self, slot: int = 0) -> None:
        """Start the monitor window (*slot*: its place among the open ones)."""
        if self.is_open:
            self.focus()
            return
        self._link = ControlServer()
        self._window = WindowProcess(
            self._link, "monitor", f"Monitor: {self.node_name}.{self.output_name}",
            module="specula_studio.monitor_window",
            argv=["--node-name", self.node_name, "--output-name", self.output_name,
                  "--server-output-name", self.server_output_name, "--slot", str(slot)])
        if self.launch_windows:
            try:
                self._window.launch()
            except Exception:
                log.exception("Cannot start the monitor window of %s", self.server_output_name)
                self._link.close()
                return
        self._stop.clear()
        self._sender = threading.Thread(target=self._send_loop, daemon=True,
                                        name=f"monitor-{self.server_output_name}")
        self._sender.start()
        self.is_open = True

    def focus(self) -> None:
        if self._link is not None:
            self._link.send({"type": "raise"})

    def close(self) -> None:
        self._bus.unsubscribe(self.server_output_name, self._on_data)
        self._stop.set()
        self._wake.set()
        if self._window is not None:
            self._window.stop()
        if self._link is not None:
            self._link.close()
        self.is_open = False
        self._probe = None

    def render_frame(self) -> bool:
        """Every frame: False once the window has been closed (by the user)."""
        if not self.is_open:
            return False
        if self._window is not None and self._window.process is not None:
            return self._window.running
        return True

    # ── sending (own thread) ──────────────────────────────────────────────────

    def _send_loop(self) -> None:
        last_sent = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            if self._stop.is_set():
                return
            wait = self.min_update_interval - (time.time() - last_sent)
            if wait > 0:                      # not faster than the window draws
                self._stop.wait(wait)
            with self._lock:
                payload, self._latest = self._latest, None
            if payload is None:
                continue
            link = self._link
            if link is None or not link.connected:
                # the window is starting: keep the frame (unless a newer came)
                with self._lock:
                    if self._latest is None:
                        self._latest = payload
                self._stop.wait(0.1)
                self._wake.set()
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            if data is not None and hasattr(data, "shape"):
                self.min_update_interval = min_update_interval(data)
            if link.send({"type": "data", "payload": payload}):
                self.sent_count += 1
                last_sent = time.time()
