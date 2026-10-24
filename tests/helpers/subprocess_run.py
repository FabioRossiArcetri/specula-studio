"""
Run a small simulation through RemoteBackend in local mode (the `specula`
command) with an injected DisplayServer, and report what the editor saw.
The run is aborted as soon as the DisplayServer port has been detected.

    python -m tests.helpers.subprocess_run   → prints @@JSON@@{...}
"""

import json
import tempfile
import threading

from specula_studio.core import catalog
from specula_studio.core.run_prep import write_run_files
from specula_studio.simulation_backend import RemoteBackend

BASE = {
    # Long enough for the DisplayServer process to start on any machine: the
    # loop runs at tens of kHz, the run is aborted once the port is known.
    "main": {"class": "SimulParams", "root_dir": ".", "pixel_pupil": 8, "pixel_pitch": 0.1,
             "total_time": 1000.0, "time_step": 0.001},
    "gen": {"class": "WaveGenerator", "constant": [3.0], "outputs": ["output"]},
}


def display_server_params() -> set:
    """Parameters of the installed DisplayServer, as the editor's catalog sees them."""
    root = catalog.specula_root()
    _, proc = catalog.build_catalog(root, runtime=False, with_help=False)
    return set(proc.get("DisplayServer", {}).get("parameters", {}))


def main():
    run_dir = tempfile.mkdtemp()
    files = write_run_files(run_dir, BASE, sim_name="t", session_id="test-session",
                            display_port=0, display_params=display_server_params())
    finished = threading.Event()
    ports = []
    terminal = []
    backend = RemoteBackend("localhost")

    def on_port(port, ip):
        ports.append(port)
        backend.abort()

    backend.start(files, {"cpu": True, "nsimul": 1, "stepping": False}, terminal.append,
                  on_port_found=on_port, on_finished=finished.set)
    finished.wait(180)
    if not finished.is_set():
        backend.abort()
    print("@@JSON@@" + json.dumps({"finished": finished.is_set(), "ports": ports,
                                   "terminal": "".join(terminal)[-3000:]}))


if __name__ == "__main__":
    main()
