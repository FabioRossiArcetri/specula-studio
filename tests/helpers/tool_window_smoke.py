"""
Run a tool window process (``control_window.main``) headless for a few frames.

The editor side is a ControlServer in this same process; only the calls
that need a GPU window are stubbed, so every widget of the window goes
through the real DearPyGui.

Usage: python -m tests.helpers.tool_window_smoke {control|hpc_submit|hpc_jobs}
"""

import os
import sys
import tempfile

os.environ.setdefault(
    "SPECULA_STUDIO_SETTINGS",
    os.path.join(tempfile.mkdtemp(prefix="specula_studio_test_"), "settings.json"),
)

import dearpygui.dearpygui as dpg  # noqa: E402

FRAMES = 5
_frames = {"n": 0}


def _running():
    _frames["n"] += 1
    return _frames["n"] <= FRAMES


for _name in ("create_viewport", "setup_dearpygui", "show_viewport", "set_primary_window",
              "render_dearpygui_frame", "set_viewport_always_top"):
    setattr(dpg, _name, lambda *a, **k: None)
dpg.is_dearpygui_running = _running

from specula_studio import control_link, control_window, hpc  # noqa: E402
from specula_studio.hpc_control import DEFAULT_PROFILE  # noqa: E402


def main(kind: str) -> int:
    init = {
        "control": {"type": "init", "recent_hosts": [{"host": "sauron", "user": ""}],
                    "terminal": "hello\n", "status": "idle"},
        "hpc_submit": {"type": "init", "profile": DEFAULT_PROFILE,
                       "sim_info": {"name": "scao", "n_overrides": 0,
                                    "run_files": ["scao_simul.yml"], "local_calib": ""}},
        "hpc_jobs": {"type": "init", "jobs": [hpc.HpcJob(
            job_id="1", name="t", login_host="h", transfer_host="h", job_dir="/w",
            state="RUNNING").to_dict()]},
    }[kind]
    events = {
        "control": [{"type": "terminal", "text": "more\n"}, {"type": "status", "text": "x"}],
        "hpc_submit": [{"type": "log", "text": "hi"},
                       {"type": "conn_status", "text": "OK", "ok": True},
                       {"type": "sim_info", "name": "b", "n_overrides": 1,
                        "run_files": ["b_simul.yml", "override1_o.yml"], "local_calib": ""}],
        "hpc_jobs": [{"type": "jobs", "jobs": []},
                     {"type": "job_output", "job_id": "1", "text": "out"}],
    }[kind]

    server = control_link.ControlServer()

    def on_connect():
        server.send(init)
        for ev in events:
            server.send(ev)
    server.on_connect = on_connect
    os.environ[control_link.KEY_ENV] = server.authkey.hex()
    host, port = server.address
    try:
        code = control_window.main(["--address", f"{host}:{port}", "--kind", kind])
    finally:
        server.close()
    if code == 0:
        print(f"@@OK {kind}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
