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
    Calls specula's Python API directly inside a daemon thread.
    No child process is created for the simulation itself.

    When a ``MonitorBus`` is supplied (the normal case from the GUI), the
    backend uses *direct monitoring*:
      - No ``DisplayServer`` node is injected in the YAML.
      - ``LoopControl.iter`` is monkey-patched to push live output arrays
        directly to the ``MonitorBus`` after every simulation step.
      - No Socket.IO connection is required; latency is minimal.

    When ``monitor_bus`` is None (legacy / testing), the backend falls back
    to the original ``specula.main_simul()`` call which still requires a
    ``DisplayServer`` in the YAML and a Socket.IO client connection.

    Stepping
    --------
    When stepping mode is enabled, specula's ``LoopControl.run()`` calls
    ``input()`` (which reads from ``sys.stdin``) to pause between steps.
    This backend creates an OS pipe, temporarily replaces ``sys.stdin`` with
    the read end for the duration of the simulation thread, and exposes the
    write end via ``step()``.

    Limitations
    -----------
    * specula must be installed (``pip install specula``).
    * ``abort()`` is reliable only in stepping mode.
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

import numpy as np
import yaml

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


def _extract_display_server_port_from_yaml(yaml_path: str) -> int | None:
    """
    Return the first valid DisplayServer ``port`` found in *yaml_path*, or None.
    """
    try:
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    for _node_name, node_dict in data.items():
        if not isinstance(node_dict, dict):
            continue
        if node_dict.get("class") != "DisplayServer":
            continue
        port = node_dict.get("port")
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        if 1024 <= port <= 65535:
            return port

    return None


def _extract_cpu_array(out_obj) -> np.ndarray | None:
    """
    Extract a CPU float32 numpy array from a SPECULA output data object.

    SPECULA output objects are ``BaseDataObj`` subclasses.  The actual numeric
    array can be retrieved in several ways depending on the concrete type:

    1. ``out_obj.get_value()``  — standard ``BaseDataObj`` API used by the
       MPI send path; most objects implement this.
    2. Common attribute names used by the most frequently seen data objects
       (Slopes, Pixels, Layer, generic Value wrappers, …).
    3. First numpy / cupy array attribute found by scanning instance ``__dict__``.

    The result is always a CPU ``float32`` numpy array, so it is safe to read
    from the DPG render thread without any GPU-synchronisation concerns.
    """
    # Resolve cupy lazily so the function works even when cupy is absent
    try:
        import specula as _sp
        _cp = _sp.cp
    except Exception:
        _cp = None

    arr = None

    # ── 1. Standard BaseDataObj API ──────────────────────────────────────────
    if hasattr(out_obj, "get_value"):
        try:
            v = out_obj.get_value()
            if v is not None:
                arr = v
        except Exception:
            pass

    # ── 2. Common named attributes ───────────────────────────────────────────
    if arr is None:
        for attr in (
            "slopes", "value", "values",
            "pixels", "modes", "phase", "phaseInNm",
            "commands", "residuals", "ef",
        ):
            v = getattr(out_obj, attr, None)
            if v is not None and hasattr(v, "__len__"):
                arr = v
                break

    # ── 3. Generic array scan (last resort) ──────────────────────────────────
    if arr is None:
        for attr, v in vars(out_obj).items():
            if attr.startswith("_"):
                continue
            if isinstance(v, np.ndarray) and v.ndim >= 1:
                arr = v
                break
            if _cp is not None and isinstance(v, _cp.ndarray) and v.ndim >= 1:
                arr = v
                break

    if arr is None:
        return None

    # ── Move GPU arrays to CPU ────────────────────────────────────────────────
    if _cp is not None and isinstance(arr, _cp.ndarray):
        arr = arr.get()   # cupy → numpy

    if not isinstance(arr, np.ndarray):
        try:
            arr = np.asarray(arr)
        except Exception:
            return None

    if arr.size == 0:
        return None

    return arr.astype(np.float32, copy=False)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class SimulationBackend(ABC):
    """Abstract base class for simulation execution strategies."""

    @abstractmethod
    def start(
        self,
        yaml_path: str,
        cmd_args: dict,
        append_terminal,
        on_port_found,
        on_finished,
    ) -> None:
        """Start the simulation (non-blocking)."""

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
# InProcessBackend — threaded specula with optional direct monitoring
# ---------------------------------------------------------------------------


class InProcessBackend(SimulationBackend):
    """Runs specula inside a daemon thread using its Python API.

    Direct monitoring (``monitor_bus`` is not None)
    -----------------------------------------------
    ``LoopControl.iter`` is monkey-patched before the simulation runs and
    restored in a ``finally`` block.  After every simulation step the patch
    reads the outputs of all SPECULA objects whose fully-qualified topic
    (``"{obj.name}.{output_key}"``) is subscribed in the ``MonitorBus`` and
    pushes a payload dict directly to the bus.

    The ``MonitorBus`` delivers each payload to every ``InProcessMonitor``
    that subscribed for that topic.  The monitor enqueues the payload and
    visualises it on the next DPG render frame.

    No Socket.IO, no HTTP, no ``DisplayServer``, no subprocess.

    Legacy mode (``monitor_bus`` is None)
    --------------------------------------
    Falls back to ``specula.main_simul()``.  The YAML must contain a
    ``DisplayServer`` node and the SocketIOClient must connect to it.
    """

    def __init__(self, monitor_bus=None) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._step_read_file: io.TextIOWrapper | None = None
        self._step_write_file: io.TextIOWrapper | None = None
        # MonitorBus reference — enables the direct monitoring path
        self._monitor_bus = monitor_bus

    # ------------------------------------------------------------------
    # Pipe helpers (stepping mode)
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

        if self._monitor_bus is not None:
            append_terminal(
                f"[In-Process] Direct monitoring mode — no DisplayServer needed.\n"
                f"[In-Process] specula.Simul({yaml_path!r}, "
                f"nsimul={nsimul}, cpu={cpu}, target={target}, "
                f"precision={precision}, stepping={stepping})\n"
            )
            # In direct mode there is no DisplayServer, so on_port_found is
            # never called and the Socket.IO client is left alone.
        else:
            append_terminal(
                f"[In-Process] Legacy mode — specula.main_simul({yaml_path!r}, "
                f"nsimul={nsimul}, cpu={cpu}, target={target}, "
                f"precision={precision}, stepping={stepping})\n"
            )
            # Resolve DisplayServer port from YAML so SimulationControl can
            # update the server URL / reconnection state.
            try:
                ds_port = _extract_display_server_port_from_yaml(yaml_path)
                if ds_port:
                    on_port_found(ds_port)
            except Exception:
                pass

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

            if self._monitor_bus is not None:
                # ── Direct monitoring: bypass Socket.IO ──────────────────────
                self._run_direct(
                    yaml_path, nsimul, cpu, target, precision, stepping,
                    append_terminal,
                )
            else:
                # ── Legacy: DisplayServer + Socket.IO ────────────────────────
                specula.main_simul(
                    yml_files=[yaml_path],
                    nsimul=nsimul,
                    cpu=cpu,
                    target=target,
                    precision=precision,
                    stepping=stepping,
                )

        except EOFError:
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
    # Direct monitoring path
    # ------------------------------------------------------------------

    def _run_direct(
        self,
        yaml_path: str,
        nsimul: int,
        cpu: bool,
        target: int,
        precision: int,
        stepping: bool,
        append_terminal,
    ) -> None:
        """
        Run the simulation using SPECULA's lower-level API and push output
        arrays directly to the ``MonitorBus`` after every simulation step.

        Algorithm
        ---------
        1. ``specula.init()`` initialises compute device / precision.
        2. For each simulation repetition, a ``Simul`` object is created and
           ``run()`` is called.  ``Simul.run()`` builds all processing objects
           and starts the ``LoopControl`` loop.
        3. Before each ``Simul.run()``, ``LoopControl.iter`` is replaced with
           a wrapper that (a) calls the original ``iter``, then (b) walks the
           trigger lists to build an object-registry on the first call, then
           (c) for every subscribed topic reads the corresponding output
           object, converts it to a CPU float32 array, and pushes a payload
           dict to the ``MonitorBus``.
        4. The original ``LoopControl.iter`` is restored in a ``finally``
           block so that no other code is affected.

        Topic naming
        ------------
        SPECULA sets ``obj.name`` from the YAML node name, so the topic
        ``"{obj.name}.{output_key}"`` matches the ``server_output_name`` that
        ``MonitorManager`` computes via ``get_server_output_name()``.  No
        special mapping is needed.
        """
        import specula
        from specula.loop_control import LoopControl
        from specula.simul import Simul

        target_device_idx = -1 if cpu else target
        try:
            specula.init(target_device_idx, precision=precision)
        except Exception as exc:
            append_terminal(f"[In-Process] specula.init() warning: {exc}\n")

        monitor_bus = self._monitor_bus

        # Mutable state shared with the closure (avoids 'nonlocal' for Python 3.8 compat)
        _state = {
            "registry_built": False,
            "obj_registry": {},   # topic -> (obj, output_key)
        }

        original_iter = LoopControl.iter

        def _patched_iter(loop_self):
            # ── Step the simulation ──────────────────────────────────────────
            original_iter(loop_self)

            # ── Build output registry on first call ──────────────────────────
            if not _state["registry_built"]:
                registry = _state["obj_registry"]
                for idx in loop_self.trigger_lists:
                    for obj in loop_self.trigger_lists[idx]:
                        obj_name = getattr(obj, "name", None)
                        if not obj_name:
                            continue
                        for out_key in obj.outputs:
                            topic = f"{obj_name}.{out_key}"
                            registry[topic] = (obj, out_key)
                _state["registry_built"] = True

            # ── Push subscribed outputs to the bus ───────────────────────────
            subscribed = monitor_bus.all_subscribed_outputs()
            if not subscribed:
                return

            registry = _state["obj_registry"]
            for topic in subscribed:
                entry = registry.get(topic)
                if entry is None:
                    continue
                obj, out_key = entry
                try:
                    out_obj = obj.outputs.get(out_key)
                    if out_obj is None:
                        continue
                    arr = _extract_cpu_array(out_obj)
                    if arr is None:
                        continue
                    ndim = arr.ndim
                    if ndim == 0 or (ndim == 1 and arr.size == 1):
                        dtype_str = "scalar"
                    elif ndim == 1:
                        dtype_str = "1d_array"
                    elif ndim == 2:
                        dtype_str = "2d_array"
                    else:
                        dtype_str = "nd_array"
                    payload = {
                        "type":  dtype_str,
                        "data":  arr,           # numpy array — no serialisation needed
                        "shape": list(arr.shape),
                    }
                    monitor_bus.push(topic, payload)
                except Exception:
                    pass  # never crash the simulation loop

        LoopControl.iter = _patched_iter
        try:
            for simul_idx in range(nsimul):
                # Reset the per-run state so the registry is rebuilt for each
                # Simul instance (objects may differ between repetitions).
                _state["registry_built"] = False
                _state["obj_registry"].clear()

                append_terminal(
                    f"[In-Process] Starting run {simul_idx + 1}/{nsimul} …\n"
                )
                Simul(
                    yaml_path,
                    simul_idx=simul_idx,
                    stepping=stepping,
                ).run()
        finally:
            LoopControl.iter = original_iter

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
        ``input()`` call to receive EOF and raise ``EOFError``.

        In non-stepping mode: marks ``is_running`` as False immediately.
        """
        self._running = False
        if self._step_write_file and not self._step_write_file.closed:
            try:
                self._step_write_file.close()
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running