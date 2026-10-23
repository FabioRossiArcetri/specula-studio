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
                 "module load cuda/12.2", "source $WORK/venv/bin/activate",
                 "export MPLBACKEND=Agg"):
        assert line in script.splitlines(), line
    assert "nvhpc" not in script                          # not on Leonardo any more
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


def test_parse_script_reads_back_a_generated_script():
    opts = _opts(setup="cd $WORK\nsource specula/bin/activate", extra_sbatch="--mem=100G",
                 cpu=True, nsimul=3, precision=0, diagram=True, extra_args="--profile",
                 nodes=2, ntasks_per_node=2, gpus_per_node=4, cpus_per_task=4)
    parsed = slurm.parse_script(slurm.render_script(opts, FILES))
    assert parsed.found_specula
    assert parsed.files == FILES[:-1]                 # Studio adds its override itself
    expected = opts.to_dict()
    expected["extra_sbatch"] = "#SBATCH --mem=100G"
    for name, value in expected.items():
        assert parsed.options[name] == value, name


def test_parse_script_hand_written_leonardo_job():
    text = """#!/bin/bash
#SBATCH -J andes
#SBATCH -A IscrC_SPECIAL
#SBATCH -p boost_usr_prod
#SBATCH --qos boost_qos_dbg
#SBATCH --time=00:20:00
#SBATCH -N 1
#SBATCH --ntasks-per-node=1 --cpus-per-task=8
#SBATCH --gres=gpu:a100:2      # two GPUs
#SBATCH --mem=100G
#SBATCH --output=andes.out
module purge
module load cuda/12.2 python/3.11
cd $WORK
source specula/bin/activate
srun specula --cpu --nsimul 2 params_andes.yml ovr/ov1.yml --foo bar
echo done
"""
    parsed = slurm.parse_script(text)
    o = parsed.options
    assert (o["job_name"], o["account"], o["partition"], o["qos"], o["time"]) == \
        ("andes", "IscrC_SPECIAL", "boost_usr_prod", "boost_qos_dbg", "00:20:00")
    assert (o["nodes"], o["ntasks_per_node"], o["cpus_per_task"], o["gpus_per_node"]) == \
        (1, 1, 8, 2)
    assert o["extra_sbatch"] == "#SBATCH --mem=100G"
    assert o["modules"] == ["cuda/12.2", "python/3.11"]
    assert o["setup"] == "cd $WORK\nsource specula/bin/activate"
    assert o["cpu"] is True and o["nsimul"] == 2 and o["extra_args"] == "--foo bar"
    assert "precision" not in o                      # not in the script: form value kept
    assert parsed.files == ["params_andes.yml", "ovr/ov1.yml"]
    notes = " | ".join(parsed.notes)
    assert "module purge" in notes and "--output" in notes and "echo done" in notes


def test_parse_script_specula_wrappers():
    for line in ("python -m specula a.yml b.yaml",
                 "mpirun -np 4 /leonardo_work/x/venv/bin/specula --mpi a.yml b.yaml",
                 "srun --mpi=none bash -c 'specula --precision=0 a.yml b.yaml'"):
        parsed = slurm.parse_script(f"#!/bin/bash\n{line}\n")
        assert parsed.found_specula and parsed.files == ["a.yml", "b.yaml"], line
    none = slurm.parse_script("#!/bin/bash\n#SBATCH -A x\npython run.py\n")
    assert not none.found_specula and none.files == []
    assert none.options["setup"] == "python run.py"
    assert any("No specula command" in n for n in none.notes)


def test_run_file_names_match_the_written_files(tmp_path):
    from specula_studio.core.run_prep import write_run_files
    ov = tmp_path / "ov_a.yml"
    ov.write_text("main_override:\n  total_time: 0.1\n")
    out = tmp_path / "out"
    out.mkdir()
    written = write_run_files(str(out), {"main": {"class": "SimulParams"}}, [str(ov)],
                              sim_name="andes")
    names = [p.replace("\\", "/").rsplit("/", 1)[-1] for p in written]
    assert slurm.run_file_names("andes", [str(ov)]) == [*names, slurm.HPC_OVERRIDE_NAME]
    assert slurm.safe_name("my sim (v2)") == "my_sim__v2_"


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
