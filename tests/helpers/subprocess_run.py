"""
Run a tiny simulation through RemoteBackend in local mode (the `specula`
command) with an injected DisplayServer, and report what the editor saw.

    python -m tests.helpers.subprocess_run   → prints @@JSON@@{...}
"""

import json
import tempfile
import threading

from specula_studio.core.run_prep import write_run_files
from specula_studio.simulation_backend import RemoteBackend

BASE = {
    # long enough for the DisplayServer process to start and announce its port
    "main": {"class": "SimulParams", "root_dir": ".", "pixel_pupil": 8, "pixel_pitch": 0.1,
             "total_time": 2.0, "time_step": 0.001},
    "gen": {"class": "WaveGenerator", "constant": [3.0], "outputs": ["output"]},
}


def main():
    run_dir = tempfile.mkdtemp()
    files = write_run_files(run_dir, BASE, sim_name="t", session_id="test-session",
                            display_port=0)
    finished = threading.Event()
    ports = []
    terminal = []
    backend = RemoteBackend("localhost")
    backend.start(files, {"cpu": True, "nsimul": 1, "stepping": False}, terminal.append,
                  on_port_found=lambda port, ip: ports.append(port),
                  on_finished=finished.set)
    finished.wait(180)
    print("@@JSON@@" + json.dumps({"finished": finished.is_set(), "ports": ports,
                                   "terminal": "".join(terminal)[-3000:]}))


if __name__ == "__main__":
    main()
