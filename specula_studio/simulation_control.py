"""
Simulation control panel: starting, stepping and aborting SPECULA runs.

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
import shutil
import tempfile
import threading
import time
import uuid

import dearpygui.dearpygui as dpg

from .core.run_prep import DEFAULT_DISPLAY_SERVER_PORT, write_run_files
from .simulation_backend import (
    InProcessBackend,
    RemoteBackend,
    SimulationBackend,
    _resolve_remote_hostname,
)
from .theme_manager import ThemeManager

log = logging.getLogger(__name__)

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "")
_TERMINAL_MAX_CHUNKS = 2000


class SimulationControl:
    def __init__(self, editor):
        self.editor        = editor
        self.terminal_data = collections.deque(maxlen=_TERMINAL_MAX_CHUNKS)
        self._terminal_lock = threading.Lock()
        self.is_running    = False
        self._backend: SimulationBackend | None = None
        self._run_dir: str | None = None
        self._server_url_file: str | None = None
        # Session id: one per run; written into the DisplayServer block, the
        # coordination file and passed to every subprocess monitor, so stale
        # monitors of a previous run never attach to a new server.
        self._session_id: str = ""
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

    # ── Control window ────────────────────────────────────────────────────────

    def _new_run_dir(self) -> str:
        """Fresh private directory for this run (the previous one is removed)."""
        if self._run_dir and os.path.isdir(self._run_dir):
            shutil.rmtree(self._run_dir, ignore_errors=True)
        self._run_dir = tempfile.mkdtemp(prefix="specula_studio_run_")
        return self._run_dir

    def show_control_window(self):
        if dpg.does_item_exist("sim_control_window"):
            dpg.show_item("sim_control_window")
            dpg.focus_item("sim_control_window")
            return

        tm = self.theme_manager
        hosts = self.editor.preferences.get("remote_hosts", [])
        last = hosts[0] if hosts else {"host": "localhost", "user": ""}

        with dpg.window(label="Simulation Control Panel", tag="sim_control_window",
                        width=980, height=640):
            with dpg.group(horizontal=True):
                with dpg.child_window(width=520):
                    dpg.add_text("Backend Mode", color=tm.get_color("control_section_header"))
                    dpg.add_combo(
                        label="Execution Mode",
                        items=["Remote", "In-Process"],
                        tag="sim_backend",
                        default_value="In-Process",
                        callback=self._on_backend_mode_changed,
                    )
                    dpg.add_text(
                        "In-Process: runs inside the editor, monitors fed directly.\n"
                        "Remote: runs the 'specula' command, locally (localhost)\n"
                        "or over SSH; monitors connect to its DisplayServer.",
                        color=tm.get_color("text_hint"),
                    )
                    dpg.add_text("Remote Server Configuration",
                                 color=tm.get_color("control_subsection_header"),
                                 tag="sim_remote_settings_label")
                    if hosts:
                        dpg.add_combo(
                            label="Recent hosts",
                            items=[self._host_label(h) for h in hosts],
                            default_value=self._host_label(last), width=-140,
                            callback=self._on_recent_host,
                        )
                    dpg.add_input_text(label="Server IP / Hostname", tag="sim_remote_ip",
                                       default_value=last.get("host", "localhost"),
                                       hint="localhost, 127.0.0.1, or remote host", width=-140)
                    dpg.add_input_text(label="SSH Username", tag="sim_remote_user",
                                       default_value=last.get("user", ""),
                                       hint="empty = current user", width=-140)
                    dpg.add_input_int(label="Display server port", tag="sim_ds_port",
                                      default_value=DEFAULT_DISPLAY_SERVER_PORT,
                                      min_value=0, max_value=65535, width=-140)
                    dpg.add_text("0 = pick a free port (requires a recent SPECULA)",
                                 color=tm.get_color("text_hint"))
                    dpg.add_separator()
                    dpg.add_text("Simulation Arguments",
                                 color=tm.get_color("control_subsection_header"))
                    dpg.add_input_int(label="N-Simul", tag="sim_nsimul", default_value=1, min_value=1)
                    dpg.add_checkbox(label="Use CPU", tag="sim_cpu")
                    dpg.add_input_int(label="GPU ID", tag="sim_target", default_value=0)
                    dpg.add_combo(label="Precision", items=["0", "1"],
                                  tag="sim_precision", default_value="1")
                    dpg.add_combo(label="Log Level", items=["DEBUG", "INFO", "WARNING"],
                                  tag="sim_log", default_value="INFO")
                    dpg.add_checkbox(label="Stepping Mode", tag="sim_stepping", default_value=True)
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(label="Profile", tag="sim_profile")
                        dpg.add_checkbox(label="SPECULA diagram (PNG)", tag="sim_diagram")
                    dpg.add_text("(Profile / diagram: local and SSH runs only)",
                                 color=tm.get_color("text_hint"))
                    dpg.add_separator()
                    dpg.add_button(label="START SIMULATION", callback=self.start_sim,
                                   width=-1, height=35)
                    dpg.add_button(label="Advance Step", callback=self.step_sim,
                                   width=-1, height=25)
                    with dpg.group(horizontal=True):
                        dpg.add_input_int(label="Steps", tag="sim_advance_n_steps",
                                          default_value=100, min_value=1, width=140)
                        dpg.add_button(label="Advance N Steps", callback=self.step_sim_n,
                                       width=-1, height=25)
                    dpg.add_button(label="Abort Simulation", callback=self.abort_sim,
                                   width=-1, height=25)
                    dpg.add_text("Status: idle", tag="sim_status_text",
                                 color=tm.get_color("text_hint"))

                with dpg.child_window(width=-1, tag="sim_terminal_child", border=True):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Terminal Output",
                                     color=tm.get_color("control_terminal_title"))
                        dpg.add_button(label="Clear", callback=self._clear_terminal)
                        dpg.add_button(label="Save...", callback=self._save_terminal)
                    dpg.add_input_text(tag="sim_terminal", multiline=True, readonly=True,
                                       width=-1, height=-1)
        self._refresh_terminal()

    @staticmethod
    def _host_label(h: dict) -> str:
        return f"{h['user']}@{h['host']}" if h.get("user") else h.get("host", "")

    def _on_recent_host(self, sender, app_data):
        user, _, host = app_data.rpartition("@")
        dpg.set_value("sim_remote_ip", host)
        dpg.set_value("sim_remote_user", user)

    def _on_backend_mode_changed(self, sender, app_data):
        log.info("Backend mode: %s", app_data)

    def _set_status(self, text: str):
        if dpg.does_item_exist("sim_status_text"):
            dpg.set_value("sim_status_text", f"Status: {text}")

    # ── Terminal ──────────────────────────────────────────────────────────────

    def append_terminal(self, text):
        """Append output (called from any thread)."""
        with self._terminal_lock:
            self.terminal_data.append(text)
        self._refresh_terminal()

    def _refresh_terminal(self):
        if dpg.does_item_exist("sim_terminal"):
            with self._terminal_lock:
                content = "".join(self.terminal_data)
            dpg.set_value("sim_terminal", content)
            try:
                dpg.set_y_scroll("sim_terminal_child", -1.0)
            except Exception:
                pass

    def _clear_terminal(self):
        with self._terminal_lock:
            self.terminal_data.clear()
        self._refresh_terminal()

    def _save_terminal(self):
        def _write(sender, app_data):
            path = app_data.get("file_path_name")
            if not path:
                return
            with self._terminal_lock:
                content = "".join(self.terminal_data)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            log.info("Terminal output saved to %s", path)

        with dpg.file_dialog(label="Save terminal output", callback=_write, width=700,
                             height=400, default_filename="specula_run.log"):
            dpg.add_file_extension(".log")
            dpg.add_file_extension(".*")

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

    def _value(self, tag, default):
        return dpg.get_value(tag) if dpg.does_item_exist(tag) else default

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

    def start_sim(self, sender=None, app_data=None, run_all_mode=False):
        if self.is_running:
            self.append_terminal("[WARNING] Simulation is already running\n")
            return

        self._session_id = str(uuid.uuid4())
        backend_mode = self._value("sim_backend", "In-Process")
        in_process = backend_mode == "In-Process"
        remote_ip = "localhost"
        remote_user = ""
        if not in_process:
            remote_ip = (self._value("sim_remote_ip", "localhost") or "localhost").strip()
            remote_user = (self._value("sim_remote_user", "") or "").strip()
        is_ssh = not in_process and remote_ip not in _LOCAL_HOSTS
        ds_port = int(self._value("sim_ds_port", DEFAULT_DISPLAY_SERVER_PORT))

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
        )
        if overrides:
            self.append_terminal(f"[INFO] {len(overrides)} override file(s) passed to SPECULA\n")

        # ── backend ───────────────────────────────────────────────────────────
        mm = self.editor.nm.monitors
        mm.set_session_id(self._session_id)
        mm.set_server_url_file(self._server_url_file)
        resolved_ip = _resolve_remote_hostname(remote_ip) if is_ssh else remote_ip
        if in_process:
            self._backend = InProcessBackend(monitor_bus=mm._monitor_bus)
        else:
            self._backend = RemoteBackend(remote_ip=remote_ip, remote_user=remote_user)
            if is_ssh:
                self.editor.preferences.add_remote_host(remote_ip, remote_user)
        mm.set_inprocess_mode(in_process)
        mm.set_backend(self._backend)

        cmd_args = {
            "run_all_mode": run_all_mode,
            "stepping":     self._value("sim_stepping", False),
            "nsimul":       self._value("sim_nsimul", 1),
            "cpu":          self._value("sim_cpu", False),
            "target":       self._value("sim_target", 0),
            "precision":    int(self._value("sim_precision", "1")),
            "log_level":    self._value("sim_log", "INFO"),
            "profile":      self._value("sim_profile", False),
            "diagram":      self._value("sim_diagram", False),
            "resolved_ip":  resolved_ip,
            "remote_ip":    remote_ip,
        }

        sio = self.editor.nm.sio_client
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

    def step_sim(self, sender=None, app_data=None):
        if self._backend is not None:
            self._backend.step()
        else:
            self.append_terminal("[WARNING] No active backend\n")

    def step_sim_n(self, sender=None, app_data=None):
        if self._backend is None:
            self.append_terminal("[WARNING] No active backend\n")
            return
        n_steps = dpg.get_value("sim_advance_n_steps")
        if n_steps < 1:
            self.append_terminal("[WARNING] Number of steps must be at least 1\n")
            return
        self.append_terminal(f"[INFO] Advancing {n_steps} step(s)...\n")
        # SPECULA's stepping prompt accepts a number of steps to advance.
        self._backend.step(n_steps)

    def abort_sim(self, sender=None, app_data=None):
        if self._backend is not None:
            self._backend.abort()
            self.append_terminal("[INFO] Abort signal sent\n")
        self._clear_server_url_file()
        self.is_running = False
        self._set_status("aborted")
