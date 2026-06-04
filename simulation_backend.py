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

Changes vs. previous version
-----------------------------
* ``InProcessBackend.__init__`` defined exactly once (Issue 4).
* ``MonitorProbeObj.check_ready`` no longer fires on every step when
  ``generation_time`` is None/negative; those cases are treated as
  "not yet computed" (False) instead of "always ready" (Issue 5).
* ``_extract_cpu_array`` accepts an optional ``output_name`` hint and tries
  ``get_value(output_name)`` before falling back to the attribute scan, so
  objects with multiple arrays (e.g. both ``slopes`` and ``value``) return
  the correct one (Issue 10).
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
    for pattern in (_URL_RE, _PORT_KW_RE):
        m = pattern.search(line)
        if m:
            port = int(m.group(1))
            if 1024 <= port <= 65535:
                return port
    return None


def _extract_display_server_port_from_yaml(yaml_path: str) -> int | None:
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
    """Resolve a remote hostname to an IP address reachable from this machine."""
    if hostname.replace(".", "").replace(":", "").isalnum():
        try:
            import socket as sock_module
            sock_module.inet_aton(hostname)
            return hostname
        except (socket.error, ValueError):
            pass

    try:
        result = subprocess.run(
            ["ssh", hostname, "hostname -I"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ips = result.stdout.strip().split()
            if ips:
                ip = ips[0]
                print(f"[REMOTE] Resolved '{hostname}' → {ip}")
                return ip
    except Exception as e:
        print(f"[REMOTE] Could not resolve '{hostname}' via SSH: {e}")

    try:
        import socket as sock_module
        ip = sock_module.gethostbyname(hostname)
        print(f"[REMOTE] Resolved '{hostname}' (DNS) → {ip}")
        return ip
    except Exception:
        pass

    print(f"[REMOTE] Warning: Could not resolve '{hostname}', using as-is")
    return hostname


def _extract_cpu_array(out_obj, output_name: str = "") -> np.ndarray | None:
    """
    Extract a CPU float32 numpy array from a SPECULA output data object.

    Parameters
    ----------
    out_obj     : BaseDataObj subclass instance.
    output_name : The short output key (e.g. "out_slopes").  When provided,
                  it is tried as an explicit attribute name *before* the
                  generic scan, reducing the chance of returning the wrong
                  array from objects that expose multiple arrays (Issue 10).

    Strategy
    --------
    1. ``out_obj.get_value(output_name)`` — standard API with name hint.
    2. ``out_obj.get_value()``            — standard API without hint.
    3. Direct attribute access by ``output_name`` (strip ``out_`` prefix).
    4. Known common attribute names (conservative ordered list).
    5. First numpy/cupy array found in ``vars(out_obj)`` (last resort).
    """
    try:
        import specula as _sp
        _cp = _sp.cp
    except Exception:
        _cp = None

    arr = None

    # ── 1. get_value with output name hint ────────────────────────────────────
    if output_name and hasattr(out_obj, "get_value"):
        try:
            v = out_obj.get_value(output_name)
            if v is not None:
                arr = v
        except TypeError:
            pass  # get_value doesn't accept a name arg — fall through
        except Exception:
            pass

    # ── 2. get_value without hint ─────────────────────────────────────────────
    if arr is None and hasattr(out_obj, "get_value"):
        try:
            v = out_obj.get_value()
            if v is not None:
                arr = v
        except Exception:
            pass

    # ── 3. Direct attribute by output_name ────────────────────────────────────
    if arr is None and output_name:
        # e.g. "out_slopes" → try "out_slopes" then "slopes"
        for candidate in (output_name, output_name.removeprefix("out_")):
            v = getattr(out_obj, candidate, None)
            if v is not None and hasattr(v, "__len__"):
                arr = v
                break

    # ── 4. Known common attribute names ──────────────────────────────────────
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

    # ── 5. Generic array scan (last resort) ───────────────────────────────────
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

    # ── Move GPU arrays to CPU ─────────────────────────────────────────────────
    if _cp is not None and isinstance(arr, _cp.ndarray):
        arr = arr.get()

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

    @abstractmethod
    def start(self, yaml_path, cmd_args, append_terminal, on_port_found, on_finished):
        pass

    @abstractmethod
    def step(self) -> None:
        pass

    @abstractmethod
    def abort(self) -> None:
        pass

    @property
    @abstractmethod
    def is_running(self) -> bool:
        pass


# ---------------------------------------------------------------------------
# RemoteBackend
# ---------------------------------------------------------------------------


class RemoteBackend(SimulationBackend):

    def __init__(self, remote_ip: str = "localhost", remote_user: str = "") -> None:
        self._process: subprocess.Popen | None = None
        self._running  = False
        self.remote_ip = remote_ip.strip() if remote_ip else "localhost"
        self.remote_user = remote_user.strip() if remote_user else ""
        self._resolved_ip: str | None = None
        self._is_localhost = self.remote_ip in ("localhost", "127.0.0.1", "")

    def set_resolved_ip(self, ip: str) -> None:
        self._resolved_ip = ip

    def _prepare_remote_yaml(self, yaml_path: str) -> None:
        if self._is_localhost:
            return
        try:
            with open(yaml_path, encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)
            if not isinstance(yaml_data, dict):
                return
            for node_name, node_dict in yaml_data.items():
                if isinstance(node_dict, dict) and node_dict.get("class") == "DisplayServer":
                    old_host = node_dict.get("host", "127.0.0.1")
                    node_dict["host"] = "0.0.0.0"
                    print(f"[REMOTE] DisplayServer '{node_name}': {old_host} → 0.0.0.0")
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False)
        except Exception as e:
            print(f"[REMOTE] Warning: could not prepare remote YAML: {e}")

    def start(self, yaml_path, cmd_args, append_terminal, on_port_found, on_finished):
        stepping  = cmd_args.get("stepping", False)
        nsimul    = cmd_args.get("nsimul", 1)
        cpu       = cmd_args.get("cpu", False)
        target    = cmd_args.get("target", -1)
        precision = cmd_args.get("precision", "1")
        log_level = cmd_args.get("log_level", "INFO")

        if "resolved_ip" in cmd_args:
            self.set_resolved_ip(cmd_args["resolved_ip"])

        self._prepare_remote_yaml(yaml_path)

        if self._is_localhost:
            self._start_local(
                yaml_path, stepping, nsimul, cpu, target, precision, log_level,
                append_terminal, on_port_found, on_finished,
            )
        else:
            self._start_remote(
                yaml_path, stepping, nsimul, cpu, target, precision, log_level,
                append_terminal, on_port_found, on_finished,
            )

    def _start_local(
        self, yaml_path, stepping, nsimul, cpu, target, precision, log_level,
        append_terminal, on_port_found, on_finished,
    ):
        cmd = ["specula", yaml_path]
        if stepping:
            cmd.append("--stepping")
        cmd += ["--nsimul", str(nsimul)]
        if cpu:
            cmd.append("--cpu")
        cmd += ["--target", str(target), "--precision", str(precision),
                "--log-level", log_level]
        append_terminal(f"[Remote] Local: {' '.join(cmd)}\n")
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, text=True, bufsize=1,
            )
            self._running = True
            threading.Thread(
                target=self._read_output,
                args=(append_terminal, on_port_found, on_finished, None),
                daemon=True,
            ).start()
        except Exception as exc:
            append_terminal(f"[Remote] Launch Error: {exc}\n")
            traceback.print_exc()
            on_finished()

    def _start_remote(
        self, yaml_path, stepping, nsimul, cpu, target, precision, log_level,
        append_terminal, on_port_found, on_finished,
    ):
        try:
            yaml_filename    = os.path.basename(yaml_path)
            remote_yaml_path = f"/tmp/{yaml_filename}"

            cmd_parts = ['bash -ic "specula', remote_yaml_path]
            if stepping:
                cmd_parts.append("--stepping")
            cmd_parts += ["--nsimul", str(nsimul)]
            if cpu:
                cmd_parts.append("--cpu")
            cmd_parts += ["--target", str(target), "--precision", str(precision),
                          "--log-level", log_level]
            remote_cmd = " ".join(cmd_parts) + '"'

            remote_target = (
                f"{self.remote_user}@{self.remote_ip}:{remote_yaml_path}"
                if self.remote_user
                else f"{self.remote_ip}:{remote_yaml_path}"
            )
            scp_cmd = ["scp", yaml_path, remote_target]
            append_terminal(f"[Remote] Copying YAML to {self.remote_ip}…\n")
            scp = subprocess.Popen(scp_cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
            scp_out, scp_err = scp.communicate()
            if scp.returncode != 0:
                append_terminal(f"[Remote] SCP Error: {scp_err or scp_out}\n")
                on_finished()
                return
            append_terminal("[Remote] YAML copied.\n")

            ssh_target = (
                f"{self.remote_user}@{self.remote_ip}"
                if self.remote_user
                else self.remote_ip
            )
            ssh_cmd = ["ssh", "-t", ssh_target, remote_cmd]
            self._process = subprocess.Popen(
                ssh_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, text=True, bufsize=1,
            )
            self._running = True
            ip_to_use = self._resolved_ip if self._resolved_ip else self.remote_ip
            threading.Thread(
                target=self._read_output,
                args=(append_terminal, on_port_found, on_finished, ip_to_use),
                daemon=True,
            ).start()
        except Exception as exc:
            append_terminal(f"[Remote] Launch Error: {exc}\n")
            traceback.print_exc()
            on_finished()

    def _read_output(self, append_terminal, on_port_found, on_finished, remote_ip):
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
                            ip_to_use  = self._resolved_ip if self._resolved_ip else remote_ip
                            on_port_found(port, ip_to_use)
        finally:
            self._running  = False
            self._process  = None
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


class DisplayServerBackend(RemoteBackend):
    """Backward-compatibility alias."""
    def __init__(self) -> None:
        super().__init__(remote_ip="localhost", remote_user="")


# ---------------------------------------------------------------------------
# MonitorProbeObj  (Issue 5 fix)
# ---------------------------------------------------------------------------


class MonitorProbeObj:
    """
    Lightweight SPECULA-compatible processing object for monitoring one output.

    Fix (Issue 5): ``check_ready`` now returns False when ``generation_time``
    is None or negative, treating those cases as "not yet computed" rather than
    "always ready".  This prevents flooding the bus with stale/zero data during
    the simulation warm-up phase.
    """

    def __init__(
        self,
        name: str,
        source_data_obj,
        topic: str,
        monitor_bus,
        output_name: str = "",
    ) -> None:
        self.name           = name
        self._source        = source_data_obj
        self._topic         = topic
        self._bus           = monitor_bus
        self._output_name   = output_name   # hint for _extract_cpu_array (Issue 10)
        self.inputs_changed = False
        self._current_time  = 0
        self._enabled       = True

    # ── LoopControl interface — hot path ──────────────────────────────────────

    def check_ready(self, t) -> bool:
        self._current_time = t
        if not self._enabled:
            self.inputs_changed = False
            return False

        gen_time = getattr(self._source, "generation_time", None)

        # Issue 5: None or negative generation_time means the object has not
        # been computed yet in this simulation — do NOT trigger.
        if gen_time is None or gen_time < 0:
            self.inputs_changed = False
            return False

        self.inputs_changed = (gen_time >= t)
        return self.inputs_changed

    def trigger(self) -> None:
        if not self.inputs_changed or not self._enabled:
            return
        try:
            arr = _extract_cpu_array(self._source, self._output_name)
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
                "data":  arr,
                "shape": list(arr.shape),
            }
            
            # FIX: Add diagnostic logging
            print(f"[PROBE] {self.name}: Pushing {dtype_str} to topic '{self._topic}', shape={payload['shape']}")
            
            self._bus.push(self._topic, payload)
        except Exception as e:
            print(f"[PROBE] {self.name}: Error in trigger: {e}")
            traceback.print_exc()

    def post_trigger(self) -> None:
        self.inputs_changed = False

    # ── LoopControl interface — setup / teardown (no-ops) ─────────────────────

    def send_outputs(self, **kwargs) -> None: pass
    def setup(self)         -> None: pass
    def sanity_check(self)  -> None: pass
    def finalize(self)      -> None: pass
    def startMemUsageCount(self) -> None: pass
    def stopMemUsageCount(self)  -> None: pass
    def printMemUsage(self)      -> None: pass

    def disable(self) -> None:
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True


# ---------------------------------------------------------------------------
# InProcessBackend  (Issue 4 fix: single __init__)
# ---------------------------------------------------------------------------


class InProcessBackend(SimulationBackend):
    """Runs specula inside a daemon thread using its Python API."""

    def __init__(self, monitor_bus=None) -> None:
        # ── All instance attributes defined exactly once ───────────────────────
        self._running                = False
        self._thread: threading.Thread | None = None

        # Stepping-mode pipe
        self._step_read_file:  io.TextIOWrapper | None = None
        self._step_write_file: io.TextIOWrapper | None = None

        # Direct probe-monitoring (Issue 4: was defined twice)
        self._monitor_bus   = monitor_bus
        self._probe_queue:  collections.deque | None = None
        self._probe_state:  dict | None              = None

        # Matplotlib bridge
        self._matplotlib_patched   = False
        self._abort_in_progress    = False

    # ── Pipe helpers ──────────────────────────────────────────────────────────

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

    # ── Matplotlib ────────────────────────────────────────────────────────────

    def _patch_matplotlib(self) -> None:
        if self._matplotlib_patched:
            return
        try:
            from matplotlib_dpg_bridge import MatplotlibDPGBridge
            MatplotlibDPGBridge.install()
            self._matplotlib_patched = True
        except Exception as exc:
            print(f"[In-Process] Warning: could not install matplotlib bridge: {exc}")

    def _cleanup_matplotlib(self) -> None:
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass

    def _restore_sys_exit(self) -> None:
        pass  # kept for call-site compatibility

    # ── SimulationBackend interface ───────────────────────────────────────────

    def start(self, yaml_path, cmd_args, append_terminal, on_port_found, on_finished):
        try:
            import specula  # noqa: F401
        except ImportError:
            append_terminal(
                "[ERROR] 'specula' package not found.\n"
                "        Install it (pip install specula) or switch to Remote mode.\n"
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
        else:
            append_terminal(
                f"[In-Process] Legacy mode — specula.main_simul({yaml_path!r}, …)\n"
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
                self._run_direct(
                    yaml_path, nsimul, cpu, target, precision, stepping,
                    append_terminal,
                )
            else:
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
            sys.stdin      = old_stdin
            self._running  = False
            self._probe_queue = None
            self._probe_state = None
            self._close_step_pipe()
            on_finished()
            append_terminal("\n--- Finished (in-process) ---\n")

    # ── Direct probe-monitoring path ──────────────────────────────────────────

    def _run_direct(
        self, yaml_path, nsimul, cpu, target, precision, stepping, append_terminal
    ):
        import importlib

        try:
            from matplotlib_dpg_bridge import MatplotlibDPGBridge as _Bridge
            _Bridge.close_all()
        except Exception:
            pass
        try:
            import matplotlib.pyplot as _plt
            _plt.close("all")
        except Exception:
            pass

        self._patch_matplotlib()

        target_device_idx = -1 if cpu else target

        import specula
        specula.init(target_device_idx, precision=int(precision))

        for mod_name in (
            "specula.base_time_obj", "specula.base_data_obj",
            "specula.base_processing_obj", "specula.loop_control", "specula.simul",
        ):
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])

        # FIX: Import AFTER reloading modules, and IMMEDIATELY patch before using
        from specula.loop_control import LoopControl
        from specula.simul import Simul

        monitor_bus = self._monitor_bus

        _pending_probes:  collections.deque = collections.deque()
        _active_probes:   dict              = {}
        _abort_requested: list              = [False]
        _state: dict = {
            "registry":        {},
            "loop_control":    None,
            "probe_priority":  99999,
            "active_probes":   _active_probes,
            "abort_requested": _abort_requested,
        }
        self._probe_queue = _pending_probes
        self._probe_state = _state

        # Save original methods IMMEDIATELY after import
        original_run  = LoopControl.run
        original_iter = LoopControl.iter
        original_simul_init = Simul.__init__

        def _build_registry_from_lc(lc_self):
            """Build the output registry from the LoopControl's trigger_lists."""
            registry: dict = {}
            for idx in sorted(lc_self.trigger_lists.keys()):
                for obj in lc_self.trigger_lists[idx]:
                    obj_name = getattr(obj, "name", None)
                    if not obj_name:
                        continue
                    outputs = getattr(obj, "outputs", {})
                    if not outputs:
                        continue
                    for out_key, out_data_obj in outputs.items():
                        topic = f"{obj_name}.{out_key}"
                        registry[topic] = (out_data_obj, out_key)
            return registry

        def _inject_probes_for_subscriptions(lc_self, registry):
            """Inject probes for all currently subscribed monitor topics."""
            subscribed = monitor_bus.all_subscribed_outputs()
            append_terminal(f"[In-Process] Subscribed topics: {subscribed}\n")
            append_terminal(f"[In-Process] Available registry: {sorted(registry.keys())}\n")
            
            # FIX: Use a priority that WILL be executed
            # Find the maximum priority in use
            max_priority = max(lc_self.trigger_lists.keys()) if lc_self.trigger_lists else 0
            probe_priority = max_priority + 1
            
            append_terminal(f"[In-Process] Using probe_priority={probe_priority}, trigger_lists keys={sorted(lc_self.trigger_lists.keys())}\n")
            
            # Ensure the priority list exists
            if probe_priority not in lc_self.trigger_lists:
                lc_self.trigger_lists[probe_priority] = []
                append_terminal(f"[In-Process] Created new trigger_lists[{probe_priority}]\n")
            
            _state["probe_priority"] = probe_priority

            for topic in subscribed:
                entry = registry.get(topic)
                if entry is not None and topic not in _active_probes:
                    source_obj, out_key = entry
                    probe = MonitorProbeObj(
                        name=f"_studio_probe_{topic}",
                        source_data_obj=source_obj,
                        topic=topic,
                        monitor_bus=monitor_bus,
                        output_name=out_key,
                    )
                    
                    # FIX: Call setup() on the probe before adding it
                    try:
                        probe.setup()
                    except Exception as e:
                        append_terminal(f"[In-Process] Warning: probe.setup() failed: {e}\n")
                    
                    lc_self.trigger_lists[probe_priority].append(probe)
                    _active_probes[topic] = probe
                    append_terminal(f"[In-Process] ✓ Probe injected for '{topic}' at priority {probe_priority}\n")
                elif entry is None:
                    append_terminal(
                        f"[In-Process] ✗ ERROR: topic '{topic}' NOT in registry\n"
                        f"[In-Process]       Available: {sorted(registry.keys())}\n"
                    )

        def _patched_simul_init(self, *args, **kwargs):
            """FIX: Simul.__init__ - just call original, probes injected in iter."""
            original_simul_init(self, *args, **kwargs)

        def _patched_run(lc_self, run_time, dt, t0=0, speed_report=False):
            """Run loop - probes will be injected on first iter() call."""
            append_terminal(f"[In-Process] >>> LoopControl.run called, starting iterations\n")
            original_run(lc_self, run_time, dt, t0=t0, speed_report=speed_report)

        def _build_registry_from_simul_objs(simul_obj):
            """Build registry from Simul.objs dict instead of LoopControl.trigger_lists."""
            registry: dict = {}
            objs_dict = getattr(simul_obj, "objs", {})
            append_terminal(f"[In-Process] >>> Building registry from Simul.objs ({len(objs_dict)} objects)\n")
            
            for obj_name, obj in objs_dict.items():
                outputs = getattr(obj, "outputs", {})
                if outputs:
                    append_terminal(f"[In-Process]       - {obj_name}: {list(outputs.keys())}\n")
                    for out_key, out_data_obj in outputs.items():
                        topic = f"{obj_name}.{out_key}"
                        registry[topic] = (out_data_obj, out_key)
            
            return registry

        _probes_injected = [False]
        _iter_count = [0]
        _simul_obj = [None]

        def _patched_simul_init(self, *args, **kwargs):
            """FIX: Simul.__init__ - store reference to self in state for attach_probe."""
            original_simul_init(self, *args, **kwargs)
            _simul_obj[0] = self
            _state["simul"] = self  # FIX: Store in state so attach_probe can access it

        def _patched_iter(lc_self) -> None:
            """FIX: Inject probes after first iteration when Simul.objs is populated."""
            _iter_count[0] += 1
            
            if _abort_requested[0]:
                raise KeyboardInterrupt("Simulation aborted by user")
            
            original_iter(lc_self)
            
            # After first iteration, build registry from Simul.objs
            if not _probes_injected[0] and _iter_count[0] == 1:
                simul = _simul_obj[0]
                if simul is not None:
                    append_terminal(f"[In-Process] >>> After first iteration - building registry from Simul.objs\n")
                    
                    # Build registry from Simul.objs
                    registry = _build_registry_from_simul_objs(simul)
                    append_terminal(f"[In-Process] >>> Built registry with {len(registry)} topics\n")
                    _state["registry"] = registry
                    _state["loop_control"] = lc_self
                    
                    # Inject probes for currently subscribed monitors
                    _inject_probes_for_subscriptions(lc_self, registry)
                    _probes_injected[0] = True
            
            # Drain pending probes (added via attach_probe)
            while _pending_probes:
                try:
                    topic, probe = _pending_probes.popleft()
                    try:
                        probe.setup()
                    except Exception as e:
                        print(f"[In-Process] Warning: probe.setup() failed: {e}")
                    priority = _state.get("probe_priority", 99999)
                    lc_self.trigger_lists[priority].append(probe)
                    _active_probes[topic] = probe
                    print(f"[PROBE] Probe for '{topic}' injected into trigger_lists[{priority}]")
                except Exception as exc:
                    print(f"[In-Process] Dynamic probe injection error: {exc}")

        # FIX: PATCH IMMEDIATELY after defining the function
        append_terminal(f"[In-Process] Patching Simul.__init__...\n")
        Simul.__init__ = _patched_simul_init
        append_terminal(f"[In-Process] Patching LoopControl.run...\n")
        LoopControl.run  = _patched_run
        append_terminal(f"[In-Process] Patching LoopControl.iter...\n")
        LoopControl.iter = _patched_iter

        try:
            for simul_idx in range(nsimul):
                if _abort_requested[0]:
                    append_terminal("[In-Process] Simulation aborted.\n")
                    break

                _active_probes.clear()
                _state["registry"].clear()
                _state["loop_control"] = None

                append_terminal(f"[In-Process] Starting run {simul_idx + 1}/{nsimul} …\n")

                try:
                    append_terminal(f"[In-Process] Creating Simul object...\n")
                    Simul(yaml_path, simul_idx=simul_idx, stepping=stepping).run()
                except KeyboardInterrupt:
                    if _abort_requested[0]:
                        append_terminal("[In-Process] Simulation aborted.\n")
                        break
                    else:
                        raise
                except Exception as e:
                    append_terminal(
                        f"[In-Process] Error in run {simul_idx + 1}: "
                        f"{type(e).__name__}: {e}\n"
                    )
                    raise
                finally:
                    self._cleanup_matplotlib()
        finally:
            Simul.__init__ = original_simul_init
            LoopControl.run  = original_run
            LoopControl.iter = original_iter

    # ── Dynamic probe management ──────────────────────────────────────────────

    def attach_probe(self, topic: str, monitor_bus) -> "MonitorProbeObj | None":
        """Attach a probe for a specific topic.
        
        FIX: Build registry on-demand from Simul.objs if not yet available.
        """
        if not self._running or self._probe_state is None:
            return None

        state = self._probe_state
        active = state.get("active_probes", {})

        existing = active.get(topic)
        if existing is not None and existing._enabled:
            return existing

        # Get or build registry
        registry = state.get("registry", {})
        if not registry:
            # Registry not built yet - try to build from Simul object
            simul = state.get("simul")
            if simul is not None:
                print(f"[In-Process] attach_probe: Building registry on-demand from Simul.objs")
                objs_dict = getattr(simul, "objs", {})
                for obj_name, obj in objs_dict.items():
                    outputs = getattr(obj, "outputs", {})
                    for out_key, out_data_obj in outputs.items():
                        topic_key = f"{obj_name}.{out_key}"
                        registry[topic_key] = (out_data_obj, out_key)
                state["registry"] = registry
                print(f"[In-Process] attach_probe: Built registry with {len(registry)} topics")

        entry = registry.get(topic)
        if entry is None:
            print(f"[In-Process] attach_probe: topic '{topic}' not found in registry")
            return None

        source_obj, out_key = entry
        probe = MonitorProbeObj(
            name=f"_studio_probe_{topic}",
            source_data_obj=source_obj,
            topic=topic,
            monitor_bus=monitor_bus,
            output_name=out_key,
        )

        # Queue for injection on next iteration
        if self._probe_queue is not None:
            self._probe_queue.append((topic, probe))
            print(f"[In-Process] attach_probe: queued probe for '{topic}'")

        return probe
    
    def detach_probe(self, probe: "MonitorProbeObj") -> None:
        """Detach and disable a probe."""
        if probe is None:
            return
        probe.disable()
        if self._probe_state is not None:
            active = self._probe_state.get("active_probes", {})
            if active.get(probe._topic) is probe:
                del active[probe._topic]

    # ── Stepping / abort ──────────────────────────────────────────────────────

    def step(self) -> None:
        if self._step_write_file and not self._step_write_file.closed:
            try:
                self._step_write_file.write("\n")
                self._step_write_file.flush()
            except Exception:
                pass

    def abort(self) -> None:
        print("[In-Process] Abort requested")
        self._running            = False
        self._abort_in_progress  = True

        if self._probe_state is not None:
            flag = self._probe_state.get("abort_requested")
            if flag is not None:
                flag[0] = True

        if self._step_write_file and not self._step_write_file.closed:
            try:
                self._step_write_file.close()
            except Exception:
                pass

        try:
            from matplotlib_dpg_bridge import MatplotlibDPGBridge
            MatplotlibDPGBridge.close_all()
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        return self._running