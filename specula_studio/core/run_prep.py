"""
Preparing a SPECULA run: the files to pass, the DisplayServer block, the
command line, and reading the display-server port from SPECULA's output.

Overrides are passed to SPECULA as additional parameter files, exactly as on
the command line (``specula base.yml override1.yml ...``), so SPECULA itself
applies them — including the per-simulation ``_<N>`` keys of ``--nsimul``
runs.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shlex
import shutil

from .yaml_io import dump_yaml, strip_studio_fields

log = logging.getLogger(__name__)

DISPLAY_SERVER_NODE_NAME = "specula_studio_display_server"
DEFAULT_DISPLAY_SERVER_PORT = 5000

# Printed by recent SPECULA versions when the DisplayServer picked a port.
_MARKER_RE = re.compile(r"\[SPECULA_PORT:(\d{2,5})\]")
# The URL / port must end the line: the simulation and its display server
# write to the same output, and a line spliced from both ("...127.0.0.1:" +
# "11768.27 Hz ...") must not give a port.
_URL_RE = re.compile(r"https?://(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(\d{4,5})/?\s*$",
                     re.IGNORECASE)
_PORT_KW_RE = re.compile(
    r"(?:display[_\s]?server|socket\.?io|server|listening|started)\b.{0,80}?"
    r"\bport\s*[:=]?\s*(\d{4,5})\s*$",
    re.IGNORECASE,
)


def extract_port(line: str) -> int | None:
    """Display-server port announced in a line of SPECULA output, if any."""
    m = _MARKER_RE.search(line)
    if m:
        return int(m.group(1))
    for pattern in (_URL_RE, _PORT_KW_RE):
        m = pattern.search(line)
        if m:
            port = int(m.group(1))
            if 1024 <= port <= 65535:
                return port
    return None


def inject_display_server(params: dict, session_id: str, port: int,
                          host: str | None = None,
                          supported: set | None = None) -> str | None:
    """
    Make sure *params* contains a DisplayServer in data mode tagged with
    *session_id*.  Returns its object name (None without a SimulParams).

    *supported* is the set of constructor parameters of the installed
    DisplayServer (from the class catalog).  ``session_id`` is a recent
    addition: it is only written when SPECULA accepts it (``None`` = assume
    it does), otherwise SPECULA refuses the unknown parameter.
    """
    with_session = supported is None or "session_id" in supported

    for name, obj in params.items():
        if isinstance(obj, dict) and obj.get("class") == "DisplayServer":
            if with_session:
                obj["session_id"] = session_id
            obj.setdefault("mode", "data")
            if host:
                obj["host"] = host
            return name

    main = next((o for o in params.values()
                 if isinstance(o, dict) and o.get("class") == "SimulParams"), None)
    if main is None:
        return None
    # SimulParams.display_server would add a second (image-mode) server.
    if main.get("display_server") is True:
        del main["display_server"]

    name = DISPLAY_SERVER_NODE_NAME
    i = 1
    while name in params:
        name = f"{DISPLAY_SERVER_NODE_NAME}_{i}"
        i += 1
    block = {"class": "DisplayServer", "port": port, "mode": "data"}
    if with_session:
        block["session_id"] = session_id
    if host:
        block["host"] = host
    params[name] = block
    return name


# Keys SPECULA's Simul reads straight from the SimulParams block of the YAML
# (it fails with KeyError when they are missing), with SimulParams' defaults.
SIMUL_PARAMS_RUN_KEYS = {"root_dir": ".", "total_time": 0.1, "time_step": 0.001}


def fill_simul_params(params: dict) -> list[str]:
    """Add the keys of SIMUL_PARAMS_RUN_KEYS missing from SimulParams blocks."""
    added = []
    for name, obj in params.items():
        if isinstance(obj, dict) and obj.get("class") == "SimulParams":
            for key, default in SIMUL_PARAMS_RUN_KEYS.items():
                if key not in obj:
                    obj[key] = default
                    added.append(f"{name}.{key}={default!r}")
    return added


def write_run_files(run_dir: str, base: dict, override_paths=(), sim_name: str = "untitled",
                    session_id: str | None = None, display_port: int | None = None,
                    display_host: str | None = None,
                    display_params: set | None = None) -> list[str]:
    """
    Write the files of one run into *run_dir* and return their paths:
    the base simulation first, then copies of the override files.
    *display_params*: parameters accepted by the installed DisplayServer.
    """
    params = strip_studio_fields(copy.deepcopy(dict(base)))
    added = fill_simul_params(params)
    if added:
        log.info("Using SimulParams defaults: %s", ", ".join(added))
    if session_id is not None:
        inject_display_server(params, session_id,
                              DEFAULT_DISPLAY_SERVER_PORT if display_port is None else display_port,
                              display_host, display_params)
    base_path = os.path.join(run_dir, f"{sim_name}_simul.yml")
    with open(base_path, "w", encoding="utf-8") as f:
        f.write(dump_yaml(params))
    paths = [base_path]
    for i, src in enumerate(override_paths, start=1):
        dst = os.path.join(run_dir, f"override{i}_{os.path.basename(src)}")
        shutil.copyfile(src, dst)
        paths.append(dst)
    return paths


def specula_cli_args(files, stepping=False, nsimul=1, cpu=False, target=0,
                     precision=1, log_level="INFO", profile=False,
                     diagram=False) -> list[str]:
    """Arguments of the ``specula`` command (without the executable)."""
    args = list(files)
    if profile:
        args.append("--profile")
    if diagram:
        args.append("--diagram")
    if stepping:
        args.append("--stepping")
    args += ["--nsimul", str(nsimul)]
    if cpu:
        args.append("--cpu")
    args += ["--target", str(target), "--precision", str(precision),
             "--log-level", str(log_level)]
    return args


def remote_shell_command(remote_dir: str, cli_args: list[str]) -> str:
    """
    Command run on the remote host through ssh.  ``bash -ic`` loads the
    user's interactive environment (conda etc.) where ``specula`` lives; every
    argument is shell-quoted.
    """
    inner = f"cd {shlex.quote(remote_dir)} && specula " + " ".join(
        shlex.quote(a) for a in cli_args)
    return f"bash -ic {shlex.quote(inner)}"
