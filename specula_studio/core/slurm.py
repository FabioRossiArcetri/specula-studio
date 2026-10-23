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

# Leonardo: the CUDA 12 libraries CuPy (cupy-cuda12x) loads at run time.
# cuda/12.2 is the version Leonardo's own MPI builds use, so the compute nodes'
# driver supports it.  MPI runs (more than one task, specula --mpi) also need
# mpi4py and an MPI module, e.g. openmpi/4.1.6--gcc--12.2.0-cuda-12.2.
DEFAULT_MODULES = [
    "cuda/12.2",
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


def safe_name(name: str) -> str:
    """*name* usable in file and job directory names."""
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", name.strip()) or "untitled"


def run_file_names(sim_name: str, override_paths) -> list[str]:
    """Names of the files passed to specula, as ``run_prep.write_run_files``
    writes them, followed by the cluster override (applied last)."""
    names = [f"{sim_name}_simul.yml"]
    names += [f"override{i}_{_basename(p)}" for i, p in enumerate(override_paths, 1)]
    return [*names, HPC_OVERRIDE_NAME]


def _basename(path: str) -> str:
    return re.split(r"[\\/]", str(path))[-1]


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


# ── reading an existing job script ────────────────────────────────────────────

_SBATCH_FIELDS = {"job-name": "job_name", "account": "account", "partition": "partition",
                  "qos": "qos", "time": "time", "nodes": "nodes",
                  "ntasks-per-node": "ntasks_per_node", "cpus-per-task": "cpus_per_task",
                  "gpus-per-node": "gpus_per_node"}
_SBATCH_SHORT = {"-J": "job-name", "-A": "account", "-p": "partition", "-q": "qos",
                 "-t": "time", "-N": "nodes", "-c": "cpus-per-task", "-n": "ntasks",
                 "-o": "output", "-e": "error"}
_INT_FIELDS = ("nodes", "ntasks_per_node", "cpus_per_task", "gpus_per_node")
# lines render_script writes around the specula command
_BOILERPLATE = {"export MPLBACKEND=Agg", "status=$?", "exit $status",
                'echo "SPECULA job $SLURM_JOB_ID on $(hostname) started at $(date)"',
                'echo "finished at $(date) with exit code $status"'}


@dataclass
class ParsedScript:
    """What :func:`parse_script` understood of a job script."""
    options: dict = field(default_factory=dict)   # SlurmOptions fields found
    files: list = field(default_factory=list)      # YAML files of the specula command
    found_specula: bool = False
    notes: list = field(default_factory=list)      # what was not kept, and why


def parse_script(text: str) -> ParsedScript:
    """
    Read a SLURM job script into :class:`SlurmOptions` fields and the list of
    YAML files given to specula.

    ``#SBATCH`` options go to their fields (unknown ones to *extra_sbatch*),
    ``module load`` lines to *modules*, other shell lines before the specula
    command to *setup*; the specula command may be wrapped in ``srun``,
    ``mpirun`` and ``bash -c '...'``.  ``--output``/``--error``, ``module
    purge`` and lines after the specula command are dropped (noted).
    """
    out = ParsedScript()
    extra_sbatch: list[str] = []
    setup: list[str] = []
    modules: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#SBATCH"):
            _parse_sbatch(line, out, extra_sbatch)
            continue
        if line.startswith("#") or line in _BOILERPLATE:
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            words = []
        if not words:
            setup.append(line)
            continue
        if words[0] == "module":
            if words[1:2] == ["load"]:
                modules += words[2:]
            elif words[1:2] == ["purge"]:
                out.notes.append("'module purge' dropped: the job loads the listed modules")
            else:
                setup.append(line)
            continue
        args = _specula_args(words)
        if args is not None:
            if out.found_specula:
                out.notes.append(f"Only the first specula command is used; dropped: {line}")
                continue
            out.found_specula = True
            _parse_specula_args(args, out)
            continue
        if out.found_specula:
            out.notes.append(f"Dropped (after the specula command): {line}")
        else:
            setup.append(line)
    out.options["modules"] = modules
    out.options["setup"] = "\n".join(setup)
    out.options["extra_sbatch"] = "\n".join(extra_sbatch)
    if not out.found_specula:
        out.notes.append("No specula command found: the simulation files are unchanged")
    return out


def _parse_sbatch(line: str, out: ParsedScript, extra: list[str]) -> None:
    try:
        words = shlex.split(line[len("#SBATCH"):], comments=True)
    except ValueError:
        words = []
    i = 0
    while i < len(words):
        word = words[i]
        value = ""
        if word.startswith("--"):
            key, eq, value = word[2:].partition("=")
            if not eq and i + 1 < len(words) and not words[i + 1].startswith("-"):
                i += 1
                value = words[i]
        elif word[:2] in _SBATCH_SHORT:
            key = _SBATCH_SHORT[word[:2]]
            value = word[2:]
            if not value and i + 1 < len(words):
                i += 1
                value = words[i]
        else:
            extra.append(f"#SBATCH {word}")
            i += 1
            continue
        i += 1
        _sbatch_option(key, value, out, extra)


def _sbatch_option(key: str, value: str, out: ParsedScript, extra: list[str]) -> None:
    name = _SBATCH_FIELDS.get(key)
    if name in _INT_FIELDS:
        try:
            out.options[name] = int(value)
        except ValueError:
            out.notes.append(f"#SBATCH --{key}={value}: not a number, kept as an extra option")
            extra.append(f"#SBATCH --{key}={value}")
    elif name:
        out.options[name] = value
    elif key == "gres":
        m = re.match(r"^gpu(?::[^:,]+)?:(\d+)$", value)
        if m:
            out.options["gpus_per_node"] = int(m.group(1))
        else:
            extra.append(f"#SBATCH --gres={value}")
    elif key == "ntasks":
        try:
            out.options["ntasks_per_node"] = int(value)
            out.notes.append(f"--ntasks={value} read as tasks per node")
        except ValueError:
            extra.append(f"#SBATCH --ntasks={value}")
    elif key in ("output", "error"):
        out.notes.append(f"--{key} dropped: the job writes slurm-<id>.out / .err, "
                         "which Studio downloads")
    else:
        extra.append(f"#SBATCH --{key}={value}" if value else f"#SBATCH --{key}")


def _specula_args(words: list[str]) -> list[str] | None:
    """Arguments of the specula command in *words*, or None if there is none."""
    for i, word in enumerate(words):
        base = posixpath.basename(word)
        if base == "specula":
            return words[i + 1:]
        if base.startswith("python") and words[i + 1:i + 3] in (["-m", "specula"],
                                                                 ["-m", "specula.main"]):
            return words[i + 3:]
        if base in ("bash", "sh") and words[i + 1:i + 2] == ["-c"] and i + 2 < len(words):
            try:
                inner = shlex.split(words[i + 2])
            except ValueError:
                return None
            return _specula_args(inner)
    return None


def _parse_specula_args(args: list[str], out: ParsedScript) -> None:
    opts = out.options
    opts["cpu"] = False
    opts["diagram"] = False
    extra: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        key, eq, value = word.partition("=")
        if key in ("--precision", "--nsimul"):
            if not eq and i + 1 < len(args):
                i += 1
                value = args[i]
            try:
                opts[key[2:]] = int(value)
            except ValueError:
                extra += [key, value]
        elif word == "--mpi":
            pass                                   # follows from the number of tasks
        elif word == "--cpu":
            opts["cpu"] = True
        elif key in ("--diagram", "--diagram-filename", "--diagram-title",
                     "--diagram-colors-on"):
            opts["diagram"] = True
            if key in ("--diagram-filename", "--diagram-title") and not eq:
                i += 1                             # its value
        elif word.lower().endswith((".yml", ".yaml")) and not word.startswith("-"):
            if posixpath.basename(word) == HPC_OVERRIDE_NAME:
                out.notes.append(f"{HPC_OVERRIDE_NAME} dropped: Studio adds it to every job")
            else:
                out.files.append(word)
        else:
            extra.append(word)
        i += 1
    opts["extra_args"] = " ".join(shlex.quote(a) for a in extra)


# ── adapting the simulation to the cluster ────────────────────────────────────

# Data mirrored on the cluster under the same absolute paths, below $WORK
DEFAULT_PATH_MAP = "/raid1 -> $WORK/raid1\n/raid2 -> $WORK/raid2\n/home -> $WORK/home"

# Keys of an object block that never hold paths
_NOT_PATHS = ("class", "inputs", "outputs", "gui_pos")


def parse_path_map(text: str) -> list[tuple[str, str]]:
    """``LOCAL -> REMOTE`` lines (``#`` comments, blank lines ignored)."""
    pairs = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        local, sep, remote = line.partition("->")
        if sep and local.strip() and remote.strip():
            pairs.append((local.strip(), remote.strip()))
    return pairs


def map_path(path: str, pairs) -> str | None:
    """*path* translated by the longest matching local prefix, or None.

    Prefixes match whole path components ("/raid1" matches "/raid1/x", not
    "/raid10"); backslashes count as slashes, so Windows paths can be mapped.
    """
    norm = path.replace("\\", "/")
    best = None
    for local, remote in pairs:
        prefix = local.replace("\\", "/").rstrip("/")
        if not prefix:
            continue
        if norm == prefix or norm.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, remote.rstrip("/"))
    if best is None:
        return None
    return best[1] + norm[len(best[0]):]


def mapped_values(merged: dict, pairs) -> list[tuple[str, str, object]]:
    """(object, key, cluster value) for every path value that *pairs* map.

    String values and lists of strings are mapped (a list is mapped as a
    whole when at least one item matches)."""
    found: list[tuple[str, str, object]] = []
    for name, obj in merged.items():
        if not isinstance(obj, dict):
            continue
        for key, value in obj.items():
            if key in _NOT_PATHS:
                continue
            if isinstance(value, str):
                mapped = map_path(value, pairs)
                if mapped is not None:
                    found.append((name, key, mapped))
            elif isinstance(value, list) and any(isinstance(v, str) for v in value):
                items = [map_path(v, pairs) if isinstance(v, str) else None for v in value]
                if any(m is not None for m in items):
                    found.append((name, key, [m if m is not None else v
                                              for m, v in zip(items, value, strict=True)]))
    return found


def hpc_override(base: dict, overrides: list[dict], remote_root_dir: str | None,
                 path_map=()) -> dict:
    """
    An override (applied last) that adapts the simulation to batch execution:

    * paths of data mirrored on the cluster are translated by *path_map*
      (``[(local prefix, cluster prefix)]``), in any object;
    * ``main.root_dir`` → *remote_root_dir* when given (e.g. uploaded
      calibration data), which wins over the mapping;
    * no display server (``display_server: false``, DisplayServer objects
      removed) — nobody can connect to a compute node;
    * DataStore objects with an absolute *local* ``store_dir`` (or one the
      mapping translates) write into ``results/<name>`` inside the job
      directory instead, where the results are downloaded from.
    """
    merged = dict(base)
    for ov in overrides:
        merged = combine_params(merged, ov)

    blocks: dict = {}
    for name, key, value in mapped_values(merged, path_map):
        blocks.setdefault(name, {})[key] = value
    remove = []
    for name, obj in merged.items():
        if not isinstance(obj, dict):
            continue
        cls = obj.get("class")
        if cls == "SimulParams":
            block = blocks.setdefault(name, {})
            block["display_server"] = False
            if remote_root_dir:
                block["root_dir"] = remote_root_dir
        elif cls == "DisplayServer":
            blocks.pop(name, None)
            remove.append(name)
        elif cls == "DataStore":
            store = str(obj.get("store_dir", ""))
            if _is_local_absolute(store) or map_path(store, path_map) is not None:
                blocks.setdefault(name, {})["store_dir"] = f"results/{name}"
    out: dict = {f"{name}_override": block for name, block in blocks.items()}
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
