import json
import os
import re
import threading
import time

import dearpygui.dearpygui as dpg
import yaml

from simulation_backend import RemoteBackend, InProcessBackend, SimulationBackend

# ---------------------------------------------------------------------------
# Fixed port for the injected DisplayServer node.
# Must match SOCKETIO_SERVER in constants.py.
# ---------------------------------------------------------------------------
_DISPLAY_SERVER_PORT = 5000
_DISPLAY_SERVER_NODE_NAME = "specula_studio_display_server"

_REMOTE_SERVER_INFO_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "specula_studio_remote_server.json",
)

# ---------------------------------------------------------------------------
# Patterns used to extract the display-server URL from specula stdout
# ---------------------------------------------------------------------------
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
        self.editor = editor
        self.process = None           # kept for backward-compat; None in backend mode
        self.terminal_data = []
        self.is_running = False
        self._reconnect_timer = None

        # Active simulation backend (set in start_sim, cleared in _on_backend_finished).
        self._backend: SimulationBackend | None = None

        # Path to the coordination file shared with monitor subprocesses.
        self._server_url_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "specula_studio_server.json",
        )
        self._clear_server_url_file()

    # ------------------------------------------------------------------
    # Coordination file helpers
    # ------------------------------------------------------------------

    def _clear_server_url_file(self):
        try:
            if os.path.exists(self._server_url_file):
                os.remove(self._server_url_file)
        except Exception:
            pass

    def _write_server_url_file(self, url: str):
        try:
            with open(self._server_url_file, "w", encoding="utf-8") as f:
                json.dump({"url": url}, f)
            print(f"[SIMULATION] Wrote server URL file: {self._server_url_file} → {url}")
        except Exception as e:
            print(f"[SIMULATION] Could not write server URL file: {e}")

    # ------------------------------------------------------------------
    # YAML Display Window
    # ------------------------------------------------------------------

    def _get_current_yaml_content(self):
        """Generate the current simulation YAML as a string."""
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
            print(f"[SIMULATION] Error generating YAML: {e}")
            return f"Error generating YAML:\n{str(e)}"

    def show_yaml_window(self):
        """Display current simulation YAML in a detached window."""
        yaml_content = self._get_current_yaml_content()

        import time
        window_tag = f"yaml_display_window_{int(time.time() * 1000)}"

        with dpg.window(
            label="Simulation YAML",
            tag=window_tag,
            width=800,
            height=600,
            no_close=False,
        ):
            with dpg.group(horizontal=True):
                dpg.add_button(label="Copy to Clipboard", width=120,
                              callback=lambda: self._copy_yaml_to_clipboard(yaml_content))
                dpg.add_button(label="Close", width=80,
                              callback=lambda: dpg.delete_item(window_tag))
                dpg.add_spacer()

            dpg.add_separator()

            dpg.add_input_text(
                tag=f"yaml_content_{window_tag}",
                default_value=yaml_content,
                multiline=True,
                readonly=True,
                width=-1,
                height=-1,
            )

    def _copy_yaml_to_clipboard(self, content):
        """Copy YAML content to system clipboard."""
        try:
            import subprocess
            if os.name == 'nt':  # Windows
                process = subprocess.Popen(['clip'], stdin=subprocess.PIPE)
                process.communicate(content.encode('utf-8'))
            elif os.uname().sysname == 'Darwin':  # macOS
                process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
                process.communicate(content.encode('utf-8'))
            else:  # Linux
                try:
                    process = subprocess.Popen(['xclip', '-selection', 'clipboard'],
                                             stdin=subprocess.PIPE)
                    process.communicate(content.encode('utf-8'))
                except FileNotFoundError:
                    print("[SIMULATION] xclip not found, trying xsel...")
                    process = subprocess.Popen(['xsel', '-b', '-i'],
                                             stdin=subprocess.PIPE)
                    process.communicate(content.encode('utf-8'))
            print("[SIMULATION] YAML copied to clipboard")
        except Exception as e:
            print(f"[SIMULATION] Failed to copy to clipboard: {e}")

    # ------------------------------------------------------------------
    # Control window
    # ------------------------------------------------------------------

    def _get_sim_path(self):
        name = getattr(self.editor, "current_scene_name", "untitled")
        if name is None:
            name = "untitled"
        return f"{name}_simul.yml"

    def show_control_window(self):
        if dpg.does_item_exist("sim_control_window"):
            dpg.show_item("sim_control_window")
            dpg.focus_item("sim_control_window")
            return

        with dpg.window(label="Simulation Control Panel", tag="sim_control_window", width=850, height=600):
            with dpg.group(horizontal=True):
                with dpg.child_window(width=400):
                    # ── Backend Mode Selection ──────────────────────────────────
                    dpg.add_text("Backend Mode", color=[255, 200, 100])
                    dpg.add_combo(
                        label="Execution Mode",
                        items=["Remote", "In-Process"],
                        tag="sim_backend",
                        default_value="Remote",
                        callback=self._on_backend_mode_changed,
                    )
                    
                    # ── Remote Server Settings (shown when Remote is selected) ────
                    dpg.add_text("Remote Server Configuration", color=[100, 200, 255], tag="sim_remote_settings_label")
                    dpg.add_input_text(
                        label="Server IP / Hostname",
                        tag="sim_remote_ip",
                        default_value="localhost",
                        hint="localhost, 127.0.0.1, or remote host IP",
                        width=-1,
                    )
                    dpg.add_input_text(
                        label="SSH Username",
                        tag="sim_remote_user",
                        default_value="",
                        hint="Leave empty to use current user",
                        width=-1,
                    )
                    dpg.add_text(
                        "For remote servers, YAML is copied via scp and\n"
                        "simulation executed via ssh. DisplayServer\n"
                        "runs on remote and is accessible locally.",
                        color=[150, 150, 150],
                        wrap=380,
                    )
                    
                    dpg.add_separator()
                    
                    # ── Simulation Arguments ────────────────────────────────────
                    dpg.add_text("Simulation Arguments", color=[100, 200, 255])
                    dpg.add_input_int(label="N-Simul", tag="sim_nsimul", default_value=1, min_value=1)
                    dpg.add_checkbox(label="Use CPU", tag="sim_cpu")
                    dpg.add_input_int(label="GPU ID", tag="sim_target", default_value=0)
                    dpg.add_combo(
                        label="Precision",
                        items=["0", "1"],
                        tag="sim_precision",
                        default_value="1"
                    )
                    dpg.add_combo(
                        label="Log Level",
                        items=["DEBUG", "INFO", "WARNING"],
                        tag="sim_log",
                        default_value="INFO"
                    )
                    dpg.add_checkbox(label="Stepping Mode", tag="sim_stepping", default_value=True)

                    dpg.add_separator()

                    # ── Control Buttons ─────────────────────────────────────────
                    dpg.add_button(
                        label="START SIMULATION",
                        callback=self.start_sim,
                        width=-1,
                        height=35
                    )
                    dpg.add_button(
                        label="Advance Step",
                        callback=self.step_sim,
                        width=-1,
                        height=25
                    )
                    dpg.add_button(
                        label="Abort Simulation",
                        callback=self.abort_sim,
                        width=-1,
                        height=25
                    )

                # ── Terminal Output ─────────────────────────────────────────────
                with dpg.child_window(width=-1, tag="sim_terminal_child", border=True):
                    dpg.add_text("Terminal Output", color=[150, 150, 150])
                    dpg.add_input_text(
                        tag="sim_terminal",
                        multiline=True,
                        readonly=True,
                        width=-1,
                        height=-1,
                    )

    def _on_backend_mode_changed(self, sender, app_data):
        """Handle backend mode change and update UI visibility."""
        mode = app_data
        # When the mode changes, show/hide remote settings accordingly
        # Note: In a full implementation, you might show/hide the remote server fields
        is_remote = (mode == "Remote")
        
        # Log the change
        print(f"[SIMULATION] Backend mode changed to: {mode}")
        
        # Update visibility of remote server settings
        if dpg.does_item_exist("sim_remote_settings_label"):
            # You can conditionally show/hide based on mode
            # For now, we always show them but they're only relevant in Remote mode
            pass

    def append_terminal(self, text):
        """Append text to the terminal output display."""
        self.terminal_data.append(text)
        if len(self.terminal_data) > 1000:
            self.terminal_data.pop(0)
        if dpg.does_item_exist("sim_terminal"):
            dpg.set_value("sim_terminal", "".join(self.terminal_data))
            try:
                dpg.set_y_scroll("sim_terminal_child", -1.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # YAML preparation
    # ------------------------------------------------------------------

    def _strip_studio_fields(self, yaml_data: dict) -> dict:
        """
        Remove all specula-studio-private fields from the YAML dict so that
        the resulting file is clean for SPECULA.

        Fields removed:
          - ``gui_pos``           — node positions (present on every node dict)
          - any top-level key starting with ``_``  — e.g. ``_overrides_metadata``
        """
        # Remove top-level studio-private keys (e.g. _overrides_metadata)
        private_top_level = [k for k in yaml_data if k.startswith('_')]
        for key in private_top_level:
            del yaml_data[key]
            print(f"[SIMULATION] Stripped private key '{key}' from YAML")

        # Remove per-node gui_pos entries
        for _node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict) and "gui_pos" in node_dict:
                del node_dict["gui_pos"]

        return yaml_data

    # kept for backward-compat
    def _strip_gui_fields(self, yaml_data: dict) -> dict:
        return self._strip_studio_fields(yaml_data)

    def _inject_display_server_node(self, yaml_data: dict) -> bool:
        """
        Inject a DisplayServer node into the simulation YAML so that specula
        always starts its built-in Socket.IO server on a fixed port when
        launched from the GUI.

        Returns True if the block was added or already present, False if no
        SimulParams node was found (DisplayServer cannot be wired).
        """
        # ── Check whether a DisplayServer already exists ──────────────────
        for node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict) and node_dict.get("class") == "DisplayServer":
                print(
                    f"[SIMULATION] DisplayServer node '{node_name}' already present "
                    f"— skipping injection"
                )
                return True

        # ── Find the SimulParams node name ────────────────────────────────
        simul_params_name = None
        for node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict) and node_dict.get("class") == "SimulParams":
                simul_params_name = node_name
                if node_dict.get("display_server") is True:
                    del node_dict["display_server"]
                    print(
                        f"[SIMULATION] Removed legacy 'display_server: true' "
                        f"from SimulParams '{node_name}'"
                    )
                break

        if simul_params_name is None:
            print(
                "[SIMULATION] Warning: No SimulParams block found — "
                "cannot inject DisplayServer node."
            )
            return False

        ds_block = {
            "class":  "DisplayServer",
            "port":   _DISPLAY_SERVER_PORT,
            "mode":   "data",
        }

        ds_name = _DISPLAY_SERVER_NODE_NAME
        suffix  = 1
        while ds_name in yaml_data:
            ds_name = f"{_DISPLAY_SERVER_NODE_NAME}_{suffix}"
            suffix += 1

        yaml_data[ds_name] = ds_block
        print(
            f"[SIMULATION] Injected DisplayServer node '{ds_name}' "
            f"(port={_DISPLAY_SERVER_PORT}, mode=data)"
        )
        return True

    def _prepare_simulation_yaml(self, file_path: str, inject_display_server: bool = True):
        """
        Post-process the exported YAML before handing it to specula:
          1. Strip all specula-studio-private fields (gui_pos, _overrides_metadata, …).
          2. Optionally inject a DisplayServer node (skipped in direct in-process mode).
        """
        try:
            with open(file_path, encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)

            if not isinstance(yaml_data, dict):
                print("[SIMULATION] Warning: YAML root is not a dict, skipping preparation")
                return

            # Strip GUI-only and studio-private fields BEFORE any other processing
            yaml_data = self._strip_studio_fields(yaml_data)

            if inject_display_server:
                self._inject_display_server_node(yaml_data)

            with open(file_path, "w", encoding="utf-8") as f:
                yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False)
            print(f"[SIMULATION] Prepared simulation YAML: {file_path}")
        except Exception as e:
            print(f"[SIMULATION] Warning: could not prepare YAML: {e}")

    # kept for backward-compat
    def _clean_simulation_yaml(self, file_path):
        self._prepare_simulation_yaml(file_path)

    # ------------------------------------------------------------------
    # Port / URL detection (confirmation mechanism)
    # ------------------------------------------------------------------

    def _try_extract_port(self, line: str):
        for pattern in (_URL_RE, _PORT_KW_RE):
            m = pattern.search(line)
            if m:
                port = int(m.group(1))
                if 1024 <= port <= 65535:
                    return port
        return None

    def _on_display_server_port_found(self, port: int, remote_ip: str = None):
        """
        Called when the DisplayServer port is detected on local or remote execution.
        
        Parameters
        ----------
        port : int
            The port number the DisplayServer is listening on.
        remote_ip : str or None
            For remote execution, the hostname/IP of the remote server.
            For local execution, None.
        """
        # Construct the correct URL based on whether execution is local or remote
        if remote_ip is None:
            # Local execution: use 127.0.0.1
            new_url = f"http://127.0.0.1:{port}"
        elif remote_ip in ("localhost", "127.0.0.1", ""):
            # Localhost passed as remote_ip: use 127.0.0.1
            new_url = f"http://127.0.0.1:{port}"
        else:
            # True remote execution: use the remote hostname/IP
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

    # ------------------------------------------------------------------
    # Backend-finished callback
    # ------------------------------------------------------------------

    def _on_backend_finished(self):
        self.is_running = False
        self.process = None
        self._clear_server_url_file()
        # Clear the backend reference from MonitorManager so probe calls are
        # no-ops after the simulation has stopped.
        mm = self.editor.nm.monitors
        if hasattr(mm, "set_backend"):
            mm.set_backend(None)

    # ------------------------------------------------------------------
    # Simulation launch / control
    # ------------------------------------------------------------------
    def _schedule_display_server_reconnect(self, delay: float = 4.0, expected_url: str = None):
        """
        Fallback reconnect attempt in case the port never appears in stdout.
        With a fixed port this should rarely be needed.
        
        Parameters
        ----------
        delay : float
            Initial delay before first reconnect attempt (seconds)
        expected_url : str
            The URL to connect to (e.g., http://gandalf:5000)
        """
        if expected_url is None:
            expected_url = f"http://127.0.0.1:{_DISPLAY_SERVER_PORT}"
        
        def _attempt(attempt_no, delay_s):
            time.sleep(delay_s)
            sio = self.editor.nm.sio_client
            if sio is None:
                return
            if sio.connected:
                print(f"[SIMULATION] Already connected to {sio.server_url}")
                return
            
            print(f"[SIMULATION] Fallback reconnect attempt {attempt_no} → {expected_url}")
            # CRITICAL: Update the client's URL before reconnecting
            sio.server_url = expected_url
            sio.reconnect()
            
            if not sio.connected and attempt_no == 1:
                # Schedule a second attempt
                threading.Thread(target=_attempt, args=(2, 6.0), daemon=True).start()

        threading.Thread(target=_attempt, args=(1, delay), daemon=True).start()

    def start_sim(self, sender=None, app_data=None, run_all_mode=False):
        """Start the simulation with the current configuration."""
        if self.is_running:
            self.append_terminal("[WARNING] Simulation is already running\n")
            return

        self._clear_server_url_file()

        # ── Determine backend mode ──────────────────────────────────────────
        backend_mode = (
            dpg.get_value("sim_backend")
            if dpg.does_item_exist("sim_backend")
            else "Remote"
        )
        use_inprocess_direct = (backend_mode == "In-Process")
        is_remote = (backend_mode == "Remote")

        # ── Export and prepare simulation YAML ───────────────────────────────
        temp_path = self._get_sim_path()
        self.editor.fh.export_simulation(temp_path, include_defaults=True)
        # Strip studio-private fields; inject DisplayServer for Remote mode (not for In-Process)
        self._prepare_simulation_yaml(
            temp_path,
            inject_display_server=not use_inprocess_direct,
        )

        mm = self.editor.nm.monitors

        # ── Get remote server info ────────────���─────────────────────────────
        remote_ip = "localhost"
        remote_user = ""
        
        if is_remote:
            remote_ip = (
                dpg.get_value("sim_remote_ip")
                if dpg.does_item_exist("sim_remote_ip")
                else "localhost"
            )
            remote_user = (
                dpg.get_value("sim_remote_user")
                if dpg.does_item_exist("sim_remote_user")
                else ""
            )

        # ── Create appropriate backend ──────────────────────────────────────
        if use_inprocess_direct:
            # Pass the MonitorBus so InProcessBackend uses the probe-based path.
            monitor_bus = mm._monitor_bus
            self._backend = InProcessBackend(monitor_bus=monitor_bus)
            mm.set_inprocess_mode(True)
        else:
            # Remote backend (handles both localhost and remote execution)
            self._backend = RemoteBackend(remote_ip=remote_ip, remote_user=remote_user)
            mm.set_inprocess_mode(False)

        # Give MonitorManager a reference to the backend for dynamic probe
        # attach / detach when monitor windows are opened or closed.
        mm.set_backend(self._backend)

        # ── Prepare command arguments ───────────────────────────────────────
        cmd_args = {
            "run_all_mode": run_all_mode,
            "stepping":  dpg.get_value("sim_stepping")  if dpg.does_item_exist("sim_stepping") else False,
            "nsimul":    dpg.get_value("sim_nsimul")    if dpg.does_item_exist("sim_nsimul")   else 1,
            "cpu":       dpg.get_value("sim_cpu")       if dpg.does_item_exist("sim_cpu")      else False,
            "target":    dpg.get_value("sim_target")    if dpg.does_item_exist("sim_target")   else -1,
            "precision": int(dpg.get_value("sim_precision") if dpg.does_item_exist("sim_precision") else "1"),
            "log_level": dpg.get_value("sim_log")       if dpg.does_item_exist("sim_log")      else "INFO",
        }

        # ── Log startup info ────────────────────────────────────────────────
        if use_inprocess_direct:
            self.append_terminal(
                f"[INFO] Backend: {backend_mode} (direct probe monitoring — no DisplayServer)\n"
            )
        elif is_remote:
            if remote_ip in ("localhost", "127.0.0.1", ""):
                self.append_terminal(
                    f"[INFO] Backend: Remote (localhost)\n"
                    f"[INFO] DisplayServer will start on port {_DISPLAY_SERVER_PORT} …\n"
                )
            else:
                user_str = f"{remote_user}@" if remote_user else ""
                self.append_terminal(
                    f"[INFO] Backend: Remote ({user_str}{remote_ip})\n"
                    f"[INFO] Transferring YAML via scp, executing via ssh…\n"
                    f"[INFO] DisplayServer will be accessible at http://{remote_ip}:{_DISPLAY_SERVER_PORT}\n"
                )

        # ── Add enabled overrides ───────────────────────────────────────────
        if hasattr(self.editor, 'override_manager'):
            enabled_overrides = self.editor.override_manager.get_enabled_overrides()
            if enabled_overrides:
                cmd_args['overrides'] = self.editor.override_manager.get_override_string()
                self.append_terminal(
                    f"[INFO] Applied {len(enabled_overrides)} override file(s)\n"
                )

        # ── CRITICAL: Disconnect old SocketIOClient connection and prepare for new one
        sio = self.editor.nm.sio_client
        if sio is not None and sio.connected:
            print(f"[SIMULATION] Disconnecting from old server at {sio.server_url}")
            try:
                sio.disconnect()
            except Exception as e:
                print(f"[SIMULATION] Error disconnecting: {e}")
        
        # ── Start the backend ───────────────────────────────────────────────
        self._backend.start(
            yaml_path=temp_path,
            cmd_args=cmd_args,
            append_terminal=self.append_terminal,
            on_port_found=self._on_display_server_port_found,
            on_finished=self._on_backend_finished,
        )

        self.is_running = True

        # ── Setup monitor reconnection for non-in-process backends ──────────
        if not use_inprocess_direct:
            # For remote execution, we need to determine the correct URL
            # This will be updated when the port is discovered
            if remote_ip in ("localhost", "127.0.0.1", ""):
                expected_url = f"http://127.0.0.1:{_DISPLAY_SERVER_PORT}"
            else:
                expected_url = f"http://{remote_ip}:{_DISPLAY_SERVER_PORT}"
            
            # Write the expected URL file for monitor subprocesses
            self._write_server_url_file(expected_url)
            
            # Notify the monitor manager of the expected URL
            if hasattr(mm, "on_display_server_ready"):
                mm.on_display_server_ready(expected_url)
            
            # Schedule reconnection attempts with the expected URL
            self._schedule_display_server_reconnect(delay=4.0, expected_url=expected_url)
                
    def step_sim(self, sender=None, app_data=None):
        """Advance one simulation step in stepping mode."""
        if self._backend is not None:
            self._backend.step()
        else:
            self.append_terminal("[WARNING] No active backend\n")

    def abort_sim(self, sender=None, app_data=None):
        """Abort the running simulation."""
        if self._backend is not None:
            self._backend.abort()
            self.append_terminal("[INFO] Abort signal sent\n")
        self._clear_server_url_file()
        self.is_running = False