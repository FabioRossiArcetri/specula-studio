"""Local Run monitors: separate windows fed by the editor over a control link."""

import subprocess
import sys
import time

import numpy as np

from specula_studio import control_link
from specula_studio.inprocess_monitor import InProcessMonitor
from specula_studio.monitor_bus import MonitorBus
from specula_studio.monitor_data import make_payload
from tests.conftest import REPO


def _monitor(bus):
    InProcessMonitor.launch_windows = False
    m = InProcessMonitor("m1", "uuid", "gen", "output", "gen.output", bus)
    m.open()
    return m


def _recv(conn, timeout=5.0):
    if conn.poll(timeout):
        return conn.recv()
    raise AssertionError("no message from the editor")


def test_frames_go_to_the_window_newest_only():
    bus = MonitorBus()
    m = _monitor(bus)
    try:
        bus.push("gen.output", make_payload(np.arange(3.0)))     # before the window: kept
        time.sleep(0.2)
        assert m.sent_count == 0 and m.update_count == 1
        conn = control_link.connect(m._link.address, m._link.authkey)
        msg = _recv(conn)
        assert msg["type"] == "data" and list(msg["payload"]["data"]) == [0.0, 1.0, 2.0]
        for v in range(50):                                      # a burst: not all sent
            bus.push("gen.output", make_payload(np.full(3, float(v))))
        time.sleep(0.5)
        last = None
        while conn.poll(0.1):
            last = conn.recv()
        assert last is not None and last["payload"]["data"][0] == 49.0
        assert m.sent_count < 50 and m.update_count == 51
        assert m.render_frame()                                  # still open
        conn.close()
    finally:
        m.close()
    assert not m.is_open and bus.subscriber_count("gen.output") == 0


def test_window_closed_by_the_user_closes_the_monitor():
    bus = MonitorBus()
    m = _monitor(bus)
    try:
        class Exited:                          # the window process has ended
            def poll(self):
                return 0
        m._window.process = Exited()
        assert m.render_frame() is False
    finally:
        m.close()


def test_monitor_window_shows_the_editors_frames():
    proc = subprocess.run([sys.executable, "-m", "tests.helpers.monitor_window_smoke"],
                          cwd=REPO, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("@@RESULT"))
    assert "updates=2" in line and "mode=image" in line and "quit_early=True" in line, line
