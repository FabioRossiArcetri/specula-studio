"""
Starting, stepping and aborting SPECULA runs.

The control panel (:class:`~.ui.control_panel.ControlPanelView`) is shown
either in its own OS window — a separate process connected through
:mod:`.control_link` (default) — or as a window inside the editor
(preference *control_panel* = "docked").  Both pass the run options as a dict
to :meth:`SimulationControl.start`; terminal output and status go back to
whichever panel is open.  Runs always execute in the editor process.

A run writes its files into a private temporary directory:

* ``<name>_simul.yml``   — the simulation (with an injected DisplayServer
  when monitors are served over Socket.IO);
* ``override<N>_*.yml``  — copies of the enabled override files, passed to
  SPECULA after the base file so SPECULA applies them itself;
* ``server.json``        — display-server URL + session id, read by the
  subprocess monitor windows.
"""

import collections
import json
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid

import dearpygui.dearpygui as dpg

from . import sshutil
from .control_link import ControlServer, ControlWindowProcess
from .core.run_prep import write_run_files
from .simulation_backend import InProcessBackend, RemoteBackend, SimulationBackend
from .theme_manager import ThemeManager
from .ui.control_panel import DEFAULT_OPTIONS, TERMINAL_MAX_CHUNKS, ControlPanelView

log = logging.getLogger(__name__)

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "")
_DOCKED_WINDOW = "sim_control_window"

DISPLAY_SERVER_LOST = (
    "[WARNING] Lost the display server ({url}) while the simulation is still running: "
    "monitors stop updating.\n"
    "  SPECULA versions without the DisplayServer idle fix stop the display server after "
    "60 s without a simulation step (e.g. a stepping run left paused).  Otherwise look "
    "for an error above, or check the network.\n"
)
STEPPING_DISPLAY_NOTE = (
    "[NOTE] Stepping mode with a display server: SPECULA versions without the "
    "DisplayServer idle fix stop the display server after 60 s without a step, and the "
    "monitors with it.\n"
)


class SimulationControl:
    def __init__(self, editor):
        self.editor        = editor
        self.terminal_data = collections.deque(maxlen=TERMINAL_MAX_CHUNKS)
        self._terminal_lock = threading.Lock()
        self.is_running    = False
        self._backend: SimulationBackend | None = None
        self._run_dir: str | None = None
        self._server_url_file: str | None = None
        # Session id: one per run; written into the DisplayServer block, the
        # coordination file and passed to every subprocess monitor, so stale
        # monitors of a previous run never attach to a new server.
        self._session_id: str = ""
        # Session whose display server the editor reached, and whether it was
        # lost while the run went on (reported in the terminal)
        self._ds_session: str = ""
        self._ds_lost = False
        self._status = "idle"
        self.view: ControlPanelView | None = None       # panel inside the editor
        self._link: ControlServer | None = None         # separate control window
        self._window: ControlWindowProcess | None = None
        self.theme_manager = ThemeManager()

    # ── Coordination file ─────────────────────────────────────────────────────

    def _write_server_url_file(self, url: str):
        """Write URL + session_id atomically so monitors always see a consistent pair."""
        if not self._server_url_file:
            return
        try:
            tmp = self._server_url_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"url": url, "session_id": self._session_id}, f)
            os.replace(tmp, self._server_url_file)
            log.info("Display server URL %s (session %s)", url, self._session_id)
        except OSError as exc:
            log.warning("Could not write the server URL file: %s", exc)

    def _clear_server_url_file(self):
        if self._server_url_file:
            try:
                os.remove(self._server_url_file)
            except OSError:
                pass

    # ── YAML window ───────────────────────────────────────────────────────────

    def _get_current_yaml_content(self):
        try:
            return self.editor.fh.export_to_yaml_string(include_defaults=False)
        except Exception as exc:
            log.exception("Error generating YAML")
            return f"Error generating YAML:\n{exc}"

    def show_yaml_window(self):
        yaml_content = self._get_current_yaml_content()
        window_tag = f"yaml_display_window_{int(time.time() * 1000)}"
        with dpg.window(label="Simulation YAML", tag=window_tag, width=800, height=600):
            with dpg.group(horizontal=True):
                dpg.add_button(label="Copy to Clipboard", width=140,
                               callback=lambda: dpg.set_clipboard_text(yaml_content))
                dpg.add_button(label="Close", width=80,
                               callback=lambda: dpg.delete_item(window_tag))
            dpg.add_separator()
            dpg.add_input_text(default_value=yaml_content, multiline=True, readonly=True,
                               width=-1, height=-1)

    # ── Control panel: separate window or docked ──────────────────────────────

    def _new_run_dir(self) -> str:
        """Fresh private directory for this run (the previous one is removed)."""
        if self._run_dir and os.path.isdir(self._run_dir):
            shutil.rmtree(self._run_dir, ignore_errors=True)
        self._run_dir = tempfile.mkdtemp(prefix="specula_studio_run_")
        return self._run_dir

    def show_control_window(self, *_):
        """Open the control panel (separate window unless the user chose docked)."""
        if self.editor.preferences.get("control_panel", "window") == "docked":
            self.show_docked_panel()
            return
        try:
            self._ensure_link()
            self._window.launch()
        except Exception as exc:
            log.warning("Cannot open the control panel in its own window (%s); "
                        "showing it inside the editor", exc)
            self.show_docked_panel()

    def show_docked_panel(self):
        if dpg.does_item_exist(_DOCKED_WINDOW):
            dpg.show_item(_DOCKED_WINDOW)
            dpg.focus_item(_DOCKED_WINDOW)
            return
        self.view = ControlPanelView(self, self.theme_manager, self._recent_hosts())
        with dpg.window(label="Simulation Control Panel", tag=_DOCKED_WINDOW,
                        width=980, height=640):
            pass
        self.view.build(_DOCKED_WINDOW)
        self.view.reset_terminal(self._terminal_text())
        self.view.set_status(self._status)

    def _recent_hosts(self) -> list:
        return list(self.editor.preferences.get("remote_hosts", []) or [])

    def _ensure_link(self):
        if self._link is None:
            self._link = ControlServer(on_connect=self._on_window_connected)
            self._window = ControlWindowProcess(self._link)

    def _on_window_connected(self):
        self._link.send({"type": "init", "recent_hosts": self._recent_hosts(),
                         "terminal": self._terminal_text(), "status": self._status})

    def poll_commands(self):
        """Run the commands sent by the control window (GUI thread, every frame)."""
        if self._link is None:
            return
        while True:
            try:
                msg = self._link.commands.get_nowait()
            except queue.Empty:
                return
            kind = msg.get("type")
            try:
                if kind == "start":
                    self.start(msg.get("options") or {})
                elif kind == "step":
                    self.step(int(msg.get("n", 1)))
                elif kind == "abort":
                    self.abort()
                elif kind == "test_ssh":
                    self.test_ssh(msg.get("target", ""))
                elif kind == "clear_terminal":
                    self.clear_terminal()
            except Exception as exc:
                log.exception("Control command %s failed", kind)
                self.append_terminal(f"[ERROR] {kind}: {exc}\n")

    def close_control_window(self):
        if self._window is not None:
            self._window.stop()
        if self._link is not None:
            self._link.close()
            self._link = None

    # ── output to the panels (any thread) ─────────────────────────────────────

    def _terminal_text(self) -> str:
        with self._terminal_lock:
            return "".join(self.terminal_data)

    def append_terminal(self, text):
        with self._terminal_lock:
            self.terminal_data.append(text)
        if self.view is not None:
            self.view.append_terminal(text)
        if self._link is not None:
            self._link.send({"type": "terminal", "text": text})

    def clear_terminal(self):
        with self._terminal_lock:
            self.terminal_data.clear()
        if self.view is not None:
            self.view.reset_terminal("")
        if self._link is not None:
            self._link.send({"type": "terminal_reset", "text": ""})

    def _set_status(self, text: str):
        self._status = text
        if self.view is not None:
            self.view.set_status(text)
        if self._link is not None:
            self._link.send({"type": "status", "text": text})

    def test_ssh(self, target: str):
        """Check a prompt-free login; the result goes to the panels."""
        def work():
            ok, msg = sshutil.check_login(target)
            if self.view is not None:
                self.view.set_ssh_test(msg, ok)
            if self._link is not None:
                self._link.send({"type": "ssh_test", "message": msg, "ok": ok})
            self.append_terminal(f"[SSH] {target}: {msg}\n")

        threading.Thread(target=work, daemon=True, name="ssh-test").start()

    # ── Display server ────────────────────────────────────────────────────────

    def _on_display_server_port_found(self, port: int, remote_ip: str | None = "localhost"):
        host = "127.0.0.1" if remote_ip in (None, *_LOCAL_HOSTS) else remote_ip
        url = f"http://{host}:{port}"
        self.append_terminal(f"[INFO] Display server running at {url}\n")
        sio = self.editor.nm.sio_client
        sio.server_url = url
        self._write_server_url_file(url)
        self.editor.nm.monitors.on_display_server_ready(url)
        if not sio.connected:
            threading.Thread(target=sio.reconnect, daemon=True).start()

    # Seconds to wait after a lost connection before reporting it: the
    # display server also goes away at the normal end of a run.
    DS_LOST_GRACE = 5.0

    def _on_display_server_connection(self, connected: bool):
        """The editor's connection to the run's DisplayServer went up or down (any thread)."""
        session = self._session_id
        if connected:
            self._ds_session = session
            if self._ds_lost:
                self._ds_lost = False
                self.append_terminal("[INFO] Reconnected to the display server\n")
                self._set_status(self._status.replace(" — display server lost", ""))
            return
        if not self.is_running or self._ds_session != session:
            return

        def report():
            sio = self.editor.nm.sio_client
            if (self.is_running and self._session_id == session and not sio.connected
                    and not self._ds_lost):
                self._ds_lost = True
                self.append_terminal(DISPLAY_SERVER_LOST.format(url=sio.server_url))
                self._set_status(f"{self._status} — display server lost")

        timer = threading.Timer(self.DS_LOST_GRACE, report)
        timer.daemon = True
        timer.start()

    def _schedule_display_server_reconnect(self, expected_url: str, delay: float = 4.0):
        """Fallback when SPECULA does not print its port: try the expected URL."""
        def _attempt(attempt_no, delay_s):
            time.sleep(delay_s)
            sio = self.editor.nm.sio_client
            if sio is None or sio.connected or not self.is_running:
                return
            log.info("Fallback connection attempt %d to %s", attempt_no, expected_url)
            sio.server_url = expected_url
            sio.reconnect()
            if not sio.connected and attempt_no == 1:
                threading.Thread(target=_attempt, args=(2, 6.0), daemon=True).start()

        threading.Thread(target=_attempt, args=(1, delay), daemon=True).start()

    # ── Run ───────────────────────────────────────────────────────────────────

    def _on_backend_finished(self):
        self.is_running = False
        self._clear_server_url_file()
        self.editor.nm.monitors.set_backend(None)
        self._set_status("finished")

    def _display_server_params(self) -> set:
        """Constructor parameters of the installed DisplayServer (class catalog)."""
        tpl = self.editor.all_templates.get("DisplayServer") or {}
        return set(tpl.get("parameters") or {})

    def _run_inputs(self) -> tuple[dict, list[str]]:
        """Base simulation and enabled override files for the run."""
        ed = self.editor
        current = ed._current_export()
        om = ed.override_manager
        if om.has_base_snapshot() and om.any_enabled():
            # Preview active: the editor shows base + overrides; SPECULA gets
            # the base (with the user's edits) plus the override files.
            return om.rebased(current), om.get_enabled_overrides()
        return current, []

    # ── actions of the control panels ─────────────────────────────────────────

    def start(self, options: dict | None = None):
        """Start a run with *options* (see ``control_panel.DEFAULT_OPTIONS``)."""
        if options is None and self.view is not None:
            options = self.view.options()
        opts = {**DEFAULT_OPTIONS, **(options or {})}
        if self.is_running:
            self.append_terminal("[WARNING] Simulation is already running\n")
            return

        self._session_id = str(uuid.uuid4())
        backend_mode = opts["backend"]
        in_process = backend_mode == "In-Process"
        remote_ip = "localhost" if in_process else (opts["host"] or "localhost").strip()
        remote_user = "" if in_process else (opts["user"] or "").strip()
        is_ssh = not in_process and remote_ip not in _LOCAL_HOSTS
        ds_port = int(opts["ds_port"])

        # ── files ─────────────────────────────────────────────────────────────
        run_dir = self._new_run_dir()
        self._server_url_file = os.path.join(run_dir, "server.json")
        base, overrides = self._run_inputs()
        files = write_run_files(
            run_dir, base, overrides,
            sim_name=self.editor.current_simulation_name or "untitled",
            session_id=None if in_process else self._session_id,
            display_port=ds_port,
            display_host="0.0.0.0" if is_ssh else None,
            display_params=self._display_server_params(),
        )
        if overrides:
            self.append_terminal(f"[INFO] {len(overrides)} override file(s) passed to SPECULA\n")
        if not in_process and opts["stepping"]:
            self.append_terminal(STEPPING_DISPLAY_NOTE)

        # ── backend ───────────────────────────────────────────────────────────
        mm = self.editor.nm.monitors
        mm.set_session_id(self._session_id)
        mm.set_server_url_file(self._server_url_file)
        resolved_ip = sshutil.resolve_host(remote_ip) if is_ssh else remote_ip
        if in_process:
            self._backend = InProcessBackend(monitor_bus=self.editor.nm.monitor_bus)
        else:
            self._backend = RemoteBackend(remote_ip=remote_ip, remote_user=remote_user)
            if is_ssh:
                self.editor.preferences.add_remote_host(remote_ip, remote_user)
        mm.set_inprocess_mode(in_process)
        mm.set_backend(self._backend)

        cmd_args = {
            "stepping":     bool(opts["stepping"]),
            "nsimul":       int(opts["nsimul"]),
            "cpu":          bool(opts["cpu"]),
            "target":       int(opts["target"]),
            "precision":    int(opts["precision"]),
            "log_level":    opts["log_level"],
            "profile":      bool(opts["profile"]),
            "diagram":      bool(opts["diagram"]),
            "resolved_ip":  resolved_ip,
            "remote_ip":    remote_ip,
        }

        sio = self.editor.nm.sio_client
        sio.on_connection_change = self._on_display_server_connection
        self._ds_session = ""
        self._ds_lost = False
        if sio.connected:
            try:
                sio.disconnect()
            except Exception:
                pass
        expected_url = None
        if not in_process and ds_port:
            host = resolved_ip if is_ssh else "127.0.0.1"
            expected_url = f"http://{host}:{ds_port}"
            sio.server_url = expected_url

        self.is_running = True
        self._set_status(f"running ({backend_mode})")
        self._backend.start(
            files, cmd_args, self.append_terminal,
            on_port_found=self._on_display_server_port_found,
            on_finished=self._on_backend_finished,
        )

        if expected_url:
            self._write_server_url_file(expected_url)
            mm.on_display_server_ready(expected_url)
            self._schedule_display_server_reconnect(expected_url)

    def step(self, n: int = 1):
        if self._backend is None:
            self.append_terminal("[WARNING] No active backend\n")
            return
        if n > 1:
            self.append_terminal(f"[INFO] Advancing {n} step(s)...\n")
        # SPECULA's stepping prompt accepts a number of steps to advance.
        self._backend.step(n)

    def abort(self):
        if self._backend is not None:
            self._backend.abort()
            self.append_terminal("[INFO] Abort signal sent\n")
        self._clear_server_url_file()
        self.is_running = False
        self._set_status("aborted")
