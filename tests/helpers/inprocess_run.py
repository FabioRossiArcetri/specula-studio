"""
Run tiny simulations with the real SPECULA through InProcessBackend.

    python -m tests.helpers.inprocess_run      → prints @@JSON@@{results}
"""

import json
import os
import tempfile
import threading
import time

import yaml

from specula_studio.core.run_prep import write_run_files
from specula_studio.monitor_bus import MonitorBus
from specula_studio.simulation_backend import InProcessBackend

BASE = {
    "main": {"class": "SimulParams", "pixel_pupil": 8, "pixel_pitch": 0.1,
             "total_time": 0.010, "time_step": 0.001},
    "gen": {"class": "WaveGenerator", "constant": [3.0], "slope": [1000.0],
            "outputs": ["output"]},
}


def _run(base, overrides=(), stepping=False, subscribe_first=True, actions=None):
    run_dir = tempfile.mkdtemp()
    ov_paths = []
    for i, ov in enumerate(overrides):
        path = os.path.join(run_dir, f"ov{i}.yml")
        with open(path, "w") as f:
            yaml.safe_dump(ov, f)
        ov_paths.append(path)
    files = write_run_files(run_dir, base, ov_paths, sim_name="t")

    bus = MonitorBus()
    received = []
    lock = threading.Lock()

    def on_data(payload):
        with lock:
            received.append(float(payload["data"].reshape(-1)[0]))

    if subscribe_first:
        bus.subscribe("gen.output", on_data)
    backend = InProcessBackend(monitor_bus=bus)
    finished = threading.Event()
    terminal = []
    backend.start(files, {"cpu": True, "nsimul": 1, "stepping": stepping, "precision": 1},
                  terminal.append, on_port_found=lambda *a: None, on_finished=finished.set)
    if actions:
        actions(backend, bus, on_data, received)
    finished.wait(120)

    from specula.loop_control import LoopControl
    hooks_restored = LoopControl.run.__name__ == "run" and LoopControl.iter.__name__ == "iter"
    return {"values": received, "finished": finished.is_set(),
            "hooks_restored": hooks_restored, "terminal": "".join(terminal)[-3000:]}


def _wait_for(pred, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def stepping_actions(backend, bus, on_data, received):
    # wait until the loop is built and waiting at the first prompt
    _wait_for(lambda: backend._registry)
    time.sleep(0.5)
    # monitor opened while the run is paused → probe added between steps
    bus.subscribe("gen.output", on_data)
    backend.attach_probe("gen.output")
    backend.step(3)
    _wait_for(lambda: len(received) >= 3, timeout=30)
    time.sleep(0.5)
    received.append("after_step3")
    backend.abort()


def main():
    results = {
        "free_run": _run(BASE),
        "stepping": _run(BASE, stepping=True, subscribe_first=False,
                         actions=stepping_actions),
        "override": _run(BASE, overrides=[{"gen_override": {"constant": [7.0], "slope": [0.0]}}]),
    }
    print("@@JSON@@" + json.dumps(results))


if __name__ == "__main__":
    main()
