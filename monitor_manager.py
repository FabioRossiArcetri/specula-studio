"""
monitor_manager.py
==================
Manages the lifecycle of monitor windows — both standalone subprocess windows
and in-process DPG windows.

Subprocess monitors (original behaviour)
    Each monitor window is a separate OS process (monitor_window.py) with its
    own DearPyGui viewport and its own Socket.IO connection.

In-process monitors (new)
    When a ``MonitorBus`` is provided AND ``set_inprocess_mode(True)`` has been
    called, ``open_monitor()`` creates an ``InProcessMonitor`` (a DPG window
    inside the main editor viewport) that receives data via the bus instead of
    a dedicated Socket.IO connection.
    In-process monitors are updated on the DPG main thread through a recurring
    frame callback set up by ``start_periodic_tasks()``.

Key additions over the first subprocess-based version:
  - The display-server URL / port is discovered at simulation start time by
    scanning specula's stdout.  Monitors opened before the port is known are
    queued as *pending* and launched as soon as the URL arrives.
  - A background reaper thread harvests subprocesses that have exited.
  - Optional ``MonitorBus`` enables in-process (DPG-native) monitor windows.
"""

import os
import subprocess
import sys
import threading
import time
import traceback

import dearpygui.dearpygui as dpg

from inprocess_monitor import InProcessMonitor

# Prefix used by SocketIOClient.get_server_output_name() fallback paths
# before server params/mapping are available (e.g. "auto_<node>.out_x").
_SYNTHETIC_SERVER_OUTPUT_PREFIX = "auto_"


class MonitorManager:
    """Manages monitor windows (subprocess and in-process)."""

    def __init__(self, sio_client, graph, monitor_bus=None, debug: bool = True):
        """
        Parameters
        ----------
        sio_client  : SocketIOClient
        graph       : GraphManager
        monitor_bus : MonitorBus or None
            When provided and ``set_inprocess_mode(True)`` is called,
            ``open_monitor()`` creates in-process DPG monitor windows that
            subscribe to the bus instead of spawning subprocesses.
            Supply ``None`` (default) to keep the original subprocess behaviour.
        debug       : bool
        """
        self.sio_client = sio_client
        self.graph = graph
        self.debug = debug
        self._monitor_bus = monitor_bus

        # FIX 1: Separate flag controls whether in-process mode is active.
        # Having a MonitorBus does NOT automatically activate in-process mode;
        # start_sim must explicitly call set_inprocess_mode(True/False).
        self._use_inprocess: bool = False

        # subprocess monitors: monitor_id -> info dict
        self.active_monitors: dict = {}
        self._lock = threading.Lock()

        # in-process monitors: monitor_id -> InProcessMonitor
        self._inprocess_monitors: dict[str, InProcessMonitor] = {}

        # Monitors requested before the display-server URL was known
        self._pending_monitors: list = []   # list of (sender, app_data, user_data)
        self._server_url: str | None = None  # set by on_display_server_ready()

        # Path to the coordination file written by SimulationControl
        self._server_url_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "specula_studio_server.json",
        )

        # Background reaper thread (for subprocess monitors)
        self._reaper_stop = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True
        )
        self._reaper_thread.start()

    # =========================================================================
    # Mode control
    # =========================================================================

    def set_inprocess_mode(self, enabled: bool) -> None:
        """
        Switch between in-process (DPG-native) and subprocess monitor mode.

        Must be called by SimulationControl *before* any monitor is opened for
        a new simulation run:
          - ``True``  → open_monitor() creates InProcessMonitor windows
          - ``False`` → open_monitor() spawns monitor_window.py subprocesses
        """
        self._use_inprocess = enabled
        self._log(
            f"Monitor mode set to: {'in-process' if enabled else 'subprocess (display-server)'}"
        )

    # =========================================================================
    # Logging
    # =========================================================================

    def _log(self, msg: str):
        if self.debug:
            print(f"[MONITOR_MGR] {msg}")

    # =========================================================================
    # Server-event stubs (API compatibility with NodeManager callers)
    # =========================================================================

    def on_server_connect(self):
        pass

    def on_server_disconnect(self):
        pass

    def on_server_connect_error(self, data):
        pass

    def on_data_update(self, name: str, raw_data):
        pass

    def on_server_params(self, data: dict):
        """
        Called when SocketIOClient receives server params and updates mapping.

        In in-process mode, this is used to:
        1) flush deferred monitor opens that waited for stable output mapping;
        2) retarget already-open monitors if their output names changed after
           mapping resolution.
        """
        if not self._use_inprocess:
            return
        self._flush_pending_monitors()
        self._refresh_inprocess_monitor_bindings()

    def _safe_update_monitor_status(self, monitor_id: str, status: str):
        pass

    # =========================================================================
    # Public query helpers
    # =========================================================================

    def is_monitor_open(self, node_uuid: str, output_name: str) -> bool:
        """Return True if any monitor (subprocess or in-process) is open for
        the given node/output combination."""
        with self._lock:
            for info in self.active_monitors.values():
                if (
                    info.get("node_uuid") == node_uuid
                    and info.get("output_name") == output_name
                    and info["process"].poll() is None
                ):
                    return True
        for monitor in self._inprocess_monitors.values():
            if (
                monitor.node_uuid == node_uuid
                and monitor.output_name == output_name
                and monitor.is_open
            ):
                return True
        return False

    def find_monitor_id(self, node_uuid: str, output_name: str) -> str | None:
        """Return the monitor_id of the first open monitor for node/output, or None."""
        with self._lock:
            for mid, info in self.active_monitors.items():
                if (
                    info.get("node_uuid") == node_uuid
                    and info.get("output_name") == output_name
                    and info["process"].poll() is None
                ):
                    return mid
        for mid, monitor in self._inprocess_monitors.items():
            if (
                monitor.node_uuid == node_uuid
                and monitor.output_name == output_name
                and monitor.is_open
            ):
                return mid
        return None

    # =========================================================================
    # Display-server URL notification
    # =========================================================================

    def on_display_server_ready(self, url: str):
        """
        Called by SimulationControl when the display-server port is detected.

        1. Updates the stored URL so new monitors use it immediately.
        2. Restarts monitors that were spawned with a wrong URL (port 5000
           fallback) and are still running (they'll have failed to connect).
        3. Flushes pending monitors that were queued before the URL was known.
        """
        self._log(f"Display server ready at {url}")
        old_url = self._server_url
        self._server_url = url

        # --- Restart already-running monitors that have the wrong URL ----------
        if old_url != url:
            with self._lock:
                stale = [
                    (mid, info)
                    for mid, info in self.active_monitors.items()
                    if info.get("server_url") != url and info["process"].poll() is None
                ]
            for mid, info in stale:
                self._log(
                    f"Restarting monitor {mid} with new URL {url} "
                    f"(was {info.get('server_url')})"
                )
                # Terminate the stale process
                try:
                    info["process"].terminate()
                except Exception:
                    pass
                # Re-open with the same node/output
                self.open_monitor(
                    None, None,
                    (info["node_uuid"], info["output_name"]),
                )
                # Remove old entry (open_monitor will add a new one)
                with self._lock:
                    self.active_monitors.pop(mid, None)

        # --- Flush pending monitors -------------------------------------------
        self._flush_pending_monitors()

    def _flush_pending_monitors(self) -> None:
        pending = list(self._pending_monitors)
        self._pending_monitors.clear()
        for (s, a, ud) in pending:
            self._log(f"Flushing pending monitor for {ud}")
            self.open_monitor(s, a, ud)

    # =========================================================================
    # Open / close
    # =========================================================================

    def open_monitor(self, sender, app_data, user_data):
        """
        Open a monitor window for a node output.
        ``user_data`` must be ``(node_uuid, output_name)``.

        FIX 1: Routes to in-process only when ``_use_inprocess`` is True
        (set via ``set_inprocess_mode()``), not merely because a MonitorBus
        exists.  This ensures Display-Server mode always spawns subprocesses.

        If the display-server URL is not yet known (and a subprocess monitor is
        required), the request is queued and fulfilled as soon as
        ``on_display_server_ready`` is called.
        """
        node_uuid, output_name = user_data

        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            self._log(f"open_monitor: node {node_uuid} not found")
            return

        node_name = node_data.get("name", "Unknown")

        # Resolve the fully-qualified server output name
        try:
            server_output_name = self.sio_client.get_server_output_name(
                node_uuid, output_name, self.graph.nodes
            )
        except Exception as e:
            self._log(f"Could not resolve server output name: {e}")
            server_output_name = f"{node_name}.{output_name}"

        # ��─ In-process path ──────────────────────────────────────────────────
        # FIX 1: Only use in-process path when explicitly enabled AND bus is ready.
        if self._use_inprocess and self._monitor_bus is not None:
            # If mapping is not ready yet, defer opening so we avoid subscribing
            # to fallback synthetic topics (e.g. auto_<node>.out_x).
            if (
                server_output_name.startswith(_SYNTHETIC_SERVER_OUTPUT_PREFIX)
                # Empty dict means params/mapping not populated yet.
                and not self.sio_client.server_nodes
            ):
                self._log(
                    f"Server mapping not ready; queueing in-process monitor for "
                    f"{node_name}.{output_name}"
                )
                self._pending_monitors.append((sender, app_data, user_data))
                return
            self._open_inprocess_monitor(
                node_uuid, node_name, output_name, server_output_name
            )
            return

        # ── Subprocess path (original behaviour) ─────────────────────────────
        node_type = node_data.get("type", "Unknown")

        if self._server_url:
            server_url = self._server_url
        elif self.sio_client.connected:
            server_url = self.sio_client.server_url
        else:
            self._log(
                f"Display-server URL not yet known; queuing monitor for "
                f"{node_name}.{output_name}"
            )
            self._pending_monitors.append((sender, app_data, user_data))
            return

        # Prevent duplicate subprocess windows for the same output
        with self._lock:
            for info in list(self.active_monitors.values()):
                if (
                    info["node_uuid"]   == node_uuid
                    and info["output_name"] == output_name
                    and info["process"].poll() is None
                ):
                    self._log(
                        f"Monitor already open for {node_name}.{output_name} "
                        f"(pid {info['process'].pid})"
                    )
                    return

        # Build subprocess command
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "monitor_window.py"
        )
        cmd = [
            sys.executable, script,
            "--server-output-name",  server_output_name,
            "--node-name",           node_name,
            "--output-name",         output_name,
            "--server-url-file",     self._server_url_file,
            "--server-url",          server_url,
        ]

        self._log(
            f"Spawning monitor: {node_name}.{output_name} → "
            f"{server_output_name} @ {server_url}"
        )

        try:
            process = subprocess.Popen(cmd, cwd=os.path.dirname(script))
        except Exception as e:
            self._log(f"Failed to spawn monitor process: {e}")
            traceback.print_exc()
            return

        monitor_id = f"{node_uuid}_{output_name}_{int(time.time() * 1000)}"
        with self._lock:
            self.active_monitors[monitor_id] = {
                "process":            process,
                "node_uuid":          node_uuid,
                "output_name":        output_name,
                "server_output_name": server_output_name,
                "node_name":          node_name,
                "node_type":          node_type,
                "server_url":         server_url,
                "started_at":         time.time(),
            }

        self._log(
            f"Monitor {monitor_id} started (pid {process.pid}) for "
            f"{server_output_name}"
        )

    # =========================================================================
    # In-process monitor helpers
    # =========================================================================

    def _open_inprocess_monitor(
        self,
        node_uuid: str,
        node_name: str,
        output_name: str,
        server_output_name: str,
    ) -> None:
        """Create an in-process DPG monitor window subscribed to the bus."""
        # Prevent duplicates
        for monitor in self._inprocess_monitors.values():
            if (
                monitor.node_uuid == node_uuid
                and monitor.output_name == output_name
                and monitor.is_open
            ):
                self._log(f"In-process monitor already open for {node_name}.{output_name}")
                monitor.focus()
                return

        monitor_id = f"{node_uuid}_{output_name}_{int(time.time() * 1000)}"
        monitor = InProcessMonitor(
            monitor_id=monitor_id,
            node_uuid=node_uuid,
            node_name=node_name,
            output_name=output_name,
            server_output_name=server_output_name,
            monitor_bus=self._monitor_bus,
        )
        monitor.open()
        self._inprocess_monitors[monitor_id] = monitor

        # FIX 2: Subscribe to the Socket.IO server so specula's DisplayServer
        # actually sends data_update events for this output.  Without this call,
        # MonitorBus.push() is never triggered and the monitor stays blank.
        try:
            self.sio_client.subscribe(server_output_name)
            self._log(
                f"Subscribed Socket.IO client to '{server_output_name}' "
                f"for in-process monitor {monitor_id}"
            )
        except Exception as e:
            self._log(
                f"Warning: could not subscribe to '{server_output_name}': {e}"
            )

        self._log(f"In-process monitor {monitor_id} opened for {server_output_name}")

    def close_monitor(self, monitor_id: str, from_window_close: bool = False):
        """Terminate a monitor — subprocess or in-process."""
        # Try subprocess monitors first
        with self._lock:
            info = self.active_monitors.pop(monitor_id, None)
        if info is not None:
            proc = info["process"]
            if proc.poll() is None:
                self._log(f"Terminating monitor {monitor_id} (pid {proc.pid})")
                try:
                    proc.terminate()
                    threading.Thread(
                        target=self._force_kill_after, args=(proc, 3.0), daemon=True
                    ).start()
                except Exception as e:
                    self._log(f"Error terminating: {e}")
            return

        # Try in-process monitors
        monitor = self._inprocess_monitors.pop(monitor_id, None)
        if monitor is not None:
            self._log(f"Closing in-process monitor {monitor_id}")

            # FIX 2: Unsubscribe from the Socket.IO server when the last
            # in-process monitor watching this output is closed.
            still_watching = any(
                m.server_output_name == monitor.server_output_name
                for m in self._inprocess_monitors.values()
            )
            if not still_watching:
                try:
                    self.sio_client.unsubscribe(monitor.server_output_name)
                    self._log(
                        f"Unsubscribed Socket.IO client from "
                        f"'{monitor.server_output_name}'"
                    )
                except Exception as e:
                    self._log(f"Warning: could not unsubscribe: {e}")

            monitor.close()

    def _refresh_inprocess_monitor_bindings(self) -> None:
        """Retarget in-process monitors after UUID->server mapping updates."""
        output_to_monitor_ids: dict[str, list[str]] = {}
        for mid, monitor in self._inprocess_monitors.items():
            output_to_monitor_ids.setdefault(monitor.server_output_name, []).append(mid)

        for mid, monitor in list(self._inprocess_monitors.items()):
            try:
                new_output = self.sio_client.get_server_output_name(
                    monitor.node_uuid, monitor.output_name, self.graph.nodes
                )
            except Exception as e:
                self._log(f"Could not refresh monitor binding for {mid}: {e}")
                continue

            if not new_output or new_output == monitor.server_output_name:
                continue

            old_output = monitor.server_output_name
            changed = monitor.retarget_server_output(new_output)
            if not changed:
                continue

            # Keep Socket.IO subscriptions aligned with the rebinding.
            try:
                self.sio_client.subscribe(new_output)
            except Exception as e:
                self._log(
                    f"Warning: could not subscribe to retargeted output '{new_output}' "
                    f"for monitor {mid}: {e}"
                )

            old_watchers = output_to_monitor_ids.get(old_output, [])
            if mid in old_watchers:
                old_watchers.remove(mid)
            if not old_watchers:
                try:
                    self.sio_client.unsubscribe(old_output)
                except Exception as e:
                    self._log(
                        f"Warning: could not unsubscribe old output '{old_output}' "
                        f"for monitor {mid}: {e}"
                    )

            self._log(f"Retargeted in-process monitor {mid}: {old_output} -> {new_output}")

    @staticmethod
    def _force_kill_after(proc: subprocess.Popen, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            proc.kill()
        except Exception:
            pass

    # =========================================================================
    # Background reaper
    # =========================================================================

    def _reaper_loop(self):
        while not self._reaper_stop.is_set():
            time.sleep(2.0)
            try:
                dead = []
                with self._lock:
                    for mid, info in self.active_monitors.items():
                        if info["process"].poll() is not None:
                            dead.append(mid)
                for mid in dead:
                    with self._lock:
                        info = self.active_monitors.pop(mid, None)
                    if info:
                        self._log(
                            f"Reaped exited monitor {mid} "
                            f"(rc={info['process'].returncode})"
                        )
            except Exception as e:
                self._log(f"Reaper error: {e}")

    # =========================================================================
    # API compatibility helpers
    # =========================================================================

    def _find_and_close_monitor(self, monitor_info):
        node_uuid, output_name = monitor_info
        with self._lock:
            candidates = [
                mid for mid, info in self.active_monitors.items()
                if info["node_uuid"] == node_uuid and info["output_name"] == output_name
            ]
        for mid in candidates:
            self.close_monitor(mid)
        # In-process monitors
        for mid, monitor in list(self._inprocess_monitors.items()):
            if monitor.node_uuid == node_uuid and monitor.output_name == output_name:
                self.close_monitor(mid)

    def after_dpg_init(self):
        self._log(
            "Ready — monitors will launch as separate OS processes "
            "(or in-process if MonitorBus is active)"
        )

    def start_periodic_tasks(self):
        """Set up a recurring DPG frame callback to tick in-process monitors."""
        if self._monitor_bus is not None:
            self._schedule_inprocess_tick()

    # ------------------------------------------------------------------
    # Recurring in-process monitor tick
    # ------------------------------------------------------------------

    def _schedule_inprocess_tick(self) -> None:
        """Schedule the next per-frame tick one frame from now."""
        try:
            next_frame = dpg.get_frame_count() + 1
            dpg.set_frame_callback(next_frame, self._inprocess_tick)
        except Exception:
            pass

    def _inprocess_tick(self) -> None:
        """Drain all in-process monitor queues and reschedule."""
        dead = []
        for mid, monitor in list(self._inprocess_monitors.items()):
            try:
                still_alive = monitor.render_frame()
            except Exception as exc:
                self._log(f"In-process monitor tick error ({mid}): {exc}")
                still_alive = False
            if not still_alive:
                dead.append(mid)

        for mid in dead:
            self._inprocess_monitors.pop(mid, None)
            self._log(f"In-process monitor {mid} closed (window was destroyed)")

        # Reschedule for the next frame
        self._schedule_inprocess_tick()

    def cleanup(self):
        self._reaper_stop.set()
        with self._lock:
            monitor_ids = list(self.active_monitors.keys())
        for mid in monitor_ids:
            self.close_monitor(mid)
        for mid in list(self._inprocess_monitors.keys()):
            self.close_monitor(mid)
        self._log("All monitors terminated")

    # =========================================================================
    # Backward-compat (subprocess start_periodic_tasks)
    # =========================================================================

    # (kept for API compatibility — the subprocess-only path had a no-op here)
