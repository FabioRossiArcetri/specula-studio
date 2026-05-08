"""
simulation_backend.py
=====================
Pluggable simulation-execution strategies for Specula Studio.

Two concrete implementations are provided:

DisplayServerBackend
    Reproduces the original behaviour: specula is launched as a child
    process; its built-in Socket.IO DisplayServer is injected into the
    simulation YAML; monitor windows connect to it over a local HTTP port.

InProcessBackend
    Calls ``specula.main_simul()`` directly inside a daemon thread.
    No child process is created for the simulation itself; the same
    Socket.IO DisplayServer is still injected so that monitor windows can
    use the existing connection path without modification.

    Stepping is handled by routing specula's ``input()`` calls through an
    OS pipe whose write end is controlled by ``step()``.

    Requires the ``specula`` Python package to be installed.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import traceback
from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"https?://(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(\d{4,5})",
    re.IGNORECASE,
)
_PORT_KW_RE = re.compile(
    r"(?:display[_\s]?server|socket\.?io|server|running|listening|started)"
    r".{0,80}?[:\s](\d{4,5})\b",
    re.IGNORECASE,
)


def _extract_port(line: str) -> int | None:
    """Return the first valid port number found in *line*, or None."""
    for pattern in (_URL_RE, _PORT_KW_RE):
        m = pattern.search(line)
        if m:
            port = int(m.group(1))
            if 1024 <= port <= 65535:
                return port
    return None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class SimulationBackend(ABC):
    """Abstract base class for simulation execution strategies.

    Subclasses implement the three life-cycle methods (start / step / abort)
    and expose the ``is_running`` property.

    All callbacks supplied to ``start()`` are called from background threads
    and must therefore be thread-safe (they may not touch DPG directly).
    """

    @abstractmethod
    def start(
        self,
        yaml_path: str,
        cmd_args: dict,
        append_terminal,
        on_port_found,
        on_finished,
    ) -> None:
        """Start the simulation (non-blocking).

        Parameters
        ----------
        yaml_path       : Path to the prepared simulation YAML.
        cmd_args        : Dict of runtime arguments:
                          ``nsimul``    – int, number of simulation repetitions
                          ``cpu``       – bool, force CPU mode
                          ``target``    – int, GPU device index (-1 = CPU)
                          ``precision`` – int/str, 0 = double, 1 = single
                          ``log_level`` – str, DEBUG / INFO / WARNING
                          ``stepping``  – bool, enable manual stepping
                          ``run_all_mode`` – bool, disable --stepping even if
                                           the stepping checkbox is checked
        append_terminal : callable(str) – write a line to the DPG terminal.
        on_port_found   : callable(int) – called when the display-server port
                          is detected or inferred.
        on_finished     : callable()    – called when the run ends or is aborted.
        """

    @abstractmethod
    def step(self) -> None:
        """Advance one step in stepping mode (no-op if not applicable)."""

    @abstractmethod
    def abort(self) -> None:
        """Abort the running simulation."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True while the simulation is active."""


# ---------------------------------------------------------------------------
# DisplayServerBackend — subprocess (original behaviour)
# ---------------------------------------------------------------------------


class DisplayServerBackend(SimulationBackend):
    """Runs specula as a child process (original behaviour).

    The simulation YAML is expected to already contain a ``DisplayServer``
    node injected by ``SimulationControl._prepare_simulation_yaml()``.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._running = False

    # ------------------------------------------------------------------

    def start(self, yaml_path, cmd_args, append_terminal, on_port_found, on_finished):
        run_all_mode = cmd_args.get("run_all_mode", False)
        stepping     = cmd_args.get("stepping", False)
        nsimul       = cmd_args.get("nsimul", 1)
        cpu          = cmd_args.get("cpu", False)
        target       = cmd_args.get("target", -1)
        precision    = cmd_args.get("precision", "1")
        log_level    = cmd_args.get("log_level", "INFO")

        cmd = ["specula", yaml_path]
        if not run_all_mode and stepping:
            cmd.append("--stepping")
        cmd.extend(["--nsimul", str(nsimul)])
        if cpu:
            cmd.append("--cpu")
        cmd.extend(["--target", str(target)])
        cmd.extend(["--precision", str(precision)])
        cmd.extend(["--log-level", log_level])

        append_terminal(f"Executing: {' '.join(cmd)}\n")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._running = True
            threading.Thread(
                target=self._read_output,
                args=(append_terminal, on_port_found, on_finished),
                daemon=True,
            ).start()
        except Exception as exc:
            append_terminal(f"Launch Error: {exc}\n")
            on_finished()

    def _read_output(self, append_terminal, on_port_found, on_finished):
        port_found = False
        while self._process and self._process.poll() is None:
            line = self._process.stdout.readline()
            if line:
                append_terminal(line)
                if not port_found:
                    port = _extract_port(line)
                    if port:
                        port_found = True
                        on_port_found(port)
        self._running = False
        self._process = None
        on_finished()

    # ------------------------------------------------------------------

    def step(self) -> None:
        if self._process and self._process.poll() is None:
            try:
                self._process.stdin.write("\n")
                self._process.stdin.flush()
            except Exception:
                pass

    def abort(self) -> None:
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


# ---------------------------------------------------------------------------
# InProcessBackend — threaded specula
# ---------------------------------------------------------------------------


class InProcessBackend(SimulationBackend):
    """Runs specula inside a daemon thread using its Python API.

    ``specula.main_simul()`` is called directly, eliminating the overhead
    of spawning a child process for the simulation.  The DisplayServer YAML
    node is still injected (same as ``DisplayServerBackend``) so that monitor
    windows use the standard Socket.IO path without any modification.

    Stepping
    --------
    When stepping mode is enabled, specula's ``LoopControl.run()`` calls
    ``input()`` (which reads from ``sys.stdin``) to pause between steps.
    This backend creates an OS pipe, temporarily replaces ``sys.stdin`` with
    the read end for the duration of the simulation thread, and exposes the
    write end via ``step()``.

    Because ``sys.stdin`` is a process-global, the replacement is in effect
    only during the simulation thread's lifetime; the original value is
    always restored in a ``finally`` block.

    Limitations
    -----------
    * ``abort()`` is reliable only in stepping mode (it closes the pipe,
      causing ``input()`` to raise ``EOFError`` and terminate the loop).
      In non-stepping mode the simulation thread runs until specula's loop
      exits naturally; ``is_running`` is set to ``False`` immediately to keep
      the UI responsive.
    * specula must be installed (``pip install specula``).
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._step_read_file: io.TextIOWrapper | None = None
        self._step_write_file: io.TextIOWrapper | None = None

    # ------------------------------------------------------------------
    # Pipe helpers
    # ------------------------------------------------------------------

    def _make_step_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        self._step_read_file  = open(read_fd,  "r", closefd=True)   # noqa: UP015
        self._step_write_file = open(write_fd, "w", buffering=1, closefd=True)

    def _close_step_pipe(self) -> None:
        for f in (self._step_read_file, self._step_write_file):
            if f and not f.closed:
                try:
                    f.close()
                except Exception:
                    pass
        self._step_read_file  = None
        self._step_write_file = None

    # ------------------------------------------------------------------
    # SimulationBackend interface
    # ------------------------------------------------------------------

    def start(self, yaml_path, cmd_args, append_terminal, on_port_found, on_finished):
        try:
            import specula  # noqa: F401
        except ImportError:
            append_terminal(
                "[ERROR] 'specula' package not found.\n"
                "        Install it (pip install specula) or switch to\n"
                "        Display-Server mode.\n"
            )
            on_finished()
            return

        stepping  = cmd_args.get("stepping", False)
        nsimul    = cmd_args.get("nsimul", 1)
        cpu       = cmd_args.get("cpu", False)
        target    = cmd_args.get("target", -1)
        precision = cmd_args.get("precision", 1)
        try:
            precision = int(precision)
        except (TypeError, ValueError):
            precision = 1

        if stepping:
            self._make_step_pipe()

        append_terminal(
            f"[In-Process] specula.main_simul({yaml_path!r}, "
            f"nsimul={nsimul}, cpu={cpu}, target={target}, "
            f"precision={precision}, stepping={stepping})\n"
        )

        self._running = True
        self._thread = threading.Thread(
            target=self._run_thread,
            args=(
                yaml_path, nsimul, cpu, target, precision, stepping,
                append_terminal, on_port_found, on_finished,
            ),
            daemon=True,
            name="specula-inprocess",
        )
        self._thread.start()

    # ------------------------------------------------------------------

    def _run_thread(
        self,
        yaml_path, nsimul, cpu, target, precision, stepping,
        append_terminal, on_port_found, on_finished,
    ):
        old_stdin = sys.stdin
        try:
            if stepping and self._step_read_file is not None:
                sys.stdin = self._step_read_file

            import specula
            specula.main_simul(
                yml_files=[yaml_path],
                nsimul=nsimul,
                cpu=cpu,
                target=target,
                precision=precision,
                stepping=stepping,
            )
        except EOFError:
            # Pipe was closed by abort() — clean shutdown in stepping mode.
            append_terminal("[In-Process] Simulation aborted (stepping pipe closed).\n")
        except Exception as exc:
            append_terminal(f"[In-Process] Error: {exc}\n")
            traceback.print_exc()
        finally:
            sys.stdin = old_stdin
            self._running = False
            self._close_step_pipe()
            on_finished()
            append_terminal("\n--- Finished (in-process) ---\n")

    # ------------------------------------------------------------------

    def step(self) -> None:
        """Advance one step by writing a newline to the pipe."""
        if self._step_write_file and not self._step_write_file.closed:
            try:
                self._step_write_file.write("\n")
                self._step_write_file.flush()
            except Exception:
                pass

    def abort(self) -> None:
        """Abort the simulation.

        In stepping mode: closes the write end of the pipe, causing specula's
        ``input()`` call to receive EOF and raise ``EOFError``, terminating
        the loop cleanly.

        In non-stepping mode: marks ``is_running`` as False immediately so
        the UI is responsive.  The simulation thread will run until specula's
        loop exits naturally.
        """
        self._running = False
        # Close the pipe so the simulation thread unblocks from input().
        if self._step_write_file and not self._step_write_file.closed:
            try:
                self._step_write_file.close()
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running
