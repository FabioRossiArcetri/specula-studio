"""SLURM script generation, cluster adaptation and output parsing (pure)."""

import shlex

from specula_studio.core import slurm

FILES = ["sim_simul.yml", "override1_ov.yml", slurm.HPC_OVERRIDE_NAME]


def _opts(**kw):
    base = dict(job_name="scao", account="try25_rossi", time="00:10:00", nodes=1,
                ntasks_per_node=1, gpus_per_node=1)
    base.update(kw)
    return slurm.SlurmOptions(**base)


def test_render_script_single_gpu():
    script = slurm.render_script(_opts(setup="source $WORK/venv/bin/activate"), FILES)
    assert script.startswith("#!/bin/bash\n") and "\r" not in script
    for line in ("#SBATCH --job-name=scao", "#SBATCH --account=try25_rossi",
                 "#SBATCH --gres=gpu:1", "#SBATCH --partition=boost_usr_prod",
                 "#SBATCH --qos=boost_qos_dbg", "#SBATCH --output=slurm-%j.out",
                 "module load cuda/12.3", "source $WORK/venv/bin/activate",
                 "export MPLBACKEND=Agg"):
        assert line in script.splitlines(), line
    srun = next(ln for ln in script.splitlines() if ln.startswith("srun"))
    assert srun.startswith("srun --mpi=none bash -c ")
    inner = shlex.split(srun)[-1]
    assert "--mpi" not in shlex.split(inner)            # one task: no MPI
    assert shlex.split(inner)[-3:] == FILES               # override applied last


def test_render_script_mpi_and_cpu_options():
    script = slurm.render_script(_opts(nodes=2, ntasks_per_node=4, gpus_per_node=4,
                                       cpu=True, diagram=True, extra_args="--profile",
                                       extra_sbatch="--mem=100G"), FILES)
    inner = shlex.split(next(ln for ln in script.splitlines() if ln.startswith("srun")))[-1]
    args = shlex.split(inner)
    assert args[0] == "specula" and "--mpi" in args and "--cpu" in args
    assert "--profile" in args and "--diagram-filename" in args
    assert "#SBATCH --mem=100G" in script
    assert "#SBATCH --gres=gpu:4" in script
    no_gpu = slurm.render_script(_opts(gpus_per_node=0, partition="dcgp_usr_prod"), FILES)
    assert "--gres" not in no_gpu


def test_validate():
    assert slurm.validate(_opts()) == []
    errors = slurm.validate(_opts(account="", time="10 min", job_name="a b",
                                  gpus_per_node=8))
    assert len(errors) == 4


def test_hpc_override_adapts_simulation():
    base = {
        "main": {"class": "SimulParams", "root_dir": "./calib", "display_server": True},
        "ds": {"class": "DisplayServer", "port": 5000},
        "store_local": {"class": "DataStore", "store_dir": "C:\\data\\run"},
        "store_rel": {"class": "DataStore", "store_dir": "./data"},
    }
    ov = slurm.hpc_override(base, [], "/leonardo_work/p/calib/scao")
    assert ov["main_override"] == {"display_server": False,
                                   "root_dir": "/leonardo_work/p/calib/scao"}
    assert ov["remove"] == ["ds"]
    assert ov["store_local_override"] == {"store_dir": "results/store_local"}
    assert "store_rel_override" not in ov
    assert slurm.hpc_override(base, [], None)["main_override"] == {"display_server": False}


def test_path_map_parsing_and_matching():
    pairs = slurm.parse_path_map(slurm.DEFAULT_PATH_MAP + "\n# comment\n\nbad line\n"
                                 "/raid2/gcarla -> /leonardo_work/x/andes  # longer wins\n"
                                 "C:\\Users\\fabio\\data -> $WORK/data")
    assert pairs[:3] == [("/raid1", "$WORK/raid1"), ("/raid2", "$WORK/raid2"),
                         ("/home", "$WORK/home")]
    assert len(pairs) == 5
    assert slurm.map_path("/raid1/guido/PASSATA/LBT", pairs) == "$WORK/raid1/guido/PASSATA/LBT"
    assert slurm.map_path("/raid1", pairs) == "$WORK/raid1"
    assert slurm.map_path("/raid10/x", pairs) is None             # whole components only
    assert slurm.map_path("/raid2/gcarla/git/a.fits", pairs) == "/leonardo_work/x/andes/git/a.fits"
    assert slurm.map_path("C:\\Users\\fabio\\data\\calib", pairs) == "$WORK/data/calib"
    assert slurm.map_path("./calib/SCAO", pairs) is None
    assert slurm.map_path("/leonardo_work/p/calib", pairs) is None


def test_hpc_override_maps_mirrored_data():
    pairs = slurm.parse_path_map(slurm.DEFAULT_PATH_MAP)
    base = {
        "main": {"class": "SimulParams",
                 "root_dir": "/raid2/gcarla/git/ANDES/andes/PASSATA_scripts/data/"},
        "pupil": {"class": "Pupilstop", "file_name": "/raid1/guido/pist2modes.fits",
                  "tag": "andes_pupil", "inputs": {"in": "/raid1/not_a_path_key"}},
        "lst": {"class": "Something", "files": ["/home/frossi/a.fits", "rel.fits", 3]},
        "store": {"class": "DataStore", "store_dir": "/raid1/guido/results/MORFEO/specula/"},
        "store_rel": {"class": "DataStore", "store_dir": "./output"},
    }
    ov = slurm.hpc_override(base, [], None, pairs)
    assert ov["main_override"] == {
        "root_dir": "$WORK/raid2/gcarla/git/ANDES/andes/PASSATA_scripts/data/",
        "display_server": False}
    assert ov["pupil_override"] == {"file_name": "$WORK/raid1/guido/pist2modes.fits"}
    assert ov["lst_override"] == {"files": ["$WORK/home/frossi/a.fits", "rel.fits", 3]}
    # results are written in the job directory, where they are downloaded from
    assert ov["store_override"] == {"store_dir": "results/store"}
    assert "store_rel_override" not in ov
    # an explicit root_dir (uploaded calibration data) wins over the mapping
    explicit = slurm.hpc_override(base, [], "/leonardo_work/p/calib/andes", pairs)
    assert explicit["main_override"]["root_dir"] == "/leonardo_work/p/calib/andes"


def test_result_dirs_follow_overrides():
    base = {"store": {"class": "DataStore", "store_dir": "./data"},
            "abs": {"class": "DataStore", "store_dir": "/leonardo_scratch/x"}}
    dirs = slurm.result_dirs(base, [{"store_override": {"store_dir": "out"}}], "/w/jobs/j1")
    assert dirs == {"store": "/w/jobs/j1/out", "abs": "/leonardo_scratch/x"}


def test_parsers():
    assert slurm.parse_sbatch("Submitted batch job 123456\n") == "123456"
    assert slurm.parse_sbatch("sbatch: error: Batch job submission failed") is None
    assert slurm.parse_squeue("RUNNING|1:23|8:37|lrdn0123\n") == {
        "state": "RUNNING", "elapsed": "1:23", "left": "8:37", "reason": "lrdn0123"}
    assert slurm.parse_squeue("") is None
    assert slurm.parse_sacct("CANCELLED by 123|00:01:02|0:15\n") == {
        "state": "CANCELLED", "elapsed": "00:01:02", "exit_code": "0:15"}
    assert slurm.is_final("COMPLETED") and slurm.is_final("CANCELLED+")
    assert not slurm.is_final("RUNNING") and not slurm.is_final("")
