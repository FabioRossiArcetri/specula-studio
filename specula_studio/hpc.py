"""
Running simulations as SLURM batch jobs on an HPC cluster (CINECA Leonardo).

Everything goes through the system ``ssh`` / ``scp`` commands, so the user's
OpenSSH configuration and agent apply.  On Leonardo authentication uses
short-lived (12 h) smallstep certificates::

    step ssh login <email> --provisioner cineca-hpc

loads one into the ssh agent; afterwards ssh/scp work without prompts.  All
commands run with ``BatchMode=yes``: a GUI cannot answer a password prompt,
and an expired certificate must fail quickly with a clear message.

Two hosts can be configured: the *login* host (commands: mkdir, sbatch,
squeue, …) and an optional *transfer* host for scp (Leonardo's data movers,
``data.leonardo.cineca.it``, recommended for large transfers; they have no
shell and do not define ``$WORK``, hence the absolute paths used here).
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field

from .core import slurm
from .core.run_prep import write_run_files
from .core.yaml_io import dump_yaml

log = logging.getLogger(__name__)

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
STEP_PROVISIONER = "cineca-hpc"
_ENV_VARS = ("HOME", "WORK", "CINECA_SCRATCH", "SCRATCH", "FAST", "USER")


class HpcError(RuntimeError):
    pass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(argv: list[str], timeout: float = 120, input_text: str | None = None
                ) -> CommandResult:
    """Run a local command (ssh, scp, step …) and capture its output."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              input=input_text, stdin=None if input_text is not None
                              else subprocess.DEVNULL)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:.0f} s: {argv[0]}")


def explain_ssh_failure(result: CommandResult) -> str:
    """A short, actionable explanation of a failed ssh/scp call."""
    err = (result.stderr or result.stdout).strip()
    low = err.lower()
    if "permission denied" in low or "no supported authentication" in low:
        return ("Authentication failed: on Leonardo log in first with "
                f"'step ssh login <email> --provisioner {STEP_PROVISIONER}' "
                "(certificates last 12 hours).")
    if "could not resolve" in low or "name or service not known" in low:
        return f"Unknown host: {err}"
    if "timed out" in low or "connection timed out" in low:
        return f"Connection timed out: {err}"
    if "host key verification failed" in low:
        return ("Host key verification failed: connect once with ssh from a terminal "
                "to accept (or update) the host key.")
    return err or f"exit code {result.returncode}"


def expand_remote(path: str, env: dict) -> str:
    """Replace $WORK / ${WORK} / ~ … with the values read from the cluster."""
    if path.startswith("~/") and env.get("HOME"):
        path = env["HOME"] + path[1:]

    def repl(m):
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, path)


class HpcClient:
    """ssh / scp access to one cluster."""

    def __init__(self, login_host: str, transfer_host: str = "", runner=run_command):
        self.login_host = login_host.strip()
        self.transfer_host = transfer_host.strip() or self.login_host
        self._run = runner
        self.env: dict = {}

    # ── basic commands ────────────────────────────────────────────────────────

    def ssh(self, command: str, timeout: float = 120, host: str | None = None
            ) -> CommandResult:
        return self._run(["ssh", *SSH_OPTIONS, host or self.login_host, command],
                         timeout=timeout)

    def check_connection(self, setup: str = "") -> dict:
        """
        Log in and read the environment.  Returns a dict with ``ok``,
        ``message``, ``env`` (HOME, WORK, …) and ``specula`` (path or "").
        """
        if not self.login_host:
            return {"ok": False, "message": "No login host configured", "env": {},
                    "specula": ""}
        probe = "; ".join(f'echo "{v}=${v}"' for v in _ENV_VARS)
        # sbatch is run from this same kind of (non-interactive ssh) shell and
        # the job inherits its environment, plus the script's setup lines.
        setup_lines = "; ".join(ln for ln in setup.strip().splitlines() if ln.strip())
        find = f"{{ {setup_lines}; }} >/dev/null 2>&1; command -v specula" if setup_lines \
            else "command -v specula"
        cmd = f"{probe}; echo HOSTNAME=$(hostname); echo SPECULA=$({find})"
        res = self.ssh(cmd, timeout=60)
        if not res.ok:
            return {"ok": False, "message": explain_ssh_failure(res), "env": {},
                    "specula": ""}
        env = {}
        for line in res.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                env[key.strip()] = value.strip()
        self.env = {k: v for k, v in env.items() if v and k in (*_ENV_VARS, "HOSTNAME")}
        specula_path = env.get("SPECULA", "")
        msg = f"Connected to {env.get('HOSTNAME', self.login_host)}"
        if self.env.get("WORK"):
            msg += f"; $WORK = {self.env['WORK']}"
        return {"ok": True, "message": msg, "env": dict(self.env), "specula": specula_path}

    def mkdir(self, remote_dir: str) -> None:
        res = self.ssh(f"mkdir -p {shlex.quote(remote_dir)}")
        if not res.ok:
            raise HpcError(f"mkdir {remote_dir}: {explain_ssh_failure(res)}")

    def upload(self, local_paths, remote_dir: str, recursive: bool = False,
               timeout: float = 3600) -> None:
        argv = ["scp", *SSH_OPTIONS, "-q"]
        if recursive:
            argv.append("-r")
        argv += [*local_paths, f"{self.transfer_host}:{remote_dir.rstrip('/')}/"]
        res = self._run(argv, timeout=timeout)
        if not res.ok:
            raise HpcError(f"upload to {remote_dir}: {explain_ssh_failure(res)}")

    def download(self, remote_path: str, local_dir: str, timeout: float = 3600) -> None:
        os.makedirs(local_dir, exist_ok=True)
        res = self._run(["scp", *SSH_OPTIONS, "-q", "-r",
                         f"{self.transfer_host}:{remote_path}", local_dir], timeout=timeout)
        if not res.ok:
            raise HpcError(f"download of {remote_path}: {explain_ssh_failure(res)}")

    # ── SLURM ─────────────────────────────────────────────────────────────────

    def sbatch(self, job_dir: str, script: str) -> str:
        res = self.ssh(f"cd {shlex.quote(job_dir)} && sbatch {shlex.quote(script)}")
        job_id = slurm.parse_sbatch(res.stdout)
        if not res.ok or job_id is None:
            raise HpcError(f"sbatch failed: {explain_ssh_failure(res)}")
        return job_id

    def job_status(self, job_id: str) -> dict:
        """state / elapsed / reason / exit_code of a job (queue first, then sacct)."""
        q = self.ssh(f"squeue -h -j {shlex.quote(job_id)} -o '%T|%M|%L|%R'", timeout=60)
        if not q.ok and "invalid job id" not in (q.stderr or "").lower():
            raise HpcError(explain_ssh_failure(q))
        info = slurm.parse_squeue(q.stdout) if q.ok else None
        if info:
            return info
        a = self.ssh(f"sacct -n -X -P -j {shlex.quote(job_id)} -o State,Elapsed,ExitCode",
                     timeout=60)
        info = slurm.parse_sacct(a.stdout) if a.ok else None
        return info or {"state": "UNKNOWN", "elapsed": "", "reason": "not in queue"}

    def cancel(self, job_id: str) -> None:
        res = self.ssh(f"scancel {shlex.quote(job_id)}")
        if not res.ok:
            raise HpcError(f"scancel: {explain_ssh_failure(res)}")

    def tail(self, path: str, lines: int = 200) -> str:
        res = self.ssh(f"tail -n {int(lines)} {shlex.quote(path)} 2>&1 || true", timeout=60)
        if not res.ok:
            raise HpcError(explain_ssh_failure(res))
        return res.stdout


# ── step (smallstep) login ────────────────────────────────────────────────────

def step_available() -> bool:
    return shutil.which("step") is not None


def step_login_command(email: str) -> list[str]:
    return ["step", "ssh", "login", email, "--provisioner", STEP_PROVISIONER]


# ── jobs ──────────────────────────────────────────────────────────────────────

@dataclass
class HpcJob:
    job_id: str
    name: str
    login_host: str
    transfer_host: str
    job_dir: str
    submitted: float = field(default_factory=time.time)
    state: str = "PENDING"
    elapsed: str = ""
    reason: str = ""
    exit_code: str = ""
    result_dirs: dict = field(default_factory=dict)
    download_to: str = ""            # local directory ("" = no automatic download)
    downloaded: bool = False
    last_error: str = ""

    @property
    def finished(self) -> bool:
        return slurm.is_final(self.state)

    def client(self, runner=run_command) -> HpcClient:
        return HpcClient(self.login_host, self.transfer_host, runner)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> HpcJob:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def update_job(job: HpcJob, runner=run_command) -> HpcJob:
    """Refresh *job*'s state from the cluster (errors go to ``last_error``)."""
    try:
        info = job.client(runner).job_status(job.job_id)
        job.state = info.get("state", job.state)
        job.elapsed = info.get("elapsed", job.elapsed)
        job.reason = info.get("reason", "")
        job.exit_code = info.get("exit_code", job.exit_code)
        job.last_error = ""
    except HpcError as exc:
        job.last_error = str(exc)
    return job


def download_results(job: HpcJob, local_dir: str, runner=run_command) -> str:
    """Copy the job's outputs (DataStore results + SLURM logs) into *local_dir*."""
    client = job.client(runner)
    target = os.path.join(local_dir, f"{job.name}_{job.job_id}")
    os.makedirs(target, exist_ok=True)
    for remote in job.result_dirs.values():
        client.download(remote, target)
    for log_name in (f"slurm-{job.job_id}.out", f"slurm-{job.job_id}.err"):
        try:
            client.download(posixpath.join(job.job_dir, log_name), target)
        except HpcError as exc:
            log.warning("%s", exc)
    job.downloaded = True
    return target


# ── submission ────────────────────────────────────────────────────────────────

@dataclass
class SubmitRequest:
    login_host: str
    transfer_host: str
    remote_base: str                  # e.g. $WORK/specula_studio
    sim_name: str
    base: dict                        # simulation (editor export, override-free)
    override_paths: list
    options: slurm.SlurmOptions
    calib_local_dir: str = ""         # upload this directory first ("" = no upload)
    remote_root_dir: str = ""         # root_dir on the cluster ("" = keep the file's)
    download_to: str = ""
    script_text: str | None = None    # user-edited script (None = generate)


def job_directory(remote_base: str, sim_name: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return posixpath.join(remote_base.rstrip("/"), "jobs",
                          f"{sim_name}_{stamp}_{uuid.uuid4().hex[:4]}")


def prepare_job_files(local_dir: str, req: SubmitRequest, job_dir: str
                      ) -> tuple[list[str], str, dict]:
    """
    Write the job's files into *local_dir*.  Returns (paths to upload,
    remote names passed to specula, result directories).
    """
    import yaml

    overrides = []
    for path in req.override_paths:
        with open(path, encoding="utf-8") as f:
            overrides.append(yaml.safe_load(f) or {})
    files = write_run_files(local_dir, req.base, req.override_paths, sim_name=req.sim_name)
    hpc_ov = slurm.hpc_override(req.base, overrides, req.remote_root_dir or None)
    hpc_path = os.path.join(local_dir, slurm.HPC_OVERRIDE_NAME)
    with open(hpc_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Added by SPECULA Studio for batch execution on the cluster\n")
        f.write(dump_yaml(hpc_ov))
    files.append(hpc_path)
    _to_unix_newlines(files)

    remote_names = [os.path.basename(p) for p in files]
    script = req.script_text if req.script_text is not None else \
        slurm.render_script(req.options, remote_names)
    script_path = os.path.join(local_dir, "job.slurm")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script.replace("\r\n", "\n"))
    dirs = slurm.result_dirs(req.base, [*overrides, hpc_ov], job_dir)
    return [*files, script_path], "job.slurm", dirs


def _to_unix_newlines(paths) -> None:
    for p in paths:
        with open(p, encoding="utf-8") as f:
            text = f.read()
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def submit(req: SubmitRequest, progress=lambda msg: None, runner=run_command,
           work_dir: str | None = None) -> HpcJob:
    """Upload everything and sbatch the job; *progress* receives status lines."""
    import tempfile

    errors = slurm.validate(req.options)
    if errors:
        raise HpcError("; ".join(errors))
    client = HpcClient(req.login_host, req.transfer_host, runner)

    progress(f"Connecting to {client.login_host}…")
    info = client.check_connection(req.options.setup)
    if not info["ok"]:
        raise HpcError(info["message"])
    progress(info["message"])
    if not info["specula"]:
        progress("Warning: 'specula' was not found in the login environment; "
                 "check the environment setup lines.")

    env = info["env"]
    remote_base = expand_remote(req.remote_base, env)
    job_dir = job_directory(remote_base, req.sim_name)
    remote_root = expand_remote(req.remote_root_dir, env) if req.remote_root_dir else ""
    req = SubmitRequest(**{**req.__dict__, "remote_root_dir": remote_root})

    progress(f"Creating {job_dir}")
    client.mkdir(job_dir)

    if req.calib_local_dir:
        if not remote_root:
            raise HpcError("Uploading calibration data needs a remote root_dir")
        parent = posixpath.dirname(remote_root.rstrip("/"))
        if posixpath.basename(remote_root.rstrip("/")) != os.path.basename(
                os.path.normpath(req.calib_local_dir)):
            raise HpcError("The remote calibration directory must end with the local "
                           "directory name")
        client.mkdir(parent)
        progress(f"Uploading calibration data to {remote_root} (via {client.transfer_host})…")
        client.upload([os.path.normpath(req.calib_local_dir)], parent, recursive=True)

    local_dir = work_dir or tempfile.mkdtemp(prefix="specula_studio_hpc_")
    os.makedirs(local_dir, exist_ok=True)
    paths, script_name, result_dirs = prepare_job_files(local_dir, req, job_dir)
    progress(f"Uploading {len(paths)} job file(s)…")
    client.upload(paths, job_dir)

    progress("Submitting with sbatch…")
    job_id = client.sbatch(job_dir, script_name)
    progress(f"Submitted batch job {job_id}")
    return HpcJob(job_id=job_id, name=req.options.job_name, login_host=client.login_host,
                  transfer_host=client.transfer_host, job_dir=job_dir,
                  result_dirs=result_dirs, download_to=req.download_to)
