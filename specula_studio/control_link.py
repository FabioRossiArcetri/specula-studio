"""
Link between the editor and its tool windows in separate processes.

DearPyGui has one OS window per process, so the simulation control panel
and the HPC windows shown "in their own window" are separate processes
(:mod:`specula_studio.control_window`, one per window).  The work still
happens in the editor (simulations, probes and monitors, ssh to the
cluster, the settings file); a window is a remote control:

* window → editor: commands, e.g. ``start {options}``, ``step {n}``,
  ``abort`` for the control panel (see :mod:`.ui.hpc_views` for the HPC
  windows);
* editor → window: events ``init`` (first), view updates, ``raise``,
  ``quit``.

Messages are plain dicts over :mod:`multiprocessing.connection` on
127.0.0.1, authenticated with a random key passed to the child process in
its environment (never on the command line).
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from typing import Any

log = logging.getLogger(__name__)

KEY_ENV = "SPECULA_STUDIO_CONTROL_KEY"


class ControlServer:
    """Editor side of one tool window: accepts one connection at a time."""

    def __init__(self, on_connect=None):
        self.authkey = secrets.token_bytes(32)
        self._listener = Listener(("127.0.0.1", 0), authkey=self.authkey)
        self.address = self._listener.address
        self.commands: queue.Queue = queue.Queue()
        self.on_connect = on_connect
        self._conn: Any = None
        self._send_lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._accept_loop, daemon=True,
                         name="tool-window-link").start()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _accept_loop(self):
        while not self._closed:
            try:
                conn = self._listener.accept()
            except Exception as exc:              # closed listener / failed auth
                if self._closed:
                    return
                log.warning("Tool window connection refused: %s", exc)
                continue
            with self._send_lock:
                old, self._conn = self._conn, conn
            if old is not None:
                old.close()
            if self.on_connect is not None:
                try:
                    self.on_connect()
                except Exception:
                    log.exception("Tool window initialisation failed")
            self._receive(conn)

    def _receive(self, conn) -> None:
        while not self._closed:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                break
            if isinstance(msg, dict) and "type" in msg:
                self.commands.put(msg)
        with self._send_lock:
            if self._conn is conn:
                self._conn = None
        log.info("Tool window disconnected")

    def send(self, msg: dict) -> bool:
        with self._send_lock:
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.send(msg)
                return True
            except (OSError, EOFError, ValueError):
                self._conn = None
                return False

    def close(self, timeout: float = 1.0):
        # Ask the window to quit and give it a moment to disconnect: closing
        # the socket with the message still unread would reset the connection
        # (on Windows the pending message is then lost).
        if self.send({"type": "quit"}):
            deadline = time.monotonic() + timeout
            while self.connected and time.monotonic() < deadline:
                time.sleep(0.02)
        self._closed = True
        with self._send_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        try:
            self._listener.close()
        except Exception:
            pass


def connect(address, authkey: bytes):
    """Window side: connect to the editor."""
    return Client(tuple(address), authkey=authkey)


class WindowProcess:
    """Launches (once) the tool window process of *kind* for a :class:`ControlServer`.

    *kind* selects the view (see ``control_window.KINDS``).
    """

    def __init__(self, server: ControlServer, kind: str, title: str):
        self.server = server
        self.kind = kind
        self.title = title
        self.process: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def launch(self) -> None:
        if self.running:
            self.server.send({"type": "raise"})
            return
        from .monitor_manager import _subprocess_env

        env = _subprocess_env()
        env[KEY_ENV] = self.server.authkey.hex()
        host, port = self.server.address
        self.process = subprocess.Popen(
            [sys.executable, "-m", "specula_studio.control_window",
             "--address", f"{host}:{port}", "--kind", self.kind, "--title", self.title],
            env=env, cwd=os.getcwd(),
        )
        log.info("%s window started (pid %s)", self.title, self.process.pid)

    def stop(self, timeout: float = 3.0) -> None:
        if not self.running or self.process is None:
            return
        self.server.send({"type": "quit"})
        try:
            self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
