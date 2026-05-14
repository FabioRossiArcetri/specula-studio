"""
simulation_backend.py
=====================
Pluggable simulation-execution strategies for Specula Studio.

Three concrete implementations are provided:

RemoteBackend
    Unified backend for running specula either locally or on a remote server.
    If remote_ip is 'localhost' or '127.0.0.1', runs locally with DisplayServer.
    Otherwise, transfers the YAML file via scp and executes the simulation
    on the remote server via ssh. The remote server's DisplayServer is
    accessible from the local machine for monitoring.
    
    Supports stepping mode, SSL/SSH key authentication, and custom ports.

InProcessBackend
    Calls specula's Python API directly inside a daemon thread.
    No child process is created for the simulation itself.

    Direct monitoring via MonitorProbeObj
    --------------------------------------
    Each active InProcessMonitor is backed by a ``MonitorProbeObj`` — a
    lightweight duck-typed object that implements the minimal LoopControl
    interface without inheriting from BaseProcessingObj.

    The probe holds a direct reference to the source BaseDataObj.
    On every simulation step where that object has been refreshed
    (``source.generation_time >= current_time``), the probe extracts a CPU
    float32 numpy array and pushes a payload dict to the MonitorBus.

    Injection mechanism
    -------------------
    ``LoopControl.run`` is monkey-patched to inject probe objects into
    ``LoopControl.trigger_lists`` after ``Simul.run()`` has built the
    simulation graph but before ``LoopControl.start()`` (which calls
    ``setup()`` on all elements).  The probes are therefore set up normally
    and participate in every subsequent ``iter()`` call without any further
    patching of the hot-path iteration logic.

    For monitors opened *after* the simulation has started, a lightweight
    ``LoopControl.iter`` patch drains a thread-safe deque of pending probes
    and injects them at the start of each iteration.

    No Socket.IO, no HTTP, no ``DisplayServer``, no subprocess.

    Legacy mode (monitor_bus is None)
    ----------------------------------
    Falls back to ``specula.main_simul()``.  The YAML must contain a
    ``DisplayServer`` node and the SocketIOClient must connect to it.

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

import collections
import io
import os
import re
import socket
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

def _resolve_remote_hostname(hostname: str) -> str:
    """
    Resolve a remote hostname to its IP address.
    
    This is used to convert hostnames (like 'gandalf') that are only resolvable
    on the remote network into IP addresses that the local client can connect to.
    
    Uses SSH to run 'hostname -I' on the remote server and extract the primary IP.
    Falls back to the original hostname if resolution fails.
    
    Parameters
    ----------
    hostname : str
        The remote hostname or IP address
        
    Returns
    -------
    str
        The resolved IP address, or the original hostname if resolution fails
    """
    import subprocess
    
    # If it's already an IP address (contains dots), return as-is
    if hostname.replace(".", "").replace(":", "").isalnum():
        try:
            # Try to parse as IP to validate
            import socket as sock_module
            sock_module.inet_aton(hostname)
            return hostname  # Valid IP address
        except (sock_module.error, ValueError):
            pass
    
    # Try to resolve via SSH
    try:
        # Run 'hostname -I' on the remote server to get its IP address
        result = subprocess.run(
            ["ssh", hostname, "hostname -I"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            ips = result.stdout.strip().split()
            if ips:
                ip = ips[0]  # Take the first IP
                print(f"[REMOTE] Resolved '{hostname}' → {ip}")
                return ip
    except Exception as e:
        print(f"[REMOTE] Could not resolve '{hostname}' via SSH: {e}")
    
    # Fallback: try standard DNS resolution
    try:
        import socket as sock_module
        ip = sock_module.gethostbyname(hostname)
        print(f"[REMOTE] Resolved '{hostname}' (DNS) → {ip}")
        return ip
    except Exception as e:
        print(f"[REMOTE] Could not resolve '{hostname}' via DNS: {e}")
    
    # Final fallback: return original hostname
    print(f"[REMOTE] Warning: Could not resolve '{hostname}', using as-is")
    return hostname


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
# RemoteBackend — unified local/remote execution with DisplayServer
# ---------------------------------------------------------------------------


class RemoteBackend(SimulationBackend):
    """
    Unified backend for running specula locally or on a remote server via SSH.
    
    If remote_ip is 'localhost', '127.0.0.1', or empty, the simulation runs
    locally with DisplayServer (equivalent to old DisplayServerBackend).
    
    For remote execution:
    - Transfers YAML file to remote server via scp
    - Executes specula on remote server via ssh
    - Remote DisplayServer binds to 0.0.0.0 so it's accessible from the client
    - Supports stepping mode via ssh stdin
    
    The simulation YAML is expected to already contain a ``DisplayServer``
    node injected by ``SimulationControl._prepare_simulation_yaml()``.
    """

    def __init__(self, remote_ip: str = "localhost", remote_user: str = "") -> None:
        """
        Parameters
        ----------
        remote_ip : str
            IP address or hostname of remote server. 'localhost' or '127.0.0.1'
            means local execution. Defaults to 'localhost'.
        remote_user : str
            SSH username. If empty, current user is assumed. Defaults to ''.
        """
        self._process: subprocess.Popen | None = None
        self._running = False
        self.remote_ip = remote_ip.strip() if remote_ip else "localhost"
        self.remote_user = remote_user.strip() if remote_user else ""
        self._resolved_ip: str | None = None  # ← ADD THIS: Store resolved IP
        
        # Determine if this is local or remote execution
        self._is_localhost = self.remote_ip in ("localhost", "127.0.0.1", "")

    def set_resolved_ip(self, ip: str) -> None:
        """Store the resolved IP address for remote connections."""
        self._resolved_ip = ip
        print(f"[REMOTE] Stored resolved IP: {ip}")

    def _prepare_remote_yaml(self, yaml_path: str) -> None:
        """
        Prepare YAML for remote execution by ensuring DisplayServer binds to 0.0.0.0.
        
        When running on a remote server, the DisplayServer must bind to 0.0.0.0
        (all interfaces) so it's accessible from the local machine.
        For localhost execution, binding to 127.0.0.1 is fine.
        """
        if self._is_localhost:
            # For local execution, no changes needed
            return
        
        try:
            with open(yaml_path, encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)
            
            if not isinstance(yaml_data, dict):
                print("[REMOTE] YAML root is not a dict, skipping DisplayServer config")
                return
            
            # Find and update DisplayServer node to bind to 0.0.0.0
            for node_name, node_dict in yaml_data.items():
                if isinstance(node_dict, dict) and node_dict.get("class") == "DisplayServer":
                    old_host = node_dict.get("host", "127.0.0.1")
                    node_dict["host"] = "0.0.0.0"
                    print(
                        f"[REMOTE] Updated DisplayServer '{node_name}' binding: "
                        f"{old_host} → 0.0.0.0"
                    )
            
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False)
            
            print(f"[REMOTE] Prepared remote YAML for binding to {self.remote_ip}")
        except Exception as e:
            print(f"[REMOTE] Warning: could not prepare remote YAML: {e}")


    def start(self, yaml_path, cmd_args, append_terminal, on_port_found, on_finished):
        """Start the simulation either locally or on remote server."""
        stepping     = cmd_args.get("stepping", False)
        nsimul       = cmd_args.get("nsimul", 1)
        cpu          = cmd_args.get("cpu", False)
        target       = cmd_args.get("target", -1)
        precision    = cmd_args.get("precision", "1")
        log_level    = cmd_args.get("log_level", "INFO")
        
        # Extract resolved IP if available in cmd_args
        if "resolved_ip" in cmd_args:
            self.set_resolved_ip(cmd_args["resolved_ip"])
        
        # For remote execution, configure DisplayServer to bind to 0.0.0.0
        self._prepare_remote_yaml(yaml_path)

        if self._is_localhost:
            # ── Local execution (DisplayServer mode) ──────────────────────────
            self._start_local(
                yaml_path, stepping, nsimul, cpu, target, precision, log_level,
                append_terminal, on_port_found, on_finished
            )
        else:
            # ── Remote execution via SSH ──────────────────────────────────────
            self._start_remote(
                yaml_path, stepping, nsimul, cpu, target, precision, log_level,
                append_terminal, on_port_found, on_finished
            )


    def _start_local(self, yaml_path, stepping, nsimul, cpu, target, precision, log_level,
                     append_terminal, on_port_found, on_finished):
        """Execute simulation locally (DisplayServer mode)."""
        cmd = ["specula", yaml_path]
        if stepping:
            cmd.append("--stepping")
        cmd.extend(["--nsimul", str(nsimul)])
        if cpu:
            cmd.append("--cpu")
        cmd.extend(["--target", str(target)])
        cmd.extend(["--precision", str(precision)])
        cmd.extend(["--log-level", log_level])

        append_terminal(f"[Remote] Executing locally: {' '.join(cmd)}\n")
        print(f"[REMOTE] Local command: {' '.join(cmd)}")

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
                args=(append_terminal, on_port_found, on_finished, None),
                daemon=True,
            ).start()
        except Exception as exc:
            append_terminal(f"[Remote] Launch Error: {exc}\n")
            print(f"[REMOTE] Launch error: {exc}")
            traceback.print_exc()
            on_finished()

    def _start_remote(self, yaml_path, stepping, nsimul, cpu, target, precision, log_level,
                    append_terminal, on_port_found, on_finished):
        """Execute simulation on remote server via SSH."""
        try:
            # Note: remote_ip is already resolved by SimulationControl.start_sim()
            # No resolution needed here
            
            # ── 1. Prepare remote command ──────────────────────────────────────
            yaml_filename = os.path.basename(yaml_path)
            remote_yaml_path = f"/tmp/{yaml_filename}"
            
            cmd_parts = ['bash -ic "specula', remote_yaml_path]
            if stepping:
                cmd_parts.append("--stepping")
            cmd_parts.extend(["--nsimul", str(nsimul)])
            if cpu:
                cmd_parts.append("--cpu")
            cmd_parts.extend(["--target", str(target)])
            cmd_parts.extend(["--precision", str(precision)])
            cmd_parts.extend(["--log-level", log_level])
            remote_cmd = " ".join(cmd_parts)
            remote_cmd += '"'

            # ── 2. Copy YAML file to remote server via scp ──────────────────────
            if self.remote_user:
                remote_target = f"{self.remote_user}@{self.remote_ip}:{remote_yaml_path}"
            else:
                remote_target = f"{self.remote_ip}:{remote_yaml_path}"

            scp_cmd = ["scp", yaml_path, remote_target]

            append_terminal(f"[Remote] Copying YAML file to {self.remote_ip}…\n")
            print(f"[REMOTE] SCP command: {' '.join(scp_cmd)}")

            try:
                scp_process = subprocess.Popen(
                    scp_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                scp_stdout, scp_stderr = scp_process.communicate()
                
                if scp_process.returncode != 0:
                    error_msg = scp_stderr if scp_stderr else scp_stdout
                    append_terminal(f"[Remote] SCP Error (rc={scp_process.returncode}): {error_msg}\n")
                    print(f"[REMOTE] SCP failed: {error_msg}")
                    on_finished()
                    return

                append_terminal(f"[Remote] YAML file copied successfully.\n")
                print(f"[REMOTE] SCP completed successfully")
            except Exception as e:
                append_terminal(f"[Remote] SCP Error: {e}\n")
                print(f"[REMOTE] SCP error: {e}")
                on_finished()
                return

            # ── 3. Execute simulation on remote server via SSH ─────────────────
            if self.remote_user:
                ssh_target = f"{self.remote_user}@{self.remote_ip}"
            else:
                ssh_target = self.remote_ip

            ssh_cmd = ["ssh", "-t", ssh_target, remote_cmd]

            append_terminal(
                f"[Remote] Executing on {self.remote_ip} as {self.remote_user or 'current user'}…\n"
            )
            append_terminal(f"[Remote] Command: {remote_cmd}\n")
            print(f"[REMOTE] SSH command: {' '.join(ssh_cmd)}")


            self._process = subprocess.Popen(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._running = True
            # Pass the resolved IP (if available) to _read_output
            ip_to_use = self._resolved_ip if self._resolved_ip else self.remote_ip
            threading.Thread(
                target=self._read_output,
                args=(append_terminal, on_port_found, on_finished, ip_to_use),
                daemon=True,
            ).start()

        except Exception as exc:
            append_terminal(f"[Remote] Launch Error: {exc}\n")
            print(f"[REMOTE] Launch error: {exc}")
            traceback.print_exc()
            on_finished()

    def _read_output(self, append_terminal, on_port_found, on_finished, remote_ip):
        """Read and display output from the specula process.
        
        Parameters
        ----------
        append_terminal : callable
            Function to append text to the simulation terminal
        on_port_found : callable
            Callback when DisplayServer port is detected; receives (port, remote_ip)
        on_finished : callable
            Callback when simulation process terminates
        remote_ip : str or None
            For remote execution, the resolved IP of the remote server.
            For local execution, None.
        """
        port_found = False
        try:
            while self._process and self._process.poll() is None:
                line = self._process.stdout.readline()
                if line:
                    append_terminal(line)
                    if not port_found:
                        port = _extract_port(line)
                        if port:
                            port_found = True
                            # Use resolved IP if available, otherwise use what was passed
                            ip_to_use = self._resolved_ip if self._resolved_ip else remote_ip
                            on_port_found(port, ip_to_use)
        finally:
            self._running = False
            self._process = None
            on_finished()

    def step(self) -> None:
        """Advance one step in stepping mode by sending newline to process stdin."""
        if self._process and self._process.poll() is None:
            try:
                self._process.stdin.write("\n")
                self._process.stdin.flush()
            except Exception:
                pass

    def abort(self) -> None:
        """Abort the simulation by terminating the process."""
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


# Backward compatibility alias (old name for local DisplayServer mode)
class DisplayServerBackend(RemoteBackend):
    """
    Backward-compatibility alias for RemoteBackend with localhost.
    Reproduces the original behaviour: specula is launched as a local child
    process with DisplayServer injection.
    """
    def __init__(self) -> None:
        super().__init__(remote_ip="localhost", remote_user="")


# ---------------------------------------------------------------------------
# MonitorProbeObj — lightweight duck-typed processing node for monitoring
# ---------------------------------------------------------------------------


class MonitorProbeObj:
    """
    Lightweight SPECULA-compatible processing object for monitoring one output.

    This class is *not* a ``BaseProcessingObj`` subclass.  It deliberately
    avoids the full SPECULA I/O wiring machinery (InputValue / InputList,
    declared input/output names, CUDA-graph capture, …) so that the
    simulation management (``Simul`` / YAML) never needs to know about it.

    Instead it implements the minimal duck-typed interface that
    ``LoopControl`` requires and is injected directly into
    ``LoopControl.trigger_lists`` after the simulation graph has been built.

    Data flow
    ---------
    * ``check_ready(t)`` returns True when
      ``source_data_obj.generation_time >= t``, i.e. when the source was
      actually computed in this iteration.
    * ``trigger()`` extracts a CPU float32 array from the source via
      ``_extract_cpu_array()`` and pushes a standard payload dict to the
      ``MonitorBus``.  The bus delivers it to every ``InProcessMonitor``
      that subscribed for this topic.
    * ``post_trigger()`` resets ``inputs_changed``.
    * All other LoopControl interface methods are harmless no-ops.

    Thread safety
    -------------
    ``trigger()`` is called exclusively from the simulation thread.  The
    ``MonitorBus.push()`` call fans out to ``InProcessMonitor._on_data()``
    callbacks which enqueue the payload for the DPG main thread.

    Parameters
    ----------
    name            : Unique name string (used in log messages).
    source_data_obj : The ``BaseDataObj`` whose data is to be monitored.
    topic           : Fully-qualified topic, e.g. ``"wfs.out_slopes"``.
    monitor_bus     : ``MonitorBus`` instance that receives the payload.
    """

    def __init__(
        self,
        name: str,
        source_data_obj,
        topic: str,
        monitor_bus,
    ) -> None:
        self.name            = name
        self._source         = source_data_obj
        self._topic          = topic
        self._bus            = monitor_bus
        self.inputs_changed  = False
        self._current_time   = 0
        self._enabled        = True

    # ------------------------------------------------------------------
    # LoopControl interface — hot path
    # ------------------------------------------------------------------

    def check_ready(self, t) -> bool:
        self._current_time = t
        if not self._enabled:
            self.inputs_changed = False
            return False
        gen_time = getattr(self._source, "generation_time", None)
        if gen_time is None or gen_time < 0:
            self.inputs_changed = True
        else:
            self.inputs_changed = (gen_time >= t)
        return self.inputs_changed

    def trigger(self) -> None:
        if not self.inputs_changed or not self._enabled:
            return
        try:
            arr = _extract_cpu_array(self._source)
            if arr is None:
                return
            
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
                "data":  arr,           # CPU numpy array — no serialisation
                "shape": list(arr.shape),
            }
            self._bus.push(self._topic, payload)
        except Exception:
            pass  # never crash the simulation loop

    def post_trigger(self) -> None:
        self.inputs_changed = False

    # ------------------------------------------------------------------
    # LoopControl interface — setup / teardown (all no-ops)
    # ------------------------------------------------------------------

    def send_outputs(self, **kwargs) -> None:
        pass   # no SPECULA outputs to send

    def setup(self) -> None:
        pass

    def sanity_check(self) -> None:
        pass

    def finalize(self) -> None:
        pass

    def startMemUsageCount(self) -> None:
        pass

    def stopMemUsageCount(self) -> None:
        pass

    def printMemUsage(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Control helpers
    # ------------------------------------------------------------------

    def disable(self) -> None:
        """Disable probe — it stays in trigger_lists but does nothing."""
        self._enabled = False

    def enable(self) -> None:
        """Re-enable a previously disabled probe."""
        self._enabled = True


# ---------------------------------------------------------------------------
# InProcessBackend — threaded specula with direct probe-based monitoring
# ---------------------------------------------------------------------------


class InProcessBackend(SimulationBackend):
    """Runs specula inside a daemon thread using its Python API.

    Direct monitoring via MonitorProbeObj (``monitor_bus`` is not None)
    -------------------------------------------------------------------
    A ``LoopControl.run`` patch injects ``MonitorProbeObj`` instances into
    ``LoopControl.trigger_lists`` **after** ``Simul.run()`` has built the
    simulation graph (so probes are injected with the correct priority) and
    **before** ``LoopControl.start()`` calls ``setup()`` on all elements
    (so probes are properly initialised).

    The probes participate in every ``iter()`` call via the normal trigger
    mechanism: ``check_ready`` compares ``generation_time`` of the source
    data object against the current simulation time and returns True only
    when the source was actually computed that step.

    For monitors opened *after* the simulation has started, a minimal
    ``LoopControl.iter`` patch drains a ``collections.deque`` of pending
    probes at the beginning of each iteration and injects them with manual
    ``setup()`` calls.

    No Socket.IO, no HTTP, no ``DisplayServer``, no subprocess.

    Legacy mode (``monitor_bus`` is None)
    --------------------------------------
    Falls back to ``specula.main_simul()``.  The YAML must contain a
    ``DisplayServer`` node and the SocketIOClient must connect to it.
    """

    def __init__(self, monitor_bus=None) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._step_read_file:  io.TextIOWrapper | None = None
        self._step_write_file: io.TextIOWrapper | None = None
        # MonitorBus reference — enables the direct probe monitoring path
        self._monitor_bus = monitor_bus
        # Set by _run_direct; used by attach_probe / detach_probe
        self._probe_queue: collections.deque | None = None   # pending probes
        self._probe_state: dict | None = None                # runtime state

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
    def __init__(self, monitor_bus=None) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._step_read_file:  io.TextIOWrapper | None = None
        self._step_write_file: io.TextIOWrapper | None = None
        self._monitor_bus = monitor_bus
        self._probe_queue: collections.deque | None = None
        self._probe_state: dict | None = None
        self._matplotlib_patched = False
        self._original_exit = None
        self._abort_in_progress = False

    # ------------------------------------------------------------------
    # Matplotlib handling (prevent crashes on window close)
    # ------------------------------------------------------------------

    def _patch_matplotlib(self) -> None:
        """Patch matplotlib to prevent sys.exit() on window close while keeping windows visible."""
        if self._matplotlib_patched:
            return
        
        try:
            import sys
            import atexit
            import matplotlib
            
            # Store original sys.exit before patching
            self._original_exit = sys.exit
            
            # Create a closure to capture self
            backend_self = self
            
            # Replace sys.exit with a safe version that respects abort state
            def _safe_exit(code=0):
                print(f"[In-Process] sys.exit({code}) called, suppressing it")
                if code != 0:
                    print(f"[In-Process] (exit code was {code}, but continuing anyway)")
            
            sys.exit = _safe_exit
            
            # Clear any existing atexit handlers that might call sys.exit
            original_atexit_register = atexit.register
            
            def _safe_atexit_register(func, *args, **kwargs):
                """Only register non-exit functions"""
                func_name = getattr(func, '__name__', str(func))
                if 'exit' not in func_name.lower():
                    original_atexit_register(func, *args, **kwargs)
                else:
                    print(f"[In-Process] Skipped atexit handler: {func_name}")
                return func
            
            atexit.register = _safe_atexit_register
            
            # Now set the interactive backend
            try:
                matplotlib.use('TkAgg', force=True)
                print("[In-Process] Set matplotlib backend to 'TkAgg'")
            except Exception as e:
                print(f"[In-Process] Warning: Could not set TkAgg: {e}")
                try:
                    matplotlib.use('Qt5Agg', force=True)
                    print("[In-Process] Set matplotlib backend to 'Qt5Agg'")
                except Exception:
                    pass
            
            # Import pyplot after backend is set
            import matplotlib.pyplot as plt
            
            # Disable blocking behavior
            plt.ioff()
            print("[In-Process] Disabled matplotlib blocking mode")
            
            # Patch plt.show to not block
            original_show = plt.show
            def _patched_show(*args, **kwargs):
                kwargs['block'] = False
                try:
                    original_show(*args, **kwargs)
                except Exception as e:
                    print(f"[In-Process] Warning: plt.show() error: {e}")
            
            plt.show = _patched_show
            
            self._matplotlib_patched = True
            print("[In-Process] Matplotlib patching complete")
        except Exception as e:
            print(f"[In-Process] Warning: Could not patch matplotlib: {e}")

    def _cleanup_matplotlib(self) -> None:
        """Clean up matplotlib figures between runs."""
        try:
            import matplotlib.pyplot as plt
            plt.close('all')
            print("[In-Process] Closed all matplotlib figures")
        except Exception:
            pass

    def _restore_sys_exit(self) -> None:
        """Restore original sys.exit ONLY when truly done."""
        if self._original_exit is not None:
            import sys
            sys.exit = self._original_exit
            self._original_exit = None
            print("[In-Process] Restored original sys.exit")

    def start(self, yaml_path, cmd_args, append_terminal, on_port_found, on_finished):
        try:
            import specula  # noqa: F401
        except ImportError:
            append_terminal(
                "[ERROR] 'specula' package not found.\n"
                "        Install it (pip install specula) or switch to\n"
                "        Remote mode.\n"
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
                f"[In-Process] Direct probe-monitoring mode — no DisplayServer.\n"
                f"[In-Process] specula.Simul({yaml_path!r}, "
                f"nsimul={nsimul}, cpu={cpu}, target={target}, "
                f"precision={precision}, stepping={stepping})\n"
            )
            # In direct mode there is no DisplayServer, so on_port_found is
            # never called and the Socket.IO client is left disconnected.
        else:
            append_terminal(
                f"[In-Process] Legacy mode — specula.main_simul({yaml_path!r}, "
                f"nsimul={nsimul}, cpu={cpu}, target={target}, "
                f"precision={precision}, stepping={stepping})\n"
            )
            try:
                ds_port = _extract_display_server_port_from_yaml(yaml_path)
                if ds_port:
                    on_port_found(ds_port, None)
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
                # ── Direct probe monitoring: bypass Socket.IO ─────────────
                self._run_direct(
                    yaml_path, nsimul, cpu, target, precision, stepping,
                    append_terminal,
                )
            else:
                # ── Legacy: DisplayServer + Socket.IO ─────────────────────
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
            self._probe_queue = None
            self._probe_state = None
            self._close_step_pipe()
            on_finished()
            append_terminal("\n--- Finished (in-process) ---\n")

    # ------------------------------------------------------------------
    # Direct probe-monitoring path
    # ------------------------------------------------------------------
    def _run_direct(self, yaml_path, nsimul, cpu, target, precision, stepping, append_terminal):
        import importlib
        import sys

        # ── Patch matplotlib to prevent exit on window close ──────────────────
        self._patch_matplotlib()
        
        target_device_idx = -1 if cpu else target

        import specula
        specula.init(target_device_idx, precision=int(precision))

        # Force-reload the modules that captured specula globals at import time.
        for mod_name in ('specula.base_time_obj', 'specula.base_data_obj',
                         'specula.base_processing_obj', 'specula.loop_control',
                         'specula.simul'):
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])

        from specula.loop_control import LoopControl
        from specula.simul import Simul

        monitor_bus = self._monitor_bus

        # ── Shared mutable state ──────────────────────────────────────────────
        _pending_probes: collections.deque = collections.deque()
        _active_probes:  dict              = {}
        _abort_requested: list = [False]
        _state: dict = {
            "registry":       {},
            "loop_control":   None,
            "probe_priority": 99999,
            "active_probes":  _active_probes,
            "abort_requested": _abort_requested,
        }
        self._probe_queue = _pending_probes
        self._probe_state = _state

        original_run  = LoopControl.run
        original_iter = LoopControl.iter

        def _patched_run(lc_self, run_time, dt, t0=0, speed_report=False):
            registry: dict = {}
            for idx in sorted(lc_self.trigger_lists.keys()):
                for obj in lc_self.trigger_lists[idx]:
                    obj_name = getattr(obj, "name", None)
                    if not obj_name:
                        continue
                    for out_key, out_data_obj in getattr(obj, "outputs", {}).items():
                        topic = f"{obj_name}.{out_key}"
                        registry[topic] = out_data_obj

            _state["registry"]     = registry
            _state["loop_control"] = lc_self

            probe_priority = (
                max(lc_self.trigger_lists.keys()) + 1
                if lc_self.trigger_lists else 0
            )
            _state["probe_priority"] = probe_priority

            for topic in monitor_bus.all_subscribed_outputs():
                source = registry.get(topic)
                if source is not None and topic not in _active_probes:
                    probe = MonitorProbeObj(
                        name=f"_studio_probe_{topic}",
                        source_data_obj=source,
                        topic=topic,
                        monitor_bus=monitor_bus,
                    )
                    lc_self.trigger_lists[probe_priority].append(probe)
                    _active_probes[topic] = probe
                    append_terminal(f"[In-Process] Probe injected for '{topic}'\n")
                elif source is None:
                    append_terminal(
                        f"[In-Process] Warning: topic '{topic}' not found "
                        f"in registry — no probe created.\n"
                    )

            original_run(lc_self, run_time, dt, t0=t0, speed_report=speed_report)

        def _patched_iter(lc_self) -> None:
            if _abort_requested[0]:
                raise KeyboardInterrupt("Simulation aborted by user")
            
            while _pending_probes:
                try:
                    topic, probe = _pending_probes.popleft()
                    probe.setup()
                    priority = _state.get("probe_priority", 99999)
                    lc_self.trigger_lists[priority].append(probe)
                    _active_probes[topic] = probe
                except Exception as exc:
                    print(f"[In-Process] Dynamic probe injection error: {exc}")
            original_iter(lc_self)

        LoopControl.run  = _patched_run
        LoopControl.iter = _patched_iter

        try:
            for simul_idx in range(nsimul):
                if _abort_requested[0]:
                    append_terminal("[In-Process] Simulation aborted by user.\n")
                    break
                
                _active_probes.clear()
                _state["registry"].clear()
                _state["loop_control"] = None

                append_terminal(
                    f"[In-Process] Starting run {simul_idx + 1}/{nsimul} …\n"
                )
                
                try:
                    Simul(
                        yaml_path,                    
                        simul_idx=simul_idx,
                        stepping=stepping,
                    ).run()
                except KeyboardInterrupt as e:
                    if _abort_requested[0]:
                        append_terminal(f"[In-Process] Simulation aborted by user.\n")
                        break
                    else:
                        raise
                except Exception as e:
                    append_terminal(f"[In-Process] Error in run {simul_idx + 1}: {type(e).__name__}: {e}\n")
                    raise
                finally:
                    self._cleanup_matplotlib()
        finally:
            LoopControl.run  = original_run
            LoopControl.iter = original_iter
            # Keep sys.exit patched - don't restore it here
            # It will be restored when the thread exits naturally


    # ------------------------------------------------------------------
    # Dynamic probe management (called from the GUI thread)
    # ------------------------------------------------------------------

    def attach_probe(self, topic: str, monitor_bus) -> "MonitorProbeObj | None":
        """Create and inject a MonitorProbeObj for *topic* at runtime.

        This is called by ``MonitorManager`` when a monitor window is opened
        *after* the simulation has already started.  If the simulation has
        not started yet (or the registry is not yet available), ``None`` is
        returned; in that case the probe will be created automatically by
        ``_patched_run`` when the simulation starts, because the monitor has
        already subscribed to the bus.

        Parameters
        ----------
        topic       : Fully-qualified topic, e.g. ``"wfs.out_slopes"``.
        monitor_bus : ``MonitorBus`` that the new probe should push to.

        Returns
        -------
        MonitorProbeObj or None
        """
        if not self._running or self._probe_state is None:
            return None

        state = self._probe_state
        active = state.get("active_probes", {})

        # Return existing probe if one is already live for this topic
        existing = active.get(topic)
        if existing is not None and existing._enabled:
            return existing

        source = state.get("registry", {}).get(topic)
        if source is None:
            return None   # topic not (yet) in registry

        probe = MonitorProbeObj(
            name=f"_studio_probe_{topic}",
            source_data_obj=source,
            topic=topic,
            monitor_bus=monitor_bus,
        )

        # Queue for injection at the start of the next simulation iteration.
        if self._probe_queue is not None:
            self._probe_queue.append((topic, probe))

        return probe

    def detach_probe(self, probe: "MonitorProbeObj") -> None:
        """Disable *probe* so it no longer pushes data.

        The probe object remains in ``LoopControl.trigger_lists`` (removing
        it safely while the simulation thread is running would require extra
        locking); disabling it causes ``check_ready`` to return False
        immediately, making every subsequent call a no-op.

        Parameters
        ----------
        probe : The probe to disable, as returned by ``attach_probe``.
        """
        if probe is None:
            return
        probe.disable()
        if self._probe_state is not None:
            active = self._probe_state.get("active_probes", {})
            if active.get(probe._topic) is probe:
                del active[probe._topic]

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
        """Abort the simulation gracefully without exiting or crashing.

        Sets the abort flag which is checked at the start of each iteration
        and between runs. Does NOT restore sys.exit - that stays patched
        to prevent any exit calls from specula or matplotlib.
        """
        print("[In-Process] Abort requested - setting flag")
        self._running = False
        self._abort_in_progress = True
        
        # Set abort flag for _run_direct mode
        if self._probe_state is not None:
            abort_flag = self._probe_state.get("abort_requested")
            if abort_flag is not None:
                abort_flag[0] = True
                print("[In-Process] Abort flag set")
        
        # Close pipe for stepping mode
        if self._step_write_file and not self._step_write_file.closed:
            try:
                self._step_write_file.close()
                print("[In-Process] Step pipe closed")
            except Exception as e:
                print(f"[In-Process] Error closing pipe: {e}")
        
        print("[In-Process] Abort complete - simulation will stop gracefully")


    @property
    def is_running(self) -> bool:
        return self._running