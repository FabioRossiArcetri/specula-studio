"""
simulation_backend.py
=====================
Ways of running SPECULA from the editor.

RemoteBackend
    Runs the ``specula`` command, either locally (subprocess) or on a remote
    host (files copied with scp into a private remote directory, command run
    with ``ssh -tt`` so that aborting also stops the remote process).
    Monitoring goes through the DisplayServer injected in the YAML.

InProcessBackend
    Runs ``specula.simul.Simul`` in a daemon thread of the editor process.
    Monitors are fed directly by :class:`MonitorProbeObj` objects added to
    SPECULA's ``LoopControl`` — no DisplayServer, no sockets.

    SPECULA has no public hook for this yet, so ``LoopControl.run`` and
    ``LoopControl.iter`` are wrapped *only while a run is active* (restored
    afterwards):

    * ``run``  — probes for the monitors already open are added to the
      trigger lists before the loop starts, so they see the first step;
    * ``iter`` — probes for monitors opened later are added between steps,
      and an abort request stops the loop.

    Stepping uses SPECULA's ``--stepping`` behaviour (``input()`` between
    steps): ``sys.stdin`` is replaced by a pipe for the duration of the run
    and :meth:`step` writes newlines into it.
"""

from __future__ import annotations

import collections
import contextlib
import io
import logging
import os
import shlex
import subprocess
import sys
import threading
import traceback
import uuid
from abc import ABC, abstractmethod

from .core.run_prep import extract_port, remote_shell_command, specula_cli_args
from .monitor_data import extract_display_array, make_payload
from .sshutil import SSH_OPTIONS, explain_ssh_failure, run_command

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class SimulationBackend(ABC):

    @abstractmethod
    def start(self, yaml_paths, cmd_args, append_terminal, on_port_found, on_finished):
        """Start SPECULA on *yaml_paths* (base file first, then overrides)."""

    @abstractmethod
    def step(self, n: int = 1) -> None:
        """Advance a stepping-mode run by *n* time steps."""

    @abstractmethod
    def abort(self) -> None:
        pass

    @property
    @abstractmethod
    def is_running(self) -> bool:
        pass


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate *proc* and its children (e.g. SPECULA's DisplayServer)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception as exc:
        log.debug("Process-tree kill failed (%s); terminating the process only", exc)
        try:
            proc.terminate()
        except Exception:
            pass


def _step_line(n: int) -> str:
    """Answer to SPECULA's stepping prompt: Enter = 1 step, or a step count."""
    return "\n" if n <= 1 else f"{int(n)}\n"


def _cli_args(yaml_paths, cmd_args) -> list[str]:
    return specula_cli_args(
        yaml_paths,
        stepping=cmd_args.get("stepping", False),
        nsimul=cmd_args.get("nsimul", 1),
        cpu=cmd_args.get("cpu", False),
        target=cmd_args.get("target", 0),
        precision=cmd_args.get("precision", 1),
        log_level=cmd_args.get("log_level", "INFO"),
        profile=cmd_args.get("profile", False),
        diagram=cmd_args.get("diagram", False),
    )


# ---------------------------------------------------------------------------
# RemoteBackend (local or ssh subprocess)
# ---------------------------------------------------------------------------


class RemoteBackend(SimulationBackend):

    def __init__(self, remote_ip: str = "localhost", remote_user: str = "") -> None:
        self._process: subprocess.Popen | None = None
        self._running = False
        self.remote_ip = remote_ip.strip() if remote_ip else "localhost"
        self.remote_user = remote_user.strip() if remote_user else ""
        self._resolved_ip: str | None = None   # what the display server is reached at
        self._is_localhost = self.remote_ip in ("localhost", "127.0.0.1", "")
        self._remote_dir: str | None = None

    @property
    def ssh_target(self) -> str:
        return f"{self.remote_user}@{self.remote_ip}" if self.remote_user else self.remote_ip

    def start(self, yaml_paths, cmd_args, append_terminal, on_port_found, on_finished):
        if isinstance(yaml_paths, str):
            yaml_paths = [yaml_paths]
        self._resolved_ip = cmd_args.get("resolved_ip") or self._resolved_ip
        if self._is_localhost:
            self._start_local(yaml_paths, cmd_args, append_terminal, on_port_found, on_finished)
        else:
            threading.Thread(
                target=self._start_remote,
                args=(yaml_paths, cmd_args, append_terminal, on_port_found, on_finished),
                daemon=True, name="specula-remote-start",
            ).start()
            self._running = True

    def _spawn(self, cmd, append_terminal, on_port_found, on_finished, remote_ip):
        # Own process group / session, so abort can stop the whole tree
        # (SPECULA starts its DisplayServer as a child process).
        if os.name == "nt":
            group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            group = {"start_new_session": True}
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, text=True, bufsize=1, **group,
        )
        self._running = True
        threading.Thread(
            target=self._read_output,
            args=(append_terminal, on_port_found, on_finished, remote_ip),
            daemon=True, name="specula-output",
        ).start()

    def _start_local(self, yaml_paths, cmd_args, append_terminal, on_port_found, on_finished):
        cmd = ["specula", *_cli_args(yaml_paths, cmd_args)]
        append_terminal(f"[Local] {shlex.join(cmd)}\n")
        try:
            self._spawn(cmd, append_terminal, on_port_found, on_finished, None)
        except Exception as exc:
            append_terminal(f"[Local] Launch error: {exc}\n")
            log.exception("Could not start specula")
            on_finished()

    def _start_remote(self, yaml_paths, cmd_args, append_terminal, on_port_found, on_finished):
        try:
            remote_dir = f"/tmp/specula_studio_{uuid.uuid4().hex[:12]}"
            append_terminal(f"[Remote] Connecting to {self.ssh_target}…\n")
            mk = run_command(["ssh", *SSH_OPTIONS, self.ssh_target,
                              f"mkdir -p {shlex.quote(remote_dir)}"], timeout=60)
            if not mk.ok:
                append_terminal(f"[Remote] Cannot connect / create {remote_dir}: "
                                f"{explain_ssh_failure(mk)}\n")
                self._running = False
                on_finished()
                return
            self._remote_dir = remote_dir

            append_terminal(f"[Remote] Copying {len(yaml_paths)} file(s) to {self.remote_ip}…\n")
            scp = run_command(["scp", *SSH_OPTIONS, "-q", *yaml_paths,
                               f"{self.ssh_target}:{remote_dir}/"], timeout=600)
            if not scp.ok:
                append_terminal(f"[Remote] scp error: {explain_ssh_failure(scp)}\n")
                self._running = False
                self._cleanup_remote()
                on_finished()
                return

            remote_files = [os.path.basename(p) for p in yaml_paths]
            remote_cmd = remote_shell_command(self._remote_dir, _cli_args(remote_files, cmd_args))
            append_terminal(f"[Remote] {self.ssh_target}: {remote_cmd}\n")
            # -tt: a pseudo-terminal ties the remote process to this ssh
            # session, so terminating ssh (abort) also stops SPECULA remotely.
            self._spawn(["ssh", "-tt", *SSH_OPTIONS, self.ssh_target, remote_cmd],
                        append_terminal, on_port_found, on_finished,
                        self._resolved_ip or self.remote_ip)
        except Exception as exc:
            append_terminal(f"[Remote] Launch error: {exc}\n")
            log.exception("Could not start the remote simulation")
            self._running = False
            on_finished()

    def _read_output(self, append_terminal, on_port_found, on_finished, remote_ip):
        port_found = False
        proc = self._process
        try:
            for line in proc.stdout:
                append_terminal(line.replace("\r\n", "\n"))
                if not port_found:
                    port = extract_port(line)
                    if port:
                        port_found = True
                        on_port_found(port, self._resolved_ip or remote_ip)
        except (ValueError, OSError):
            pass          # stream closed by abort
        finally:
            proc.wait()
            self._running = False
            self._process = None
            self._cleanup_remote()
            on_finished()

    def _cleanup_remote(self, kill: bool = False) -> None:
        if self._is_localhost or not self._remote_dir:
            return
        remote_dir, self._remote_dir = self._remote_dir, None
        cmd = f"rm -rf {shlex.quote(remote_dir)}"
        if kill:
            cmd = f"pkill -f {shlex.quote(remote_dir)}; " + cmd
        target = self.ssh_target

        def cleanup():
            res = run_command(["ssh", *SSH_OPTIONS, target, cmd], timeout=60)
            if not res.ok:
                log.warning("Remote clean-up of %s on %s failed: %s", remote_dir, target,
                            explain_ssh_failure(res))

        threading.Thread(target=cleanup, daemon=True, name="remote-cleanup").start()

    def step(self, n: int = 1) -> None:
        if self._process and self._process.poll() is None:
            try:
                self._process.stdin.write(_step_line(n))
                self._process.stdin.flush()
            except Exception:
                pass

    def abort(self) -> None:
        if self._process:
            _kill_process_tree(self._process)
        self._cleanup_remote(kill=True)
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


# ---------------------------------------------------------------------------
# MonitorProbeObj
# ---------------------------------------------------------------------------


class MonitorProbeObj:
    """
    Minimal object with the ``LoopControl`` interface that publishes one
    SPECULA output to the monitor bus whenever it is refreshed.

    ``generation_time`` None or negative means "never computed": the probe
    does not fire, so monitors do not show uninitialised buffers.
    """

    # Attributes LoopControl reads on every element (no inputs, outputs or
    # MPI destinations: the probe only observes).
    remote_outputs: dict = {}
    inputs: dict = {}
    outputs: dict = {}

    def __init__(self, name: str, source_data_obj, topic: str, monitor_bus,
                 output_name: str = "") -> None:
        self.name = name
        self._source = source_data_obj
        self._topic = topic
        self._bus = monitor_bus
        self._output_name = output_name
        self.inputs_changed = False
        self._enabled = True

    # ── LoopControl interface — hot path ──────────────────────────────────────

    def check_ready(self, t) -> bool:
        if not self._enabled or not self._bus.subscriber_count(self._topic):
            self.inputs_changed = False      # nobody watching: skip the copy
            return False
        gen_time = getattr(self._source, "generation_time", None)
        self.inputs_changed = bool(gen_time is not None and gen_time >= 0 and gen_time >= t)
        return self.inputs_changed

    def trigger(self) -> None:
        if not self.inputs_changed or not self._enabled:
            return
        try:
            arr = extract_display_array(self._source, self._output_name)
            if arr is not None:
                self._bus.push(self._topic, make_payload(arr))
        except Exception as exc:
            log.warning("Probe %s: %s", self.name, exc)

    def post_trigger(self) -> None:
        self.inputs_changed = False

    # ── LoopControl interface — no-ops ────────────────────────────────────────

    def send_outputs(self, *args, **kwargs) -> None: pass
    def setup(self) -> None: pass
    def sanity_check(self) -> None: pass
    def finalize(self) -> None: pass
    def startMemUsageCount(self) -> None: pass
    def stopMemUsageCount(self) -> None: pass
    def printMemUsage(self) -> None: pass

    def disable(self) -> None:
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True


# ---------------------------------------------------------------------------
# InProcessBackend
# ---------------------------------------------------------------------------


def fresh_specula(device_idx: int, precision: int):
    """
    Import SPECULA initialised for *device_idx* / *precision*, as a new
    ``specula`` process would.

    SPECULA modules copy ``xp``, ``global_precision`` … from ``specula`` when
    they are imported, so ``specula.init()`` must run before any submodule
    import.  Submodules imported earlier in this process (by a previous run,
    or for help/tooltips) are dropped so that they are imported again after
    ``init()`` with the right device and precision.  No SPECULA object
    survives between runs, so nothing refers to the dropped modules.
    """
    import importlib

    for name in [m for m in sys.modules if m.startswith("specula.")]:
        del sys.modules[name]
    specula = importlib.import_module("specula")
    specula.init(device_idx, precision=precision)
    return specula


def _output_registry(simul) -> dict:
    """{"<object>.<output>": (data_obj, output_name)} for a built Simul."""
    registry = {}
    for obj_name, obj in getattr(simul, "objs", {}).items():
        for out_key, out_obj in (getattr(obj, "outputs", None) or {}).items():
            registry[f"{obj_name}.{out_key}"] = (out_obj, out_key)
    return registry


class _AbortRun(KeyboardInterrupt):
    """Raised inside LoopControl.iter to stop an in-process run."""


class InProcessBackend(SimulationBackend):
    """Runs SPECULA inside a daemon thread, monitors fed by probes."""

    PROBE_TOPIC_PREFIX = "_studio_probe_"

    def __init__(self, monitor_bus) -> None:
        self._monitor_bus = monitor_bus
        self._running = False
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._lock = threading.Lock()
        self._registry: dict = {}          # topic -> (data_obj, output_name)
        self._active: dict = {}            # topic -> probe in the loop
        self._pending: collections.deque = collections.deque()
        self._probe_idx: int | None = None
        self._step_read: io.TextIOWrapper | None = None
        self._step_write: io.TextIOWrapper | None = None

    # ── SimulationBackend interface ───────────────────────────────────────────

    def start(self, yaml_paths, cmd_args, append_terminal, on_port_found, on_finished):
        if isinstance(yaml_paths, str):
            yaml_paths = [yaml_paths]
        try:
            import specula  # noqa: F401
        except ImportError:
            append_terminal("[ERROR] The 'specula' package is not installed in this "
                            "environment. Install it or use the Remote mode.\n")
            on_finished()
            return
        stepping = bool(cmd_args.get("stepping", False))
        if stepping:
            read_fd, write_fd = os.pipe()
            self._step_read = open(read_fd, "r", closefd=True)  # noqa: SIM115
            self._step_write = open(write_fd, "w", buffering=1, closefd=True)  # noqa: SIM115
        self._abort.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_thread,
            args=(list(yaml_paths), cmd_args, stepping, append_terminal, on_finished),
            daemon=True, name="specula-inprocess",
        )
        self._thread.start()

    def step(self, n: int = 1) -> None:
        if self._step_write and not self._step_write.closed:
            try:
                self._step_write.write(_step_line(n))
                self._step_write.flush()
            except Exception:
                pass

    def abort(self) -> None:
        log.info("In-process abort requested")
        self._abort.set()
        self._running = False
        # Unblock a stepping run waiting in input()
        if self._step_write and not self._step_write.closed:
            try:
                self._step_write.close()
            except Exception:
                pass
        try:
            from .matplotlib_dpg_bridge import MatplotlibDPGBridge
            MatplotlibDPGBridge.close_all()
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        return self._running

    # ── probes ────────────────────────────────────────────────────────────────

    def _make_probe(self, topic: str) -> MonitorProbeObj | None:
        entry = self._registry.get(topic)
        if entry is None:
            return None
        data_obj, out_key = entry
        return MonitorProbeObj(self.PROBE_TOPIC_PREFIX + topic, data_obj, topic,
                               self._monitor_bus, out_key)

    def attach_probe(self, topic: str) -> MonitorProbeObj | None:
        """Start feeding *topic* ("object.output") to the monitor bus."""
        with self._lock:
            existing = self._active.get(topic)
            if existing is not None:
                existing.enable()
                return existing
            if not self._registry:
                return None              # not built yet: added when the loop starts
            probe = self._make_probe(topic)
            if probe is None:
                log.warning("Monitor topic '%s' does not exist in the running simulation",
                            topic)
                return None
            self._pending.append((topic, probe))
            return probe

    def detach_probe(self, probe: MonitorProbeObj) -> None:
        if probe is None:
            return
        probe.disable()
        with self._lock:
            if self._active.get(probe._topic) is probe:
                del self._active[probe._topic]

    def _add_to_loop(self, loop, topic, probe):
        if self._probe_idx is None:
            self._probe_idx = (max(loop.trigger_lists) + 1) if loop.trigger_lists else 0
        loop.trigger_lists.setdefault(self._probe_idx, []).append(probe)
        self._active[topic] = probe

    # ── run thread ────────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _loop_hooks(self, current_simul):
        """Wrap LoopControl.run / iter for the duration of one run."""
        from specula.loop_control import LoopControl

        original_run, original_iter = LoopControl.run, LoopControl.iter
        backend = self

        def run(loop_self, *args, **kwargs):
            simul = current_simul[0]
            with backend._lock:
                backend._probe_idx = None
                backend._registry = _output_registry(simul) if simul is not None else {}
                backend._active.clear()
                for topic in backend._monitor_bus.all_subscribed_outputs():
                    probe = backend._make_probe(topic)
                    if probe is not None:
                        backend._add_to_loop(loop_self, topic, probe)
            return original_run(loop_self, *args, **kwargs)

        def iter_(loop_self, *args, **kwargs):
            if backend._abort.is_set():
                raise _AbortRun()
            if backend._pending:
                with backend._lock:
                    while backend._pending:
                        topic, probe = backend._pending.popleft()
                        backend._add_to_loop(loop_self, topic, probe)
            return original_iter(loop_self, *args, **kwargs)

        LoopControl.run, LoopControl.iter = run, iter_
        try:
            yield
        finally:
            LoopControl.run, LoopControl.iter = original_run, original_iter

    def _run_thread(self, yaml_paths, cmd_args, stepping, append_terminal, on_finished):
        old_stdin = sys.stdin
        try:
            if stepping and self._step_read is not None:
                sys.stdin = self._step_read

            cpu = bool(cmd_args.get("cpu", False))
            target = -1 if cpu else int(cmd_args.get("target", 0))
            precision = int(cmd_args.get("precision", 1))
            nsimul = int(cmd_args.get("nsimul", 1))
            fresh_specula(target, precision)
            from specula.simul import Simul

            self._prepare_matplotlib()
            append_terminal(f"[In-Process] Simul({', '.join(map(repr, yaml_paths))}, "
                            f"nsimul={nsimul}, target={target}, precision={precision}, "
                            f"stepping={stepping})\n")

            current_simul = [None]
            with self._loop_hooks(current_simul):
                for simul_idx in range(nsimul):
                    if self._abort.is_set():
                        break
                    append_terminal(f"[In-Process] Run {simul_idx + 1}/{nsimul}\n")
                    simul = Simul(*yaml_paths, simul_idx=simul_idx, stepping=stepping)
                    current_simul[0] = simul
                    try:
                        simul.run()
                    finally:
                        self._cleanup_matplotlib()
        except (_AbortRun, EOFError):
            append_terminal("[In-Process] Simulation aborted.\n")
        except Exception as exc:
            append_terminal(f"[In-Process] Error: {type(exc).__name__}: {exc}\n")
            append_terminal(traceback.format_exc())
            log.exception("In-process simulation failed")
        finally:
            sys.stdin = old_stdin
            self._running = False
            with self._lock:
                self._registry = {}
                self._active.clear()
                self._pending.clear()
            for f in (self._step_read, self._step_write):
                if f is not None and not f.closed:
                    try:
                        f.close()
                    except Exception:
                        pass
            self._step_read = self._step_write = None
            on_finished()
            append_terminal("\n--- Finished (in-process) ---\n")

    # ── matplotlib (SPECULA display objects) ──────────────────────────────────

    @staticmethod
    def _prepare_matplotlib() -> None:
        try:
            from .matplotlib_dpg_bridge import MatplotlibDPGBridge
            MatplotlibDPGBridge.close_all()
            MatplotlibDPGBridge.install()
        except Exception as exc:
            log.warning("Could not install the matplotlib bridge: %s", exc)

    @staticmethod
    def _cleanup_matplotlib() -> None:
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
