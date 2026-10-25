"""
Run the monitor window of a Local Run (``monitor_window`` with ``--link``)
headless: the editor side is a ControlServer in this process sending frames;
only the calls that need a GPU window are stubbed.

Usage: python -m tests.helpers.monitor_window_smoke
"""

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault(
    "SPECULA_STUDIO_SETTINGS",
    os.path.join(tempfile.mkdtemp(prefix="specula_studio_test_"), "settings.json"),
)

import dearpygui.dearpygui as dpg  # noqa: E402
import numpy as np  # noqa: E402

_frames = {"n": 0, "limit": 400}


def _running():
    _frames["n"] += 1
    time.sleep(0.005)
    return _frames["n"] <= _frames["limit"]


for _name in ("create_viewport", "setup_dearpygui", "show_viewport", "set_primary_window",
              "render_dearpygui_frame", "set_viewport_always_top"):
    setattr(dpg, _name, lambda *a, **k: None)
dpg.is_dearpygui_running = _running

from specula_studio import control_link  # noqa: E402
from specula_studio.monitor_data import make_payload  # noqa: E402
from specula_studio.monitor_window import StandaloneMonitor  # noqa: E402


def main() -> int:
    server = control_link.ControlServer()
    os.environ[control_link.KEY_ENV] = server.authkey.hex()
    host, port = server.address
    monitor = StandaloneMonitor(server_url="", server_url_file=None,
                                server_output_name="gen.output", node_name="gen",
                                output_name="output", link_address=f"{host}:{port}", slot=1)

    def editor():
        deadline = time.time() + 10
        while not server.connected and time.time() < deadline:
            time.sleep(0.02)
        server.send({"type": "data", "payload": make_payload(np.arange(5.0))})
        time.sleep(0.3)
        server.send({"type": "data", "payload": make_payload(np.ones((8, 8)))})
        time.sleep(0.3)
        server.send({"type": "raise"})
        time.sleep(0.2)
        server.send({"type": "quit"})

    threading.Thread(target=editor, daemon=True).start()
    t0 = time.time()
    monitor.run()                                  # returns on "quit"
    elapsed = time.time() - t0
    server.close()
    mode = monitor.dpg_plotter.current_mode if monitor.dpg_plotter else None
    print(f"@@RESULT updates={monitor.update_count} mode={mode} "
          f"quit_early={_frames['n'] < _frames['limit']} elapsed={elapsed:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
