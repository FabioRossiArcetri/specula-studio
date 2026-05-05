"""
monitor_manager.py
==================
Manages the lifecycle of standalone monitor window subprocesses.

Each monitor window is a separate OS process (monitor_window.py) with its own
DearPyGui viewport and its own Socket.IO connection.

Key addition over the first subprocess-based version:
  - The display-server URL / port is discovered at simulation start time by
    scanning specula's stdout.  Monitors opened before the port is known are
    queued as *pending* and launched as soon as the URL arrives.
  - A background reaper thread harvests subprocesses that have exited.
"""

import os
import subprocess
import sys
import threading
import time
import traceback


class MonitorManager:
    """Manages standalone monitor window subprocesses."""

    def __init__(self, sio_client, graph, debug: bool = True):
        """
        Parameters
        ----------
        sio_client : SocketIOClient
        graph      : GraphManager
        debug      : bool
        """
        self.sio_client = sio_client
        self.graph = graph
        self.debug = debug

        # monitor_id -> info dict
        self.active_monitors: dict = {}
        self._lock = threading.Lock()

        # Monitors requested before the display-server URL was known
        self._pending_monitors: list = []   # list of (sender, app_data, user_data)
        self._server_url: str | None = None  # set by on_display_server_ready()

        # Path to the coordination file written by SimulationControl
        self._server_url_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "specula_studio_server.json",
        )

        # Background reaper thread
        self._reaper_stop = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True
        )
        self._reaper_thread.start()

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

    def _safe_update_monitor_status(self, monitor_id: str, status: str):
        pass

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
        Spawn a standalone monitor window for a node output.
        ``user_data`` must be ``(node_uuid, output_name)``.

        If the display-server URL is not yet known, the request is queued and
        fulfilled as soon as ``on_display_server_ready`` is called.
        """
        node_uuid, output_name = user_data

        node_data = self.graph.nodes.get(node_uuid)
        if not node_data:
            self._log(f"open_monitor: node {node_uuid} not found")
            return

        node_name = node_data.get("name", "Unknown")
        node_type = node_data.get("type", "Unknown")

        # ── Determine which server URL to use ─────────────────────────────────
        # Priority: (1) explicitly discovered URL, (2) sio_client URL if connected,
        # (3) defer until URL is known.
        if self._server_url:
            server_url = self._server_url
        elif self.sio_client.connected:
            server_url = self.sio_client.server_url
        else:
            # URL not known yet — queue and return
            self._log(
                f"Display-server URL not yet known; queuing monitor for "
                f"{node_name}.{output_name}"
            )
            self._pending_monitors.append((sender, app_data, user_data))
            return

        # ── Prevent duplicate windows for the same output ────────────────────
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

        # ── Resolve server output name ────────────────────────────────────────
        try:
            server_output_name = self.sio_client.get_server_output_name(
                node_uuid, output_name, self.graph.nodes
            )
        except Exception as e:
            self._log(f"Could not resolve server output name: {e}")
            server_output_name = f"{node_name}.{output_name}"

        # ── Build subprocess command ──────────────────────────────────────────
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "monitor_window.py"
        )
        cmd = [
            sys.executable, script,
            "--server-output-name",  server_output_name,
            "--node-name",           node_name,
            "--output-name",         output_name,
            "--server-url-file",     self._server_url_file,
            # Pass the current best-known URL as a starting point
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

    def close_monitor(self, monitor_id: str, from_window_close: bool = False):
        """Terminate a monitor process."""
        with self._lock:
            info = self.active_monitors.pop(monitor_id, None)
        if info is None:
            return
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

    def after_dpg_init(self):
        self._log("Ready — monitors will launch as separate OS processes")

    def start_periodic_tasks(self):
        pass

    def cleanup(self):
        self._reaper_stop.set()
        with self._lock:
            monitor_ids = list(self.active_monitors.keys())
        for mid in monitor_ids:
            self.close_monitor(mid)
        self._log("All monitor processes terminated")