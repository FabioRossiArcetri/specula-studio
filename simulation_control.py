import subprocess
import threading
import os
import time
import yaml
import dearpygui.dearpygui as dpg

from constants import SOCKETIO_SERVER

class SimulationControl:
    def __init__(self, editor):
        self.editor = editor
        self.process = None
        self.terminal_data = []
        self.is_running = False
        self._reconnect_timer = None

    def _get_sim_path(self):
        # Fix: Ensure name is a string, default to 'untitled' if None
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
                # Settings Column
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

                # Terminal Column
                # IMPORTANT: We tag the CHILD WINDOW for scrolling
                with dpg.child_window(width=-1, tag="sim_terminal_child", border=True):
                    dpg.add_text("Terminal Output", color=[150, 150, 150])
                    dpg.add_input_text(
                        tag="sim_terminal", 
                        multiline=True, 
                        readonly=True, 
                        width=-1, 
                        height=-1
                    )

    def append_terminal(self, text):
        self.terminal_data.append(text)
        if len(self.terminal_data) > 1000: self.terminal_data.pop(0)
        
        if dpg.does_item_exist("sim_terminal"):
            dpg.set_value("sim_terminal", "".join(self.terminal_data))
            
            # FIX: Scroll the CHILD WINDOW (container), not the InputText
            try:
                dpg.set_y_scroll("sim_terminal_child", -1.0)
            except Exception:
                pass

    def _strip_gui_fields(self, yaml_data):
        """
        Strip GUI-specific fields from node representations in YAML.
        Removes 'gui_pos' field from all node definitions.
        
        Args:
            yaml_data (dict): The parsed YAML data structure
            
        Returns:
            dict: The cleaned YAML data
        """
        for node_name, node_dict in yaml_data.items():
            if isinstance(node_dict, dict):
                # Remove 'gui_pos' field if present
                if 'gui_pos' in node_dict:
                    del node_dict['gui_pos']
        
        return yaml_data

    def _inject_display_server_into_yaml(self, yaml_data):
        """
        Ensure the display_server flag is enabled in all SimulParams blocks.

        When running a simulation locally from the GUI the specula process must
        start its built-in Socket.IO display server so that the monitor windows
        can subscribe to output data.  This is controlled by the ``display_server``
        boolean field on ``SimulParams``.  If the user has not set it explicitly
        in the scene we inject it here so it is always present in the YAML that
        is handed to specula.

        Args:
            yaml_data (dict): The parsed YAML data structure (modified in-place)

        Returns:
            bool: True if at least one SimulParams block was found and patched.
        """
        found = False
        for node_name, node_dict in yaml_data.items():
            if not isinstance(node_dict, dict):
                continue
            if node_dict.get('class') != 'SimulParams':
                continue
            found = True
            if not node_dict.get('display_server', False):
                node_dict['display_server'] = True
                print(f"[SIMULATION] Injected 'display_server: true' into SimulParams block '{node_name}'")
            else:
                print(f"[SIMULATION] 'display_server' already enabled in SimulParams block '{node_name}'")

        if not found:
            print(
                "[SIMULATION] Warning: No SimulParams block found in YAML — "
                "display_server could not be injected. "
                "The simulation monitors may not receive data."
            )
        return found

    def _prepare_simulation_yaml(self, file_path):
        """
        Load the exported YAML, strip GUI fields, and inject the display_server
        flag into SimulParams so that specula always starts its Socket.IO display
        server when launched from the GUI.

        This replaces the old ``_clean_simulation_yaml`` method and extends it
        with the display-server injection step.

        Args:
            file_path (str): Path to the exported simulation YAML file
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                yaml_data = yaml.safe_load(f)
            
            if not isinstance(yaml_data, dict):
                print(f"[SIMULATION] Warning: YAML root is not a dict, skipping preparation")
                return

            # Step 1: Strip GUI-only fields (e.g. 'gui_pos')
            yaml_data = self._strip_gui_fields(yaml_data)

            # Step 2: Ensure the display server is enabled in SimulParams so
            #         the GUI monitors can connect to the running simulation.
            self._inject_display_server_into_yaml(yaml_data)

            # Write the prepared YAML back
            with open(file_path, 'w', encoding='utf-8') as f:
                yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False)

            print(f"[SIMULATION] Prepared simulation YAML: {file_path}")

        except Exception as e:
            print(f"[SIMULATION] Warning: Could not prepare YAML file {file_path}: {e}")

    # ------------------------------------------------------------------
    # Backward-compatibility alias
    # ------------------------------------------------------------------
    def _clean_simulation_yaml(self, file_path):
        """Deprecated alias kept for compatibility — delegates to _prepare_simulation_yaml."""
        self._prepare_simulation_yaml(file_path)

    def _schedule_display_server_reconnect(self, delay: float = 4.0):
        """
        Schedule a background attempt to connect the Socket.IO client to the
        display server that specula will start inside the simulation process.

        The delay gives specula time to initialise and open the server socket
        before we try to connect.  A second attempt is made after another few
        seconds in case the simulation takes longer to boot.

        Args:
            delay (float): Seconds to wait before the first connection attempt.
        """
        def _attempt(attempt_no: int, delay_s: float):
            time.sleep(delay_s)
            try:
                sio = self.editor.nm.sio_client
                if sio is None:
                    return
                if sio.connected:
                    print(
                        f"[SIMULATION] Display-server reconnect attempt {attempt_no}: "
                        f"already connected to {sio.server_url}"
                    )
                    return
                print(
                    f"[SIMULATION] Display-server reconnect attempt {attempt_no}: "
                    f"connecting to {sio.server_url} …"
                )
                sio.reconnect()
                if sio.connected:
                    print(f"[SIMULATION] Connected to display server on attempt {attempt_no}")
                else:
                    # Schedule one more retry if this was the first attempt
                    if attempt_no == 1:
                        threading.Thread(
                            target=_attempt,
                            args=(2, 5.0),
                            daemon=True,
                        ).start()
            except Exception as e:
                print(f"[SIMULATION] Display-server reconnect attempt {attempt_no} failed: {e}")

        self._reconnect_timer = threading.Thread(
            target=_attempt,
            args=(1, delay),
            daemon=True,
        )
        self._reconnect_timer.start()

    def start_sim(self, sender=None, app_data=None, run_all_mode=False):
        if self.is_running: return
        
        temp_path = self._get_sim_path()
        self.editor.fh.export_simulation(temp_path, include_defaults=True)
        
        # Clean the exported YAML (strip GUI fields) AND inject display_server:
        # true into SimulParams so that specula starts its Socket.IO server.
        self._prepare_simulation_yaml(temp_path)
        
        cmd = ["specula", temp_path]
        if not run_all_mode and dpg.get_value("sim_stepping"):
            cmd.append("--stepping")
        
        cmd.extend(["--nsimul", str(dpg.get_value("sim_nsimul"))])
        if dpg.get_value("sim_cpu"): cmd.append("--cpu")
        cmd.extend(["--target", str(dpg.get_value("sim_target"))])
        cmd.extend(["--precision", dpg.get_value("sim_precision")])
        cmd.extend(["--log-level", dpg.get_value("sim_log")])

        self.append_terminal(f"Executing: {' '.join(cmd)}\n")
        self.append_terminal(
            f"Display server will be available at: {SOCKETIO_SERVER}\n"
            f"Monitor windows will connect automatically once specula is ready.\n"
        )
        
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                stdin=subprocess.PIPE, text=True, bufsize=1
            )
            self.is_running = True
            threading.Thread(target=self._read_output, daemon=True).start()

            # Schedule a reconnect attempt so the GUI monitors can subscribe to
            # data from the local simulation's display server.  We wait a few
            # seconds to give specula time to start up and open the socket.
            self._schedule_display_server_reconnect(delay=4.0)

        except Exception as e:
            self.append_terminal(f"Launch Error: {e}\n")

    def _read_output(self):
        while self.process and self.process.poll() is None:
            line = self.process.stdout.readline()
            if line:
                self.append_terminal(line)
        self.is_running = False
        self.process = None
        self.append_terminal("\n--- Finished ---\n")

    def step_sim(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            except: pass

    def abort_sim(self):
        if self.process:
            self.process.terminate()