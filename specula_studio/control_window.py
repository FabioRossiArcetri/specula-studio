"""
The editor's tool windows in their own OS windows.

Started by the editor (``python -m specula_studio.control_window --address
host:port --kind KIND``, key in the environment), one process per window:

* ``control``    — the simulation control panel;
* ``hpc_submit`` — HPC → Submit Batch Job;
* ``hpc_jobs``   — HPC → Batch Jobs.

Everything a window does is sent to the editor over
:mod:`specula_studio.control_link`; the editor runs the simulations and the
HPC work and sends back what the window shows.  Closing a window stops
nothing (a running simulation, the following of batch jobs); it can be
reopened from its menu.
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

# kind -> (default title, viewport width, viewport height): pixels of the
# layout's built-in 13 px font (see render_scale.px)
KINDS = {
    "control":    ("SPECULA Studio - Simulation Control", 1020, 700),
    "hpc_submit": ("SPECULA Studio - HPC Batch Job (SLURM)", 900, 960),
    "hpc_jobs":   ("SPECULA Studio - HPC Jobs", 1020, 720),
}
MAX_SIZE = (1800, 1000)     # a full HD screen, its taskbar and the title bar


class _Sender:
    """Thread-safe sending of plain-dict messages to the editor."""

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()

    def send(self, msg: dict) -> None:
        with self.lock:
            try:
                self.conn.send(msg)
            except (OSError, EOFError, ValueError):
                log.warning("Editor connection lost")


class RemoteActions(_Sender):
    """ControlPanelView actions forwarded to the editor."""

    def start(self, options: dict) -> None:
        self.send({"type": "start", "options": options})

    def step(self, n: int = 1) -> None:
        self.send({"type": "step", "n": int(n)})

    def abort(self) -> None:
        self.send({"type": "abort"})

    def test_ssh(self, target: str) -> None:
        self.send({"type": "test_ssh", "target": target})

    def clear_terminal(self) -> None:
        self.send({"type": "clear_terminal"})


def apply_event(view, msg: dict, dpg) -> bool:
    """Apply one editor event to the control panel *view*; False means the window must close."""
    kind = msg.get("type")
    if kind == "terminal":
        view.append_terminal(msg.get("text", ""))
    elif kind == "terminal_reset":
        view.reset_terminal(msg.get("text", ""))
    elif kind == "status":
        view.set_status(msg.get("text", ""))
    elif kind == "ssh_test":
        view.set_ssh_test(msg.get("message", ""), msg.get("ok"))
    elif kind in ("quit", "closed"):
        return False
    return True


def _build_view(kind: str, conn, init: dict, tm):
    """Build the view of *kind* in the main window; return its event handler."""
    if kind == "control":
        from .ui.control_panel import ControlPanelView

        view = ControlPanelView(RemoteActions(conn), tm,
                                recent_hosts=init.get("recent_hosts", []))
        view.build(_MAIN)
        view.reset_terminal(init.get("terminal", ""))
        view.set_status(init.get("status", "idle"))
        return lambda msg: apply_event(view, msg, None)

    from .ui.hpc_views import HpcJobsView, HpcSubmitView

    sender = _Sender(conn)
    hpc_view: HpcSubmitView | HpcJobsView
    if kind == "hpc_submit":
        hpc_view = HpcSubmitView(sender.send, tm, init["profile"], init.get("sim_info", {}))
    else:
        hpc_view = HpcJobsView(sender.send, tm, init.get("jobs", []), init.get("outputs"),
                               init.get("selected", ""))
    hpc_view.build(_MAIN)

    def apply(msg: dict) -> bool:
        hpc_view.apply(msg)
        return True
    return apply


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SPECULA Studio tool window")
    parser.add_argument("--address", required=True, help="host:port of the editor")
    parser.add_argument("--kind", choices=sorted(KINDS), default="control")
    parser.add_argument("--title", default="")
    args = parser.parse_args(argv)
    default_title, width, height = KINDS[args.kind]

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

    # The editor sends what the window shows first (an "init" event).
    init = conn.recv() if conn.poll(15) else {}

    import dearpygui.dearpygui as dpg

    from . import render_scale, settings
    from .fonts import FONT_PATH
    from .theme_manager import ThemeManager

    dpg.create_context()
    tm = ThemeManager()
    tm.apply_theme()
    prefs = settings.store()
    render_scale.set_size(prefs.get("render_size", "MEDIUM"))
    # the layout follows the font size, within a full HD screen
    width = min(render_scale.px(width), MAX_SIZE[0])
    height = min(render_scale.px(height), MAX_SIZE[1])
    # the editor's font: the preferred one, else DejaVuSerif
    font = prefs.get("font_path")
    if not (font and os.path.exists(font)):
        font = FONT_PATH
    if os.path.exists(font):
        with dpg.font_registry():
            dpg.bind_font(dpg.add_font(font, render_scale.font_size()))

    with dpg.window(tag=_MAIN):
        pass
    apply = _build_view(args.kind, conn, init, tm)

    events: queue.Queue = queue.Queue()

    def receive():
        while True:
            try:
                events.put(conn.recv())
            except (EOFError, OSError):
                events.put({"type": "closed"})
                return

    threading.Thread(target=receive, daemon=True, name="editor-link").start()

    dpg.create_viewport(title=args.title or default_title, width=width, height=height)
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
                kind = msg.get("type")
                if kind in ("quit", "closed"):
                    keep = False
                elif kind == "raise":
                    # No "focus" API for the viewport: briefly keeping it on top
                    # brings it in front of the other windows.
                    dpg.set_viewport_always_top(True)
                    dpg.set_frame_callback(dpg.get_frame_count() + 2,
                                           lambda: dpg.set_viewport_always_top(False))
                else:
                    try:
                        keep = apply(msg)
                    except Exception:
                        log.exception("Cannot show the editor event %s", kind)
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
