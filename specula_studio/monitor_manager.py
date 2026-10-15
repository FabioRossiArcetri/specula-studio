"""
monitor_manager.py
==================
Lifecycle of the monitor windows that show simulation outputs live.

In-process runs (InProcessBackend)
    ``open_monitor`` creates an :class:`InProcessMonitor` — a window inside
    the editor — and asks the backend for a probe on ``<object>.<output>``.
    Probes push data to the :class:`MonitorBus`; :meth:`tick` (called every
    frame) drains it into the windows.

Subprocess runs (RemoteBackend, local or SSH)
    Each monitor is a separate process (``python -m
    specula_studio.monitor_window``) with its own Socket.IO connection to the
    DisplayServer of the run.  The server URL and the run's session id are
    shared through a small JSON file in the run directory.  Monitors opened
    before the server is known are queued and started when it is.
"""

import os
import subprocess
import sys
import threading
import time
import traceback


from .inprocess_monitor import InProcessMonitor
import logging

log = logging.getLogger(__name__)


def _subprocess_env() -> dict:
    """Environment for child processes that must import ``specula_studio``.

    Works both for an installed package and for a source checkout run via
    the top-level ``main.py`` shim (the checkout root is put on PYTHONPATH).
    """
    env = os.environ.copy()
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["SPECULA_STUDIO_LOG_LEVEL"] = logging.getLevelName(
        logging.getLogger("specula_studio").getEffectiveLevel()
    )
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (pkg_parent, env.get("PYTHONPATH", "")) if p
    )
    return env


def _is_direct_backend(backend) -> bool:
    try:
        from .simulation_backend import InProcessBackend
        return isinstance(backend, InProcessBackend) and backend._monitor_bus is not None
    except Exception:
        return False


class MonitorManager:
    """Manages monitor windows (subprocess and in-process)."""

    def __init__(self, sio_client, graph, monitor_bus=None, debug: bool = True):
        self.sio_client  = sio_client
        self.graph       = graph
        self.debug       = debug
        self._monitor_bus = monitor_bus
        self._use_inprocess: bool  = False
        self._backend              = None
        self._session_id: str      = ""   # set by simulation_control (Issue 9)

        self.active_monitors: dict  = {}
        self._lock = threading.Lock()
        self._inprocess_monitors: dict[str, InProcessMonitor] = {}
        self._pending_monitors: list = []
        self._server_url: str | None = None
        self._tick_count: int = 0

        self._server_url_file: str | None = None   # set per run

        self._reaper_stop   = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True
        )
        self._reaper_thread.start()

    # ── Mode / backend / session control ──────────────────────────────────────

    def set_inprocess_mode(self, enabled: bool) -> None:
        self._use_inprocess = enabled
        self._log(f"Monitor mode: {'in-process' if enabled else 'subprocess'}")

    def set_backend(self, backend) -> None:
        self._backend = backend
        self._log(
            f"Backend reference {'set' if backend is not None else 'cleared'}: "
            f"{type(backend).__name__ if backend else 'None'}"
        )

    def set_server_url_file(self, path: str | None) -> None:
        """Coordination file of the current run (read by subprocess monitors)."""
        self._server_url_file = path

    def set_session_id(self, session_id: str) -> None:
        """Store the session_id for the current simulation run (Issue 9)."""
        self._session_id = session_id
        self._log(f"Session ID updated: {session_id}")

    # ── Logging ──────────────────────────────────────────────────────────[...]

    def _log(self, msg: str):
        log.debug(msg)

    # ── Server-event stubs ────────────────────────────────────────────────────

    def on_server_connect(self):    pass
    def on_server_disconnect(self): pass
    def on_server_connect_error(self, data): pass
    def on_data_update(self, name: str, raw_data): pass

    def on_server_params(self, data: dict):
        """The DisplayServer listed its objects: start monitors waiting for it."""
        if self._use_inprocess:
            return          # in-process monitors use probe topics, not the server
        self._flush_pending_monitors()

    # ── Public query helpers ──────────────────────────────────────────────────

    def is_monitor_open(self, node_uuid: str, output_name: str) -> bool:
        with self._lock:
            for info in self.active_monitors.values():
                if (
                    info.get("node_uuid") == node_uuid
                    and info.get("output_name") == output_name
                    and info["process"].poll() is None
                ):
                    return True
        for m in self._inprocess_monitors.values():
            if m.node_uuid == node_uuid and m.output_name == output_name and m.is_open:
                return True
        return False

    def find_monitor_id(self, node_uuid: str, output_name: str) -> str | None:
        with self._lock:
            for mid, info in self.active_monitors.items():
                if (
                    info.get("node_uuid") == node_uuid
                    and info.get("output_name") == output_name
                    and info["process"].poll() is None
                ):
                    return mid
        for mid, m in self._inprocess_monitors.items():
            if m.node_uuid == node_uuid and m.output_name == output_name and m.is_open:
                return mid
        return None

    # ── Display-server URL notification ───────────────────────────────────────

    def on_display_server_ready(self, url: str):
        self._log(f"Display server ready at {url}")
        old_url = self._server_url
        self._server_url = url

        if old_url != url:
            with self._lock:
                stale = [
                    (mid, info)
                    for mid, info in self.active_monitors.items()
                    if info.get("server_url") != url and info["process"].poll() is None
                ]
            for mid, info in stale:
                self._log(f"Restarting monitor {mid} with new URL {url}")
                try:
                    info["process"].terminate()
                except Exception:
                    pass
                self.open_monitor(None, None, (info["node_uuid"], info["output_name"]))
                with self._lock:
                    self.active_monitors.pop(mid, None)

        self._flush_pending_monitors()

    def _flush_pending_monitors(self) -> None:
        pending = list(self._pending_monitors)
        self._pending_monitors.clear()
        for (s, a, ud) in pending:
            self._log(f"Flushing pending monitor for {ud}")
            self.open_monitor(s, a, ud)

    # ── Open / close ──────────────────────────────────────────────────────────
    def open_monitor(self, sender, app_data, user_data):
        """Open a monitor for the specified node output.

        FIX: In direct mode, use direct probe topic (node_name.output_name).
        In Socket.IO mode, resolve server_output_name via DisplayServer.
        """
        node_uuid, output_name = user_data

        if node_uuid not in self.graph.nodes:
            self._log(f"Node {node_uuid} not found")
            return

        node_data = self.graph.nodes.get(node_uuid, {})
        node_name = node_data.get("name", node_uuid)

        # ── Direct in-process mode path ────────────────────────────────────────
        if self._use_inprocess:
            # In direct mode, use the probe topic format directly:
            # no Socket.IO resolution needed
            server_output_name = f"{node_name}.{output_name}"

            self._open_inprocess_monitor(
                node_uuid, node_name, output_name, server_output_name
            )
            return

        # ── Subprocess/Socket.IO path ──────────────────────────────────────────
        try:
            server_output_name = self.sio_client.get_server_output_name(
                node_uuid, output_name, self.graph.nodes
            )
        except ValueError as e:
            self._log(
                f"Cannot resolve server output name: {e}. "
                f"Queueing monitor for {node_name}.{output_name}"
            )
            self._pending_monitors.append((sender, app_data, user_data))
            return

        node_type = node_data.get("type", "Unknown")

        if self._server_url:
            server_url = self._server_url
        elif self.sio_client.connected:
            server_url = self.sio_client.server_url
        else:
            self._log(
                f"Display-server URL not yet known; queueing monitor for "
                f"{node_name}.{output_name}"
            )
            self._pending_monitors.append((sender, app_data, user_data))
            return

        with self._lock:
            for info in list(self.active_monitors.values()):
                if (
                    info["node_uuid"]   == node_uuid
                    and info["output_name"] == output_name
                    and info["process"].poll() is None
                ):
                    self._log(f"Monitor already open for {node_name}.{output_name}")
                    return

        cmd = [
            sys.executable, "-m", "specula_studio.monitor_window",
            "--server-output-name", server_output_name,
            "--node-name",          node_name,
            "--output-name",        output_name,
            "--server-url",         server_url,
        ]
        if self._server_url_file:
            cmd += ["--server-url-file", self._server_url_file]
        # Pass session_id so the monitor can validate the coordination file (Issue 9)
        if self._session_id:
            cmd += ["--session-id", self._session_id]

        self._log(f"Spawning monitor: {node_name}.{output_name} → {server_output_name} @ {server_url}")

        try:
            process = subprocess.Popen(cmd, env=_subprocess_env())
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
                "session_id":         self._session_id,
                "started_at":         time.time(),
            }

    # ── In-process monitor helpers ─────────────────────────────────────────────

    def _open_inprocess_monitor(
        self,
        node_uuid: str,
        node_name: str,
        output_name: str,
        server_output_name: str,
    ) -> None:
        for monitor in self._inprocess_monitors.values():
            if (monitor.node_uuid == node_uuid and monitor.output_name == output_name
                    and monitor.is_open):
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

        if _is_direct_backend(self._backend):
            # None until the simulation is built; the backend then adds probes
            # for every topic with subscribers when the loop starts.
            monitor._probe = self._backend.attach_probe(server_output_name)
        self._log(f"In-process monitor {monitor_id} opened for {server_output_name}")

    def close_monitor(self, monitor_id: str, from_window_close: bool = False):
        with self._lock:
            info = self.active_monitors.pop(monitor_id, None)
        if info is not None:
            proc = info["process"]
            if proc.poll() is None:
                try:
                    proc.terminate()
                    threading.Thread(
                        target=self._force_kill_after, args=(proc, 3.0), daemon=True
                    ).start()
                except Exception as e:
                    self._log(f"Error terminating: {e}")
            return

        monitor = self._inprocess_monitors.pop(monitor_id, None)
        if monitor is not None:
            if monitor._probe is not None and _is_direct_backend(self._backend):
                self._backend.detach_probe(monitor._probe)
            monitor.close()

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

    # ── Background reaper ──────────────────────────────────────────────────────

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
                        self._log(f"Reaped exited monitor {mid} (rc={info['process'].returncode})")
            except Exception as e:
                self._log(f"Reaper error: {e}")

    # ── Ticking ──────────────────────────────────────────────────────────[...]

    def after_dpg_init(self):
        self._log("Ready")

    def start_periodic_tasks(self):
        self._log("start_periodic_tasks called (ticking via main render loop)")

    def tick(self) -> None:
        """Drain and render all in-process monitors every frame.

        FIX: Add diagnostic logging to verify data is flowing through the bus.
        """
        if not self._inprocess_monitors:
            return

        # Diagnostic: log subscribed topics and push counts periodically
        self._tick_count += 1
        if self._tick_count % 300 == 0 and log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Subscribed topics: %s | push counts: %s | drop counts: %s",
                self._monitor_bus.all_subscribed_outputs(),
                self._monitor_bus.push_counts(),
                self._monitor_bus.drop_counts(),
            )

        dead = []
        for mid, monitor in list(self._inprocess_monitors.items()):
            try:
                still_alive = monitor.render_frame()
            except Exception as exc:
                self._log(f"In-process monitor tick error ({mid}): {exc}")
                traceback.print_exc()
                still_alive = False
            if not still_alive:
                dead.append(mid)
        for mid in dead:
            monitor = self._inprocess_monitors.pop(mid, None)
            # Window closed with its [x]: stop the probe feeding it.
            if monitor is not None and monitor._probe is not None \
                    and _is_direct_backend(self._backend):
                self._backend.detach_probe(monitor._probe)
            if monitor is not None:
                monitor.close()   # unsubscribe + delete the hidden window
            self._log(f"In-process monitor {mid} closed")

    def cleanup(self):
        self._reaper_stop.set()
        with self._lock:
            monitor_ids = list(self.active_monitors.keys())
        for mid in monitor_ids:
            self.close_monitor(mid)
        for mid in list(self._inprocess_monitors.keys()):
            self.close_monitor(mid)
        self._log("All monitors terminated")
