"""
HPC workflow (ssh/scp/sbatch) against a fake command runner that records
every command and answers like a SLURM cluster would.
"""

import pytest
import yaml

from specula_studio import hpc
from specula_studio.sshutil import CommandResult
from specula_studio.core import slurm

ENV_OUT = ("HOME=/leonardo/home/userexternal/frossi\nWORK=/leonardo_work/try25_rossi\n"
           "CINECA_SCRATCH=/leonardo_scratch/large/userexternal/frossi\nSCRATCH=\nFAST=\n"
           "USER=frossi\nHOSTNAME=login05.leonardo.local\n"
           "SPECULA=/leonardo_work/try25_rossi/venv/bin/specula\n"
           "SPECULA_SRC=/leonardo_work/try25_rossi/SPECULA\n")


class FakeCluster:
    def __init__(self, auth_ok=True, existing=()):
        self.calls = []
        self.auth_ok = auth_ok
        self.uploaded = {}         # remote dir -> list of local paths
        self.queue_state = "PENDING"
        self.existing = set(existing)   # paths that exist on the cluster

    def __call__(self, argv, timeout=0, input_text=None):
        self.calls.append(list(argv))
        prog = argv[0]
        if not self.auth_ok:
            return CommandResult(255, "", "frossi@login.leonardo.cineca.it: "
                                              "Permission denied (publickey).")
        if prog == "scp":
            dest = argv[-1].split(":", 1)[1]
            self.uploaded.setdefault(dest.rstrip("/"), []).extend(
                a for a in argv[1:-1] if not a.startswith("-") and a not in
                ("BatchMode=yes", "ConnectTimeout=20"))
            return CommandResult(0, "", "")
        command = argv[-1]
        if "echo \"HOME=$HOME\"" in command:
            return CommandResult(0, ENV_OUT, "")
        if command.startswith("for p in "):
            import shlex
            paths = shlex.split(command[len("for p in "):command.index("; do")])
            return CommandResult(0, "".join(f"MISSING:{p}\n" for p in paths
                                            if p not in self.existing), "")
        if "sbatch" in command:
            return CommandResult(0, "Submitted batch job 4242\n", "")
        if command.startswith("squeue"):
            if self.queue_state is None:
                return CommandResult(0, "", "")
            return CommandResult(0, f"{self.queue_state}|0:10|9:50|(Priority)\n", "")
        if command.startswith("sacct"):
            return CommandResult(0, "COMPLETED|00:05:00|0:0\n", "")
        return CommandResult(0, "", "")


def _request(tmp_path, calib=True):
    calib_dir = tmp_path / "scao_calib"
    calib_dir.mkdir()
    (calib_dir / "rec.fits").write_bytes(b"x")
    ov = tmp_path / "ov.yml"
    ov.write_text("main_override:\n  total_time: 0.5\n")
    base = {"main": {"class": "SimulParams", "root_dir": "./calib", "gui_pos": [1, 2]},
            "store": {"class": "DataStore", "store_dir": "./data"},
            "_overrides_metadata": {"overrides": []}}
    opts = slurm.SlurmOptions(job_name="scao", account="try25_rossi")
    return hpc.SubmitRequest(
        login_host="leonardo", transfer_host="", remote_base="$WORK/specula_studio",
        sim_name="scao", base=base, override_paths=[str(ov)], options=opts,
        calib_local_dir=str(calib_dir) if calib else "",
        remote_root_dir="$WORK/specula_studio/calib/scao_calib" if calib else "",
        download_to=str(tmp_path / "results"))


def test_all_commands_are_non_interactive(tmp_path):
    fake = FakeCluster()
    hpc.submit(_request(tmp_path), runner=fake, work_dir=str(tmp_path / "job"))
    for argv in fake.calls:
        assert argv[0] in ("ssh", "scp")
        assert "BatchMode=yes" in argv


def test_submit_uploads_calibration_and_job_files(tmp_path):
    fake = FakeCluster()
    work = tmp_path / "job"
    work.mkdir()
    job = hpc.submit(_request(tmp_path), runner=fake, work_dir=str(work))

    assert job.job_id == "4242"
    assert job.job_dir.startswith("/leonardo_work/try25_rossi/specula_studio/jobs/scao_")
    # calibration uploaded recursively into the parent of the remote root_dir
    calib_call = next(c for c in fake.calls if c[0] == "scp" and "-r" in c)
    assert calib_call[-1] == "leonardo:/leonardo_work/try25_rossi/specula_studio/calib/"
    # job files uploaded into the job directory, then sbatch there
    names = sorted(p.replace("\\", "/").split("/")[-1] for p in fake.uploaded[job.job_dir])
    assert names == sorted(["scao_simul.yml", "override1_ov.yml",
                            slurm.HPC_OVERRIDE_NAME, "job.slurm"])
    sbatch = next(c[-1] for c in fake.calls if "sbatch" in c[-1])
    assert job.job_dir in sbatch and "job.slurm" in sbatch

    # the cluster override points root_dir to the uploaded data ($WORK expanded)
    hpc_ov = yaml.safe_load((work / slurm.HPC_OVERRIDE_NAME).read_text())
    assert hpc_ov["main_override"]["root_dir"] == \
        "/leonardo_work/try25_rossi/specula_studio/calib/scao_calib"
    sim = yaml.safe_load((work / "scao_simul.yml").read_text())
    assert "gui_pos" not in sim["main"] and "_overrides_metadata" not in sim
    script = (work / "job.slurm").read_bytes()
    assert b"\r\n" not in script and b"#SBATCH --account=try25_rossi" in script
    assert job.result_dirs == {"store": f"{job.job_dir}/data"}


def test_submit_maps_mirrored_data_and_checks_it(tmp_path):
    """Leonardo: data mirrored under $WORK, SPECULA in a venv in $WORK."""
    work_root = "/leonardo_work/try25_rossi"
    fake = FakeCluster(existing={f"{work_root}/raid2/gcarla/data/"})
    req = _request(tmp_path, calib=False)
    req.base["main"]["root_dir"] = "/raid2/gcarla/data/"
    req.base["pupil"] = {"class": "Pupilstop", "file_name": "/raid1/guido/pist2modes.fits"}
    req.options.setup = "source $WORK/specula/bin/activate"
    req.path_map = slurm.parse_path_map(slurm.DEFAULT_PATH_MAP)
    work = tmp_path / "job"
    work.mkdir()
    messages = []
    hpc.submit(req, progress=messages.append, runner=fake, work_dir=str(work))

    hpc_ov = yaml.safe_load((work / slurm.HPC_OVERRIDE_NAME).read_text())
    assert hpc_ov["main_override"]["root_dir"] == f"{work_root}/raid2/gcarla/data/"
    assert hpc_ov["pupil_override"]["file_name"] == f"{work_root}/raid1/guido/pist2modes.fits"
    # the setup line runs before specula is looked for, and is in the job script
    probe = next(c[-1] for c in fake.calls if 'echo "HOME=$HOME"' in c[-1])
    assert "source $WORK/specula/bin/activate" in probe
    assert "source $WORK/specula/bin/activate" in (work / "job.slurm").read_text()
    # missing data is reported before submitting, found data is not
    missing = [m for m in messages if "does not exist on the cluster" in m]
    assert missing == [f"Warning: {work_root}/raid1/guido/pist2modes.fits does not exist "
                       "on the cluster (check the path mapping, or copy the data there)"]


def test_check_connection_reports_the_specula_checkout():
    info = hpc.HpcClient("leonardo", runner=FakeCluster()).check_connection(
        "source $WORK/specula/bin/activate")
    assert info["ok"] and info["specula_src"] == "/leonardo_work/try25_rossi/SPECULA"


def test_submit_rejects_invalid_options_before_connecting(tmp_path):
    fake = FakeCluster()
    req = _request(tmp_path, calib=False)
    req.options.account = ""
    with pytest.raises(hpc.HpcError, match="account"):
        hpc.submit(req, runner=fake)
    assert fake.calls == []


def test_expired_certificate_is_explained(tmp_path):
    fake = FakeCluster(auth_ok=False)
    info = hpc.HpcClient("leonardo", runner=fake).check_connection()
    assert not info["ok"] and "step ssh login" in info["message"]
    with pytest.raises(hpc.HpcError, match="step ssh login"):
        hpc.submit(_request(tmp_path, calib=False), runner=fake)


def test_job_status_follows_queue_then_accounting(tmp_path):
    fake = FakeCluster()
    job = hpc.HpcJob(job_id="4242", name="scao", login_host="leonardo",
                     transfer_host="leonardo", job_dir="/w/jobs/j",
                     result_dirs={"store": "/w/jobs/j/data"})
    hpc.update_job(job, runner=fake)
    assert job.state == "PENDING" and not job.finished and job.reason == "(Priority)"
    fake.queue_state = None                       # left the queue
    hpc.update_job(job, runner=fake)
    assert job.state == "COMPLETED" and job.finished and job.exit_code == "0:0"

    target = hpc.download_results(job, str(tmp_path / "res"), runner=fake)
    downloads = [c for c in fake.calls if c[0] == "scp" and c[-1] == target]
    sources = [c[-2] for c in downloads]
    assert "leonardo:/w/jobs/j/data" in sources
    assert "leonardo:/w/jobs/j/slurm-4242.out" in sources and job.downloaded
    again = hpc.HpcJob.from_dict(job.to_dict())
    assert again == job


def test_expand_remote():
    env = {"WORK": "/leonardo_work/p", "HOME": "/home/u"}
    assert hpc.expand_remote("$WORK/a/${WORK}", env) == "/leonardo_work/p/a//leonardo_work/p"
    assert hpc.expand_remote("~/x", env) == "/home/u/x"
    assert hpc.expand_remote("$UNSET/x", env) == "$UNSET/x"
