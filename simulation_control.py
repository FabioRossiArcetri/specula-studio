import subprocess
import threading
import os
import re
import json
import time
import yaml
import dearpygui.dearpygui as dpg

from constants import SOCKETIO_SERVER

# ---------------------------------------------------------------------------
# Fixed port for the injected DisplayServer node.
# Must match SOCKETIO_SERVER in constants.py.
# ---------------------------------------------------------------------------
_DISPLAY_SERVER_PORT = 5000
_DISPLAY_SERVER_NODE_NAME = "specula_studio_display_server"

# ---------------------------------------------------------------------------
# Patterns used to extract the display-server URL from specula stdout
# (kept as a confirmation mechanism even though the port is now fixed)
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
        self.process = None
        self.terminal_data = []
        self.is_running = False
        self._reconnect_timer = None

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
            # Create a temporary export to get the YAML content
            temp_path = "_temp_yaml_display.yml"
            self.editor.fh.export_simulation(temp_path, include_defaults=False)
            
            with open(temp_path, "r", encoding="utf-8") as f:
                yaml_content = f.read()
            
            # Clean up temporary file
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
        
        # Use a tag that includes a timestamp to allow multiple instances
        import time
        window_tag = f"yaml_display_window_{int(time.time() * 1000)}"
        
        with dpg.window(
            label="Simulation YAML",
            tag=window_tag,
            width=800,
            height=600,
            no_close=False,
        ):
            # Toolbar
            with dpg.group(horizontal=True):
                dpg.add_button(label="Copy to Clipboard", width=120,
                              callback=lambda: self._copy_yaml_to_clipboard(yaml_content))
                dpg.add_button(label="Close", width=80,
                              callback=lambda: dpg.delete_item(window_tag))
                dpg.add_spacer()
            
            dpg.add_separator()
            
            # Content display
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
            # Use system clipboard
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

        with dpg.window(label="Simulation Control Panel", tag="sim_control_window", width=700, height=500):
            with dpg.group(horizontal=True):
                with dpg.child_window(width=250):
                    dpg.add_text("Arguments", color=[100, 200, 255])
                    dpg.add_input_int(label="N-Simul", tag="sim_nsimul", default_value=1)
                    dpg.add_checkbox(label="Use CPU", tag="sim_cpu")
                    dpg.add_input_int(label="GPU ID", tag="sim_target", default_value=-1)
                    dpg.add_combo(label="Precision", items=["0", "1"], tag="sim_precision", default_value="1")
                    dpg.add_combo(label="Log", items=["DEBUG", "INFO", "WARNING"], tag="sim_log", default_value="INFO")
                    dpg.add_checkbox(label="Stepping", tag="sim_stepping", default_value=True)

                    dpg.add_spacer(height=10)
                    dpg.add_button(label="START SIMULATION", callback=self.start_sim, width=-1, height=30)
                    dpg.add_button(label="Advance Step", callback=self.step_sim, width=-1)
                    dpg.add_button(label="Abort", callback=self.abort_sim, width=-1)

                with dpg.child_window(width=-1, tag="sim_terminal_child", border=True):
                    dpg.add_text("Terminal Output", color=[150, 150, 150])
                    dpg.add_input_text(
                        tag="sim_terminal",
                        multiline=True,
                        readonly=True,
                        width=-1,
                        height=-1,
                    )

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

    # ------------------------------------------------------------------
    # YAML preparation
    # ------------------------------------------------------------------

    def _strip_gui_fields(self, yaml_data: dict) -> dict:
        """Remove GUI-only fields (gui_pos) from all nodes."""
        for node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict) and "gui_pos" in node_dict:
                del node_dict["gui_pos"]
        return yaml_data

    def _inject_display_server_node(self, yaml_data: dict) -> bool:
        """
        Inject a DisplayServer node into the simulation YAML so that specula
        always starts its built-in Socket.IO server on a fixed port when
        launched from the GUI.

        The injected block looks like:

            specula_studio_display_server:
              class: DisplayServer
              port: 5000
              mode: data
              params_dict_ref: <SimulParams node name>

        Rules
        -----
        - If a DisplayServer block already exists (user added one manually),
          it is left untouched and we return True immediately.
        - The node is appended at the end of yaml_data so it does not affect
          the order of user-defined nodes.
        - Any legacy ``display_server: true`` flag on SimulParams is removed
          to avoid having two display servers.

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
                # Remove legacy display_server flag if present
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

        # ── Build the DisplayServer block ─────────────────────────────────
        # input_ref_getter, output_ref_getter and info_getter are internal
        # callables that specula's simulation runner provides automatically;
        # they must NOT appear in the YAML config.
        ds_block = {
            "class":            "DisplayServer",
            "port":             _DISPLAY_SERVER_PORT,
            "mode":             "data",            
        }

        # Choose a node name that does not clash with existing keys
        ds_name = _DISPLAY_SERVER_NODE_NAME
        suffix  = 1
        while ds_name in yaml_data:
            ds_name = f"{_DISPLAY_SERVER_NODE_NAME}_{suffix}"
            suffix += 1

        yaml_data[ds_name] = ds_block
        print(
            f"[SIMULATION] Injected DisplayServer node '{ds_name}' "
            f"(port={_DISPLAY_SERVER_PORT}, mode=data, "            
        )
        return True

    def _prepare_simulation_yaml(self, file_path: str):
        """
        Post-process the exported YAML before handing it to specula:
          1. Strip GUI-only fields (gui_pos).
          2. Inject a DisplayServer node so the Socket.IO display server
             always starts on the fixed port.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)
            if not isinstance(yaml_data, dict):
                print(f"[SIMULATION] Warning: YAML root is not a dict, skipping preparation")
                return

            yaml_data = self._strip_gui_fields(yaml_data)
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

    def _on_display_server_port_found(self, port: int):
        new_url = f"http://127.0.0.1:{port}"
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
    # Simulation launch / control
    # ------------------------------------------------------------------

    def _schedule_display_server_reconnect(self, delay: float = 4.0):
        """
        Fallback reconnect attempt in case the port never appears in stdout.
        With a fixed port this should rarely be needed.
        """
        def _attempt(attempt_no, delay_s):
            time.sleep(delay_s)
            sio = self.editor.nm.sio_client
            if sio is None or sio.connected:
                return
            print(f"[SIMULATION] Fallback reconnect attempt {attempt_no} → {sio.server_url}")
            sio.reconnect()
            if not sio.connected and attempt_no == 1:
                threading.Thread(target=_attempt, args=(2, 6.0), daemon=True).start()

        threading.Thread(target=_attempt, args=(1, delay), daemon=True).start()

    def start_sim(self, sender=None, app_data=None, run_all_mode=False):
        if self.is_running:
            return

        self._clear_server_url_file()

        temp_path = self._get_sim_path()
        self.editor.fh.export_simulation(temp_path, include_defaults=True)
        # Strip GUI fields and inject the DisplayServer node
        self._prepare_simulation_yaml(temp_path)

        cmd = ["specula", temp_path]
        if not run_all_mode and dpg.get_value("sim_stepping"):
            cmd.append("--stepping")
        cmd.extend(["--nsimul", str(dpg.get_value("sim_nsimul"))])
        if dpg.get_value("sim_cpu"):
            cmd.append("--cpu")
        cmd.extend(["--target", str(dpg.get_value("sim_target"))])
        cmd.extend(["--precision", dpg.get_value("sim_precision")])
        cmd.extend(["--log-level", dpg.get_value("sim_log")])

        self.append_terminal(f"Executing: {' '.join(cmd)}\n")
        self.append_terminal(
            f"[INFO] DisplayServer will start on port {_DISPLAY_SERVER_PORT} …\n"
        )

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.is_running = True
            threading.Thread(target=self._read_output, daemon=True).start()
            # Write the expected URL immediately (port is fixed)
            expected_url = f"http://127.0.0.1:{_DISPLAY_SERVER_PORT}"
            self._write_server_url_file(expected_url)
            mm = self.editor.nm.monitors
            if hasattr(mm, "on_display_server_ready"):
                mm.on_display_server_ready(expected_url)
            # Fallback in case specula reports a different port
            self._schedule_display_server_reconnect(delay=4.0)
        except Exception as e:
            self.append_terminal(f"Launch Error: {e}\n")

    def _read_output(self):
        port_found = False
        while self.process and self.process.poll() is None:
            line = self.process.stdout.readline()
            if line:
                self.append_terminal(line)
                if not port_found:
                    port = self._try_extract_port(line)
                    if port:
                        port_found = True
                        self._on_display_server_port_found(port)

        self.is_running = False
        self.process = None
        self._clear_server_url_file()
        self.append_terminal("\n--- Finished ---\n")

    def step_sim(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            except Exception:
                pass

    def abort_sim(self):
        if self.process:
            self.process.terminate()
        self._clear_server_url_file()

