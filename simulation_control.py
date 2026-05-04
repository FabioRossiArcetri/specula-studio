import subprocess
import threading
import os
import dearpygui.dearpygui as dpg

class SimulationControl:
    def __init__(self, editor):
        self.editor = editor
        self.process = None
        self.terminal_data = []
        self.is_running = False

    def _get_sim_path(self):
        # Fix: Ensure name is a string, default to 'untitled' if None
        name = getattr(self.editor, "current_scene_name", "untitled")
        if name is None:
            name = "untitled"
        return f"{name}_simul.yml"

    def show_window(self):
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

    def start_sim(self, sender=None, app_data=None, run_all_mode=False):
        if self.is_running: return
        
        temp_path = self._get_sim_path()
        self.editor.fh.export_simulation(temp_path, include_defaults=True)
        
        cmd = ["specula", temp_path]
        if not run_all_mode and dpg.get_value("sim_stepping"):
            cmd.append("--stepping")
        
        cmd.extend(["--nsimul", str(dpg.get_value("sim_nsimul"))])
        if dpg.get_value("sim_cpu"): cmd.append("--cpu")
        cmd.extend(["--target", str(dpg.get_value("sim_target"))])
        cmd.extend(["--precision", dpg.get_value("sim_precision")])
        cmd.extend(["--log-level", dpg.get_value("sim_log")])

        self.append_terminal(f"Executing: {' '.join(cmd)}\n")
        
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                stdin=subprocess.PIPE, text=True, bufsize=1
            )
            self.is_running = True
            threading.Thread(target=self._read_output, daemon=True).start()
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