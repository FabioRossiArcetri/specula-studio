import json
import os
import re
import threading
import time
import uuid

import dearpygui.dearpygui as dpg
import yaml

from simulation_backend import RemoteBackend, InProcessBackend, SimulationBackend

_DISPLAY_SERVER_PORT      = 5000
_DISPLAY_SERVER_NODE_NAME = "specula_studio_display_server"

_REMOTE_SERVER_INFO_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "specula_studio_remote_server.json",
)

_URL_RE = re.compile(
    r'https?://(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(\d{4,5})',
    re.IGNORECASE,
)
_PORT_KW_RE = re.compile(
    r'(?:display[_\s]?server|socket\.?io|server|running|listening|started)'
    r'.{0,80}?[:\s](\d{4,5})\b',
    re.IGNORECASE,
)


class SimulationControl:
    def __init__(self, editor):
        self.editor        = editor
        self.process       = None
        self.terminal_data = []
        self.is_running    = False
        self._reconnect_timer = None
        self._backend: SimulationBackend | None = None

        # Session ID — generated once per run, injected into the DisplayServer
        # YAML block so SPECULA re-uses it. Written to the coordination file and
        # passed as --session-id to every subprocess monitor.
        self._session_id: str = ""

        self._server_url_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "specula_studio_server.json",
        )
        self._clear_server_url_file()

    # ── Coordination file ─────────────────────────────────────────────────────

    def _clear_server_url_file(self):
        try:
            if os.path.exists(self._server_url_file):
                os.remove(self._server_url_file)
        except Exception:
            pass

    def _write_server_url_file(self, url: str):
        """Write URL + session_id atomically so monitors always see a consistent pair."""
        try:
            payload = {"url": url, "session_id": self._session_id}
            # Write to a temp file then rename so monitors never see a partial write.
            tmp = self._server_url_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._server_url_file)
            print(
                f"[SIMULATION] Wrote server URL file: {url} "
                f"(session={self._session_id})"
            )
        except Exception as e:
            print(f"[SIMULATION] Could not write server URL file: {e}")

    # ── YAML Display Window ───────────────────────────────────────────────────

    def _get_current_yaml_content(self):
        try:
            temp_path = "_temp_yaml_display.yml"
            self.editor.fh.export_simulation(temp_path, include_defaults=False)
            with open(temp_path, encoding="utf-8") as f:
                yaml_content = f.read()
            try:
                os.remove(temp_path)
            except Exception:
                pass
            return yaml_content
        except Exception as e:
            return f"Error generating YAML:\n{str(e)}"

    def show_yaml_window(self):
        yaml_content = self._get_current_yaml_content()
        window_tag   = f"yaml_display_window_{int(time.time() * 1000)}"
        with dpg.window(label="Simulation YAML", tag=window_tag, width=800, height=600):
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Copy to Clipboard", width=120,
                    callback=lambda: self._copy_yaml_to_clipboard(yaml_content),
                )
                dpg.add_button(
                    label="Close", width=80,
                    callback=lambda: dpg.delete_item(window_tag),
                )
                dpg.add_spacer()
            dpg.add_separator()
            dpg.add_input_text(
                tag=f"yaml_content_{window_tag}",
                default_value=yaml_content,
                multiline=True, readonly=True, width=-1, height=-1,
            )

    def _copy_yaml_to_clipboard(self, content):
        try:
            import subprocess
            if os.name == "nt":
                process = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
            elif os.uname().sysname == "Darwin":
                process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            else:
                try:
                    process = subprocess.Popen(
                        ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE
                    )
                except FileNotFoundError:
                    process = subprocess.Popen(["xsel", "-b", "-i"], stdin=subprocess.PIPE)
            process.communicate(content.encode("utf-8"))
        except Exception as e:
            print(f"[SIMULATION] Failed to copy to clipboard: {e}")

    # ── Control window ────────────────────────────────────────────────────────

    def _get_sim_path(self):
        name = getattr(self.editor, "current_scene_name", "untitled") or "untitled"
        return f"{name}_simul.yml"

    def show_control_window(self):
        if dpg.does_item_exist("sim_control_window"):
            dpg.show_item("sim_control_window")
            dpg.focus_item("sim_control_window")
            return

        with dpg.window(
            label="Simulation Control Panel",
            tag="sim_control_window",
            width=900, height=600,
        ):
            with dpg.group(horizontal=True):
                with dpg.child_window(width=500):
                    dpg.add_text("Backend Mode", color=[255, 200, 100])
                    dpg.add_combo(
                        label="Execution Mode",
                        items=["Remote", "In-Process"],
                        tag="sim_backend",
                        default_value="In-Process",
                        callback=self._on_backend_mode_changed,
                    )
                    dpg.add_text(
                        "Remote Server Configuration",
                        color=[100, 200, 255],
                        tag="sim_remote_settings_label",
                    )
                    dpg.add_input_text(
                        label="Server IP / Hostname", tag="sim_remote_ip",
                        default_value="localhost",
                        hint="localhost, 127.0.0.1, or remote host IP", width=-1,
                    )
                    dpg.add_input_text(
                        label="SSH Username", tag="sim_remote_user",
                        default_value="", hint="Leave empty to use current user", width=-1,
                    )
                    dpg.add_separator()
                    dpg.add_text("Simulation Arguments", color=[100, 200, 255])
                    dpg.add_input_int(label="N-Simul", tag="sim_nsimul", default_value=1, min_value=1)
                    dpg.add_checkbox(label="Use CPU", tag="sim_cpu")
                    dpg.add_input_int(label="GPU ID", tag="sim_target", default_value=0)
                    dpg.add_combo(
                        label="Precision", items=["0", "1"],
                        tag="sim_precision", default_value="1",
                    )
                    dpg.add_combo(
                        label="Log Level", items=["DEBUG", "INFO", "WARNING"],
                        tag="sim_log", default_value="INFO",
                    )
                    dpg.add_checkbox(label="Stepping Mode", tag="sim_stepping", default_value=True)
                    dpg.add_separator()
                    dpg.add_button(
                        label="START SIMULATION", callback=self.start_sim,
                        width=-1, height=35,
                    )
                    dpg.add_button(
                        label="Advance Step", callback=self.step_sim,
                        width=-1, height=25,
                    )
                    with dpg.group(horizontal=True):
                        dpg.add_input_int(
                            label="Steps", tag="sim_advance_n_steps",
                            default_value=100, min_value=1, width=140,
                        )
                        dpg.add_button(
                            label="Advance N Steps", callback=self.step_sim_n,
                            width=-1, height=25,
                        )
                    dpg.add_button(
                        label="Abort Simulation", callback=self.abort_sim,
                        width=-1, height=25,
                    )

                with dpg.child_window(width=-1, tag="sim_terminal_child", border=True):
                    dpg.add_text("Terminal Output", color=[150, 150, 150])
                    dpg.add_input_text(
                        tag="sim_terminal", multiline=True,
                        readonly=True, width=-1, height=-1,
                    )

    def _on_backend_mode_changed(self, sender, app_data):
        print(f"[SIMULATION] Backend mode changed to: {app_data}")

    def append_terminal(self, text):
        self.terminal_data.append(text)
        if len(self.terminal_data) > 1000:
            self.terminal_data.pop(0)
        if dpg.does_item_exist("sim_terminal"):
            dpg.set_value("sim_terminal", "".join(self.terminal_data))
            try:
                dpg.set_y_scroll("sim_terminal_child", -1.0)
            except Exception:
                pass

    # ── YAML preparation ──────────────────────────────────────────────────────

    def _strip_studio_fields(self, yaml_data: dict) -> dict:
        for key in [k for k in yaml_data if k.startswith("_")]:
            del yaml_data[key]
        for node_dict in yaml_data.values():
            if isinstance(node_dict, dict) and "gui_pos" in node_dict:
                del node_dict["gui_pos"]
        return yaml_data

    def _strip_gui_fields(self, yaml_data: dict) -> dict:
        return self._strip_studio_fields(yaml_data)

    def _inject_display_server_node(self, yaml_data: dict) -> bool:
        """
        Inject (or update) a DisplayServer node.

        The ``session_id`` generated by this SimulationControl instance is
        written into the DisplayServer block so that SPECULA's DisplayServer
        uses the exact same UUID that was written to the coordination file and
        passed as --session-id to every monitor subprocess.
        """
        # ── Update existing DisplayServer block with our session_id ────────────
        for node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict) and node_dict.get("class") == "DisplayServer":
                node_dict["session_id"] = self._session_id
                print(
                    f"[SIMULATION] DisplayServer '{node_name}' already present — "
                    f"injected session_id={self._session_id}"
                )
                return True

        # ── Find SimulParams ───────────────────────────────────────────────────
        simul_params_name = None
        for node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict) and node_dict.get("class") == "SimulParams":
                simul_params_name = node_name
                if node_dict.get("display_server") is True:
                    del node_dict["display_server"]
                break

        if simul_params_name is None:
            print("[SIMULATION] Warning: No SimulParams block found — cannot inject DisplayServer.")
            return False

        ds_name = _DISPLAY_SERVER_NODE_NAME
        suffix  = 1
        while ds_name in yaml_data:
            ds_name = f"{_DISPLAY_SERVER_NODE_NAME}_{suffix}"
            suffix += 1

        yaml_data[ds_name] = {
            "class":      "DisplayServer",
            "port":       _DISPLAY_SERVER_PORT,
            "mode":       "data",
            "session_id": self._session_id,   # ← key fix: single source of truth
        }
        print(
            f"[SIMULATION] Injected DisplayServer '{ds_name}' "
            f"(port={_DISPLAY_SERVER_PORT}, session_id={self._session_id})"
        )
        return True

    def _prepare_simulation_yaml(self, file_path: str, inject_display_server: bool = True):
        try:
            with open(file_path, encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)
            if not isinstance(yaml_data, dict):
                return
            yaml_data = self._strip_studio_fields(yaml_data)
            if inject_display_server:
                self._inject_display_server_node(yaml_data)
            with open(file_path, "w", encoding="utf-8") as f:
                yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False)
            print(f"[SIMULATION] Prepared simulation YAML: {file_path}")
        except Exception as e:
            print(f"[SIMULATION] Warning: could not prepare YAML: {e}")

    def _clean_simulation_yaml(self, file_path):
        self._prepare_simulation_yaml(file_path)

    # ── Port / URL detection ──────────────────────────────────────────────────

    def _try_extract_port(self, line: str):
        for pattern in (_URL_RE, _PORT_KW_RE):
            m = pattern.search(line)
            if m:
                port = int(m.group(1))
                if 1024 <= port <= 65535:
                    return port
        return None

    def _on_display_server_port_found(self, port: int, remote_ip: str = "localhost"):
        if remote_ip in ("localhost", "127.0.0.1", ""):
            new_url = f"http://127.0.0.1:{port}"
        else:
            new_url = f"http://{remote_ip}:{port}"

        print(f"[SIMULATION] Display server confirmed at {new_url}")
        self.append_terminal(f"[INFO] Display server running at {new_url}\n")

        sio = self.editor.nm.sio_client
        sio.server_url = new_url
        self._write_server_url_file(new_url)

        mm = self.editor.nm.monitors
        if hasattr(mm, "on_display_server_ready"):
            mm.on_display_server_ready(new_url)

        if not sio.connected:
            threading.Thread(target=sio.reconnect, daemon=True).start()

    # ── Backend-finished callback ─────────────────────────────────────────────

    def _on_backend_finished(self):
        self.is_running = False
        self.process    = None
        self._clear_server_url_file()
        mm = self.editor.nm.monitors
        if hasattr(mm, "set_backend"):
            mm.set_backend(None)

    # ── Simulation launch / control ───────────────────────────────────────────

    def _schedule_display_server_reconnect(
        self, delay: float = 4.0, expected_url: str = None
    ):
        if expected_url is None:
            expected_url = f"http://127.0.0.1:{_DISPLAY_SERVER_PORT}"

        def _attempt(attempt_no, delay_s):
            time.sleep(delay_s)
            sio = self.editor.nm.sio_client
            if sio is None or sio.connected:
                return
            print(f"[SIMULATION] Fallback reconnect attempt {attempt_no} → {expected_url}")
            sio.server_url = expected_url
            sio.reconnect()
            if not sio.connected and attempt_no == 1:
                threading.Thread(target=_attempt, args=(2, 6.0), daemon=True).start()

        threading.Thread(target=_attempt, args=(1, delay), daemon=True).start()

    def start_sim(self, sender=None, app_data=None, run_all_mode=False):
        if self.is_running:
            self.append_terminal("[WARNING] Simulation is already running\n")
            return

        # ── Generate session ID first — everything else derives from it ────────
        self._session_id = str(uuid.uuid4())
        print(f"[SIMULATION] New run session_id={self._session_id}")
        self._clear_server_url_file()

        backend_mode         = (
            dpg.get_value("sim_backend")
            if dpg.does_item_exist("sim_backend") else "Remote"
        )
        use_inprocess_direct = (backend_mode == "In-Process")
        is_remote            = (backend_mode == "Remote")

        temp_path = self._get_sim_path()
        self.editor.fh.export_simulation(temp_path, include_defaults=True)
        # _inject_display_server_node now also stamps session_id into the block
        self._prepare_simulation_yaml(
            temp_path,
            inject_display_server=not use_inprocess_direct,
        )

        mm = self.editor.nm.monitors

        if hasattr(mm, "set_session_id"):
            mm.set_session_id(self._session_id)

        remote_ip   = "localhost"
        remote_user = ""

        if is_remote:
            remote_ip = (
                dpg.get_value("sim_remote_ip")
                if dpg.does_item_exist("sim_remote_ip") else "localhost"
            )
            remote_user = (
                dpg.get_value("sim_remote_user")
                if dpg.does_item_exist("sim_remote_user") else ""
            )

        if is_remote and remote_ip not in ("localhost", "127.0.0.1", ""):
            from simulation_backend import _resolve_remote_hostname
            remote_ip_for_monitors = _resolve_remote_hostname(remote_ip)
        else:
            remote_ip_for_monitors = remote_ip

        if use_inprocess_direct:
            monitor_bus   = mm._monitor_bus
            self._backend = InProcessBackend(monitor_bus=monitor_bus)
            mm.set_inprocess_mode(True)
        else:
            self._backend = RemoteBackend(remote_ip=remote_ip, remote_user=remote_user)
            mm.set_inprocess_mode(False)

        mm.set_backend(self._backend)

        cmd_args = {
            "run_all_mode": run_all_mode,
            "stepping":     dpg.get_value("sim_stepping")  if dpg.does_item_exist("sim_stepping")  else False,
            "nsimul":       dpg.get_value("sim_nsimul")    if dpg.does_item_exist("sim_nsimul")    else 1,
            "cpu":          dpg.get_value("sim_cpu")       if dpg.does_item_exist("sim_cpu")       else False,
            "target":       dpg.get_value("sim_target")    if dpg.does_item_exist("sim_target")    else -1,
            "precision":    int(dpg.get_value("sim_precision") if dpg.does_item_exist("sim_precision") else "1"),
            "log_level":    dpg.get_value("sim_log")       if dpg.does_item_exist("sim_log")       else "INFO",
            "resolved_ip":  remote_ip_for_monitors,
            "remote_ip":    remote_ip,
        }

        if hasattr(self.editor, "override_manager"):
            enabled = self.editor.override_manager.get_enabled_overrides()
            if enabled:
                cmd_args["overrides"] = self.editor.override_manager.get_override_string()
                self.append_terminal(f"[INFO] Applied {len(enabled)} override file(s)\n")

        if remote_ip in ("localhost", "127.0.0.1", ""):
            expected_url = f"http://127.0.0.1:{_DISPLAY_SERVER_PORT}"
        else:
            expected_url = f"http://{remote_ip_for_monitors}:{_DISPLAY_SERVER_PORT}"

        sio = self.editor.nm.sio_client
        if sio is not None and sio.connected:
            try:
                sio.disconnect()
            except Exception:
                pass
        if sio is not None:
            sio.server_url = expected_url

        self._backend.start(
            yaml_path=temp_path,
            cmd_args=cmd_args,
            append_terminal=self.append_terminal,
            on_port_found=lambda port, ip: self._on_display_server_port_found(port, ip),
            on_finished=self._on_backend_finished,
        )

        self.is_running = True

        if not use_inprocess_direct:
            self._write_server_url_file(expected_url)
            if hasattr(mm, "on_display_server_ready"):
                mm.on_display_server_ready(expected_url)
            self._schedule_display_server_reconnect(delay=4.0, expected_url=expected_url)

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
        for i in range(n_steps):
            self._backend.step()
            if i < n_steps - 1:
                time.sleep(0.05)

    def abort_sim(self, sender=None, app_data=None):
        if self._backend is not None:
            self._backend.abort()
            self.append_terminal("[INFO] Abort signal sent\n")
        self._clear_server_url_file()
        self.is_running = False
