"""Losing the DisplayServer during a run: reported, and monitors back off."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from specula_studio import constants, simulation_control
from specula_studio.monitor_window import StandaloneMonitor
from specula_studio.socketio_client import SocketIOClient


def _control(monkeypatch):
    monkeypatch.setattr(simulation_control.SimulationControl, "DS_LOST_GRACE", 0.05)
    sio = SimpleNamespace(connected=True, server_url="http://sauron:5000")
    editor = SimpleNamespace(nm=SimpleNamespace(sio_client=sio))
    sc = simulation_control.SimulationControl(editor)
    sc.is_running = True
    sc._session_id = "run-1"
    sc._status = "running (Remote)"
    return sc, sio


def _terminal(sc):
    return "".join(sc.terminal_data)


def test_lost_display_server_is_reported_once(monkeypatch):
    sc, sio = _control(monkeypatch)
    sc._on_display_server_connection(True)
    sio.connected = False
    sc._on_display_server_connection(False)
    sc._on_display_server_connection(False)
    time.sleep(0.3)
    assert _terminal(sc).count("Lost the display server (http://sauron:5000)") == 1
    assert sc._status == "running (Remote) — display server lost"

    sio.connected = True
    sc._on_display_server_connection(True)
    assert "Reconnected to the display server" in _terminal(sc)
    assert sc._status == "running (Remote)"


def test_no_report_at_the_end_of_the_run(monkeypatch):
    sc, sio = _control(monkeypatch)
    sc._on_display_server_connection(True)
    sio.connected = False
    sc._on_display_server_connection(False)
    sc.is_running = False                       # the run ended within the grace time
    time.sleep(0.3)
    assert "Lost the display server" not in _terminal(sc)


def test_no_report_before_the_new_run_connected(monkeypatch):
    """A late disconnect of the previous run's server is not a loss of this one."""
    sc, sio = _control(monkeypatch)
    sc._on_display_server_connection(True)
    sc._session_id = "run-2"                    # new run, its server not reached yet
    sio.connected = False
    sc._on_display_server_connection(False)
    time.sleep(0.3)
    assert "Lost the display server" not in _terminal(sc)


def test_only_unexpected_disconnects_notify():
    client = SocketIOClient(server_url="http://127.0.0.1:1")
    events = []
    client.on_connection_change = events.append
    on_disconnect = client.sio.handlers["/"]["disconnect"]

    client._closing = True                       # inside SocketIOClient.disconnect()
    on_disconnect()
    assert events == []
    client._closing = False                      # the server went away
    on_disconnect("transport close")
    assert events == [False]

    client.connected = True
    client.sio = MagicMock()
    client.disconnect()
    client.sio.disconnect.assert_called_once()
    assert client._closing is False


def test_monitor_stall_requests_back_off_and_reset():
    mon = StandaloneMonitor("http://127.0.0.1:1", None, "gen.output", "gen", "output")
    mon.connected = True
    mon.sio = MagicMock()
    delays = []
    try:
        for _ in range(5):
            mon._on_stall_detected()
            delays.append(mon._stall_timer.interval)
        assert delays == [20.0, 40.0, 60.0, 60.0, 60.0]
        assert mon.sio.emit.call_count == 5
        assert mon._pending_status == "waiting"
        mon._arm_stall_watchdog(reset=True)          # a 'done' arrived
        assert mon._stall_timer.interval == constants.STREAM_STALL_TIMEOUT
    finally:
        mon._disarm_stall_watchdog()
