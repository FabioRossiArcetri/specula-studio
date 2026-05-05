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
# Patterns used to extract the display-server URL from specula stdout
# ---------------------------------------------------------------------------
# Matches:  "Running on http://0.0.0.0:5432"   (aiohttp / uvicorn style)
_URL_RE = re.compile(
    r'https?://(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(\d{4,5})',
    re.IGNORECASE,
)
# Matches:  "display server … port 5432"  /  "server started on :5432"
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
        # Written as soon as we detect the display-server port from specula stdout.
        self._server_url_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "specula_studio_server.json",
        )
        # Remove any stale file from a previous run
        self._clear_server_url_file()

    # ------------------------------------------------------------------
    # Coordination file helpers
    # ------------------------------------------------------------------

    def _clear_server_url_file(self):
        """Remove the coordination file (called on start and on process exit)."""
        try:
            if os.path.exists(self._server_url_file):
                os.remove(self._server_url_file)
        except Exception:
            pass

    def _write_server_url_file(self, url: str):
        """Write the discovered display-server URL for monitor subprocesses."""
        try:
            with open(self._server_url_file, "w", encoding="utf-8") as f:
                json.dump({"url": url}, f)
            print(f"[SIMULATION] Wrote server URL file: {self._server_url_file} → {url}")
        except Exception as e:
            print(f"[SIMULATION] Could not write server URL file: {e}")

    # ------------------------------------------------------------------
    # Display window helpers
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

    def _strip_gui_fields(self, yaml_data):
        for node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict) and "gui_pos" in node_dict:
                del node_dict["gui_pos"]
        return yaml_data

    def _inject_display_server_into_yaml(self, yaml_data):
        """
        Ensure SimulParams has display_server: true so specula auto-creates
        its built-in Socket.IO display server.
        """
        found = False
        for node_name, node_dict in yaml_data.items():
            if not isinstance(node_dict, dict):
                continue
            if node_dict.get("class") != "SimulParams":
                continue
            found = True
            if not node_dict.get("display_server", False):
                node_dict["display_server"] = True
                print(f"[SIMULATION] Injected 'display_server: true' into SimulParams '{node_name}'")

        if not found:
            print(
                "[SIMULATION] Warning: No SimulParams block found — "
                "display_server could not be injected."
            )
        return found

    def _prepare_simulation_yaml(self, file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)
            if not isinstance(yaml_data, dict):
                return
            yaml_data = self._strip_gui_fields(yaml_data)
            self._inject_display_server_into_yaml(yaml_data)
            with open(file_path, "w", encoding="utf-8") as f:
                yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False)
            print(f"[SIMULATION] Prepared simulation YAML: {file_path}")
        except Exception as e:
            print(f"[SIMULATION] Warning: could not prepare YAML: {e}")

    # kept for backward-compat
    def _clean_simulation_yaml(self, file_path):
        self._prepare_simulation_yaml(file_path)

    # ------------------------------------------------------------------
    # Port / URL detection
    # ------------------------------------------------------------------

    def _try_extract_port(self, line: str):
        """
        Attempt to extract the display-server port from a single stdout line.
        Returns the port as int, or None if not found.
        """
        for pattern in (_URL_RE, _PORT_KW_RE):
            m = pattern.search(line)
            if m:
                port = int(m.group(1))
                # Ignore obviously wrong values (e.g. year, small numbers)
                if 1024 <= port <= 65535:
                    return port
        return None

    def _on_display_server_port_found(self, port: int):
        """
        Called (from the stdout reader thread) when specula announces its port.
        Updates sio_client and MonitorManager, then writes the coordination file.
        """
        new_url = f"http://127.0.0.1:{port}"
        print(f"[SIMULATION] Display server detected at {new_url}")
        self.append_terminal(f"[INFO] Display server running at {new_url}\n")

        # Update the main-process Socket.IO client
        sio = self.editor.nm.sio_client
        sio.server_url = new_url

        # Write coordination file so monitor subprocesses can discover the URL
        self._write_server_url_file(new_url)

        # Notify MonitorManager (flushes any pending monitors, restarts wrong-URL ones)
        mm = self.editor.nm.monitors
        if hasattr(mm, "on_display_server_ready"):
            mm.on_display_server_ready(new_url)

        # Reconnect main-process sio_client
        if not sio.connected:
            threading.Thread(target=sio.reconnect, daemon=True).start()

    # ------------------------------------------------------------------
    # Simulation launch / control
    # ------------------------------------------------------------------

    def _schedule_display_server_reconnect(self, delay: float = 4.0):
        """
        Fallback: if we never detect the port from stdout, try port 5000 (the
        default in constants.py) after *delay* seconds, then once more.
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

        # Remove stale coordination file from any previous run
        self._clear_server_url_file()

        temp_path = self._get_sim_path()
        self.editor.fh.export_simulation(temp_path, include_defaults=True)
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
            "[INFO] Waiting for specula to announce display-server port …\n"
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

            # Fallback in case the port never appears in stdout
            self._schedule_display_server_reconnect(delay=5.0)

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