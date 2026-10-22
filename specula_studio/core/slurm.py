"""
SLURM batch jobs for HPC clusters (written with CINECA Leonardo in mind).

Pure functions, no I/O: the job script, the override file that adapts a
simulation to the cluster, and parsers for the output of sbatch / squeue /
sacct.  The transport (ssh / scp) lives in :mod:`specula_studio.hpc`.

On a cluster SPECULA runs without a DisplayServer: nobody can connect to the
compute nodes, so monitors are not available; results are written by the
simulation's DataStore objects and downloaded afterwards.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field, fields

from .overrides import combine_params

# Leonardo defaults, taken from SPECULA's config/leonardo/jobScripts.
DEFAULT_MODULES = [
    "cuda/12.3",
    "openmpi/4.1.6--nvhpc--23.11",
    "nvhpc/23.11",
]

PARTITIONS = ["boost_usr_prod", "dcgp_usr_prod", "lrd_all_serial"]
QOS_CHOICES = ["normal", "boost_qos_dbg", "boost_qos_lprod", "boost_qos_bprod",
               "dcgp_qos_dbg", "dcgp_qos_lprod", "dcgp_qos_bprod"]

# name -> (partition, qos, time limit) ; limits from the Leonardo docs
PRESETS = {
    "Booster debug (30 min)": ("boost_usr_prod", "boost_qos_dbg", "00:30:00"),
    "Booster production (24 h)": ("boost_usr_prod", "normal", "24:00:00"),
    "Booster long (4 days)": ("boost_usr_prod", "boost_qos_lprod", "4-00:00:00"),
    "DCGP CPU debug (30 min)": ("dcgp_usr_prod", "dcgp_qos_dbg", "00:30:00"),
    "DCGP CPU production (24 h)": ("dcgp_usr_prod", "normal", "24:00:00"),
}

HPC_OVERRIDE_NAME = "zz_studio_hpc_override.yml"

_TIME_RE = re.compile(r"^(\d+-)?\d{1,3}:\d{2}(:\d{2})?$|^\d+$")
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")


@dataclass
class SlurmOptions:
    job_name: str = "specula"
    account: str = ""
    partition: str = "boost_usr_prod"
    qos: str = "boost_qos_dbg"
    time: str = "00:30:00"
    nodes: int = 1
    ntasks_per_node: int = 1
    cpus_per_task: int = 8
    gpus_per_node: int = 1
    modules: list = field(default_factory=lambda: list(DEFAULT_MODULES))
    setup: str = ""                 # shell lines run before specula (venv, conda...)
    extra_sbatch: str = ""          # extra "#SBATCH ..." lines / options
    # specula command line
    cpu: bool = False
    precision: int = 1
    nsimul: int = 1
    diagram: bool = False
    extra_args: str = ""

    @property
    def total_tasks(self) -> int:
        return max(1, self.nodes) * max(1, self.ntasks_per_node)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict | None) -> SlurmOptions:
        opts = cls()
        for f in fields(cls):
            if data and f.name in data:
                setattr(opts, f.name, data[f.name])
        return opts


def validate(opts: SlurmOptions) -> list[str]:
    """Problems that would make sbatch refuse the job (empty = OK)."""
    errors = []
    if not opts.account.strip():
        errors.append("The project account (--account) is required on Leonardo")
    if not _JOB_NAME_RE.match(opts.job_name or ""):
        errors.append("Job name: up to 64 letters, digits or _ . + -")
    if not _TIME_RE.match((opts.time or "").strip()):
        errors.append("Time limit must look like HH:MM:SS or D-HH:MM:SS")
    for name in ("nodes", "ntasks_per_node", "cpus_per_task"):
        if int(getattr(opts, name)) < 1:
            errors.append(f"{name.replace('_', ' ')} must be at least 1")
    if int(opts.gpus_per_node) < 0:
        errors.append("GPUs per node cannot be negative")
    if int(opts.gpus_per_node) > 4 and opts.partition.startswith("boost"):
        errors.append("Leonardo Booster nodes have 4 GPUs")
    return errors


def specula_command(opts: SlurmOptions, files) -> str:
    """The specula invocation run by srun inside the job."""
    args = []
    if opts.total_tasks > 1:
        args.append("--mpi")
    if opts.cpu:
        args.append("--cpu")
    args += ["--precision", str(opts.precision), "--nsimul", str(opts.nsimul)]
    if opts.diagram:
        args += ["--diagram-filename", f"{opts.job_name}.png"]
    if opts.extra_args.strip():
        args += shlex.split(opts.extra_args)
    cmd = "specula " + " ".join(shlex.quote(a) for a in [*args, *files])
    # As in SPECULA's Leonardo examples: SPECULA handles MPI itself.
    return f"srun --mpi=none bash -c {shlex.quote(cmd)}"


def render_script(opts: SlurmOptions, files) -> str:
    """The complete sbatch script (LF line endings)."""
    sb = [
        f"--job-name={opts.job_name}",
        f"--time={opts.time.strip()}",
        f"--nodes={opts.nodes}",
        f"--ntasks-per-node={opts.ntasks_per_node}",
        f"--cpus-per-task={opts.cpus_per_task}",
    ]
    if int(opts.gpus_per_node) > 0:
        sb.append(f"--gres=gpu:{opts.gpus_per_node}")
    sb += [
        f"--partition={opts.partition}",
        f"--qos={opts.qos}",
        f"--account={opts.account.strip()}",
        "--output=slurm-%j.out",
        "--error=slurm-%j.err",
    ]
    lines = ["#!/bin/bash", "# Generated by SPECULA Studio", ""]
    lines += [f"#SBATCH {s}" for s in sb]
    for extra in opts.extra_sbatch.splitlines():
        extra = extra.strip()
        if extra:
            lines.append(extra if extra.startswith("#SBATCH") else f"#SBATCH {extra}")
    lines.append("")
    for mod in opts.modules:
        if str(mod).strip():
            lines.append(f"module load {str(mod).strip()}")
    if opts.setup.strip():
        lines += ["", "# environment", *opts.setup.strip().splitlines()]
    lines += [
        "",
        "# no display on compute nodes",
        "export MPLBACKEND=Agg",
        "",
        'echo "SPECULA job $SLURM_JOB_ID on $(hostname) started at $(date)"',
        specula_command(opts, files),
        "status=$?",
        'echo "finished at $(date) with exit code $status"',
        "exit $status",
        "",
    ]
    return "\n".join(lines)


# ── adapting the simulation to the cluster ────────────────────────────────────

def hpc_override(base: dict, overrides: list[dict], remote_root_dir: str | None) -> dict:
    """
    An override (applied last) that adapts the simulation to batch execution:

    * ``main.root_dir`` → the calibration directory on the cluster (if given);
    * no display server (``display_server: false``, DisplayServer objects
      removed) — nobody can connect to a compute node;
    * DataStore objects with an absolute *local* ``store_dir`` write into
      ``results/<name>`` inside the job directory instead.
    """
    merged = dict(base)
    for ov in overrides:
        merged = combine_params(merged, ov)

    out: dict = {}
    remove = []
    for name, obj in merged.items():
        if not isinstance(obj, dict):
            continue
        cls = obj.get("class")
        if cls == "SimulParams":
            block: dict = {"display_server": False}
            if remote_root_dir:
                block["root_dir"] = remote_root_dir
            out[f"{name}_override"] = block
        elif cls == "DisplayServer":
            remove.append(name)
        elif cls == "DataStore":
            store = str(obj.get("store_dir", ""))
            if _is_local_absolute(store):
                out[f"{name}_override"] = {"store_dir": f"results/{name}"}
    if remove:
        out["remove"] = remove
    return out


def _is_local_absolute(path: str) -> bool:
    """Absolute paths that cannot be valid on the cluster (Windows drives,
    the user's home on this machine)."""
    if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
        return True
    return path.startswith(("/home/", "/Users/"))


def result_dirs(base: dict, overrides: list[dict], job_dir: str) -> dict:
    """{DataStore name: remote directory with its results}."""
    merged = dict(base)
    for ov in overrides:
        merged = combine_params(merged, ov)
    dirs = {}
    for name, obj in merged.items():
        if isinstance(obj, dict) and obj.get("class") == "DataStore":
            store = str(obj.get("store_dir", "") or ".")
            dirs[name] = store if store.startswith("/") else posixpath.normpath(
                posixpath.join(job_dir, store))
    return dirs


# ── parsing cluster command output ────────────────────────────────────────────

FINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
                "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}


def parse_sbatch(output: str) -> str | None:
    """Job id from ``sbatch`` output ("Submitted batch job 123456")."""
    m = re.search(r"Submitted batch job (\d+)", output)
    return m.group(1) if m else None


def parse_squeue(output: str) -> dict | None:
    """``squeue -h -j ID -o '%T|%M|%L|%R'`` → state, elapsed, left, reason."""
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 4 and parts[0]:
            return {"state": parts[0], "elapsed": parts[1], "left": parts[2],
                    "reason": parts[3]}
    return None


def parse_sacct(output: str) -> dict | None:
    """``sacct -n -X -P -j ID -o State,Elapsed,ExitCode`` → state, elapsed, exit."""
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 3 and parts[0]:
            return {"state": parts[0].split()[0], "elapsed": parts[1],
                    "exit_code": parts[2]}
    return None


def is_final(state: str) -> bool:
    return state.split()[0].rstrip("+") in FINAL_STATES if state else False
