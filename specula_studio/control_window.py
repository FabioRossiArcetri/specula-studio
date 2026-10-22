"""
The simulation control panel in its own OS window.

Started by the editor (``python -m specula_studio.control_window --address
host:port``, key in the environment); everything it does is sent to the
editor over :mod:`specula_studio.control_link`, which runs the simulations.
Closing this window does not stop a running simulation; it can be reopened
from *Simulation → Control Panel*.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading

log = logging.getLogger(__name__)

_MAIN = "control_main_window"


class RemoteActions:
    """ControlPanelView actions forwarded to the editor."""

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()

    def _send(self, msg: dict) -> None:
        with self.lock:
            try:
                self.conn.send(msg)
            except (OSError, EOFError, ValueError):
                log.warning("Editor connection lost")

    def start(self, options: dict) -> None:
        self._send({"type": "start", "options": options})

    def step(self, n: int = 1) -> None:
        self._send({"type": "step", "n": int(n)})

    def abort(self) -> None:
        self._send({"type": "abort"})

    def test_ssh(self, target: str) -> None:
        self._send({"type": "test_ssh", "target": target})

    def clear_terminal(self) -> None:
        self._send({"type": "clear_terminal"})


def apply_event(view, msg: dict, dpg) -> bool:
    """Apply one editor event to *view*; False means the window must close."""
    kind = msg.get("type")
    if kind == "terminal":
        view.append_terminal(msg.get("text", ""))
    elif kind == "terminal_reset":
        view.reset_terminal(msg.get("text", ""))
    elif kind == "status":
        view.set_status(msg.get("text", ""))
    elif kind == "ssh_test":
        view.set_ssh_test(msg.get("message", ""), msg.get("ok"))
    elif kind == "raise":
        # No "focus" API for the viewport: briefly keeping it on top brings it
        # in front of the other windows.
        dpg.set_viewport_always_top(True)
        dpg.set_frame_callback(dpg.get_frame_count() + 2,
                               lambda: dpg.set_viewport_always_top(False))
    elif kind in ("quit", "closed"):
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SPECULA Studio simulation control window")
    parser.add_argument("--address", required=True, help="host:port of the editor")
    parser.add_argument("--title", default="SPECULA Studio - Simulation Control")
    args = parser.parse_args(argv)

    from .logs import setup_logging
    setup_logging(os.environ.get("SPECULA_STUDIO_LOG_LEVEL", "INFO"))

    key = os.environ.get("SPECULA_STUDIO_CONTROL_KEY", "")
    host, _, port = args.address.rpartition(":")
    from .control_link import connect
    try:
        conn = connect((host, int(port)), bytes.fromhex(key))
    except Exception as exc:
        log.error("Cannot connect to the editor at %s: %s", args.address, exc)
        return 1

    # The editor sends recent hosts, terminal backlog and status first.
    init = conn.recv() if conn.poll(15) else {}

    import dearpygui.dearpygui as dpg

    from . import render_scale, settings
    from .theme_manager import ThemeManager
    from .ui.control_panel import ControlPanelView

    dpg.create_context()
    tm = ThemeManager()
    tm.apply_theme()
    prefs = settings.store()
    render_scale.set_size(prefs.get("render_size", "MEDIUM"))
    font = prefs.get("font_path")
    if font and os.path.exists(font):
        with dpg.font_registry():
            dpg.bind_font(dpg.add_font(font, render_scale.font_size()))

    view = ControlPanelView(RemoteActions(conn), tm,
                            recent_hosts=init.get("recent_hosts", []))
    with dpg.window(tag=_MAIN):
        pass
    view.build(_MAIN)
    view.reset_terminal(init.get("terminal", ""))
    view.set_status(init.get("status", "idle"))

    events: queue.Queue = queue.Queue()

    def receive():
        while True:
            try:
                events.put(conn.recv())
            except (EOFError, OSError):
                events.put({"type": "closed"})
                return

    threading.Thread(target=receive, daemon=True, name="editor-link").start()

    dpg.create_viewport(title=args.title, width=1020, height=700)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window(_MAIN, True)
    try:
        while dpg.is_dearpygui_running():
            keep = True
            while keep:
                try:
                    msg = events.get_nowait()
                except queue.Empty:
                    break
                keep = apply_event(view, msg, dpg)
            if not keep:
                break
            dpg.render_dearpygui_frame()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        dpg.destroy_context()
    return 0


if __name__ == "__main__":
    sys.exit(main())
