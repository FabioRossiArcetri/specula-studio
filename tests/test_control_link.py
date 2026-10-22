"""Editor <-> control window link (no GUI)."""

import time
from multiprocessing import AuthenticationError

import pytest

from specula_studio import control_link
from specula_studio.control_window import RemoteActions, apply_event


def _wait(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_roundtrip_init_commands_and_events():
    server = control_link.ControlServer()
    server.on_connect = lambda: server.send({"type": "init", "status": "idle"})
    conn = control_link.connect(server.address, server.authkey)
    try:
        assert conn.recv() == {"type": "init", "status": "idle"}
        actions = RemoteActions(conn)
        actions.start({"backend": "Remote", "host": "sauron"})
        actions.step(5)
        actions.abort()
        got = [server.commands.get(timeout=5) for _ in range(3)]
        assert got[0] == {"type": "start", "options": {"backend": "Remote", "host": "sauron"}}
        assert got[1] == {"type": "step", "n": 5} and got[2] == {"type": "abort"}
        assert server.send({"type": "terminal", "text": "hi\n"})
        assert conn.recv() == {"type": "terminal", "text": "hi\n"}
    finally:
        conn.close()
        server.close()


def test_wrong_key_is_refused():
    server = control_link.ControlServer()
    try:
        with pytest.raises((AuthenticationError, OSError, EOFError)):
            control_link.connect(server.address, b"wrong-key")
        assert not server.connected
    finally:
        server.close()


def test_disconnect_is_noticed_and_send_is_harmless():
    server = control_link.ControlServer()
    conn = control_link.connect(server.address, server.authkey)
    assert _wait(lambda: server.connected)
    conn.close()
    assert _wait(lambda: not server.connected)
    assert server.send({"type": "status", "text": "x"}) is False
    server.close()


class _View:
    def __init__(self):
        self.calls = []

    def append_terminal(self, text): self.calls.append(("append", text))
    def reset_terminal(self, text): self.calls.append(("reset", text))
    def set_status(self, text): self.calls.append(("status", text))
    def set_ssh_test(self, msg, ok): self.calls.append(("ssh", msg, ok))


def test_apply_event():
    view = _View()
    assert apply_event(view, {"type": "terminal", "text": "a"}, dpg=None)
    assert apply_event(view, {"type": "terminal_reset", "text": ""}, dpg=None)
    assert apply_event(view, {"type": "status", "text": "running"}, dpg=None)
    assert apply_event(view, {"type": "ssh_test", "message": "ok", "ok": True}, dpg=None)
    assert view.calls == [("append", "a"), ("reset", ""), ("status", "running"),
                          ("ssh", "ok", True)]
    assert apply_event(view, {"type": "quit"}, dpg=None) is False
    assert apply_event(view, {"type": "closed"}, dpg=None) is False
