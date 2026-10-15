"""Run preparation helpers (pure)."""

import shlex

import yaml

from specula_studio.core import run_prep


def test_extract_port():
    assert run_prep.extract_port("[SPECULA_PORT:50155] DisplayServer listening") == 50155
    assert run_prep.extract_port(" * Running on http://127.0.0.1:5000") == 5000
    assert run_prep.extract_port("DisplayServer started on port 6000") == 6000
    assert run_prep.extract_port("t=0.001 step 12") is None


def test_inject_display_server():
    params = {"main": {"class": "SimulParams", "display_server": True}}
    name = run_prep.inject_display_server(params, "sid", 0, host="0.0.0.0")
    assert params[name] == {"class": "DisplayServer", "port": 0, "mode": "data",
                            "session_id": "sid", "host": "0.0.0.0"}
    assert "display_server" not in params["main"]      # no second image-mode server
    # an existing DisplayServer is reused
    assert run_prep.inject_display_server(params, "sid2", 5000) == name
    assert params[name]["session_id"] == "sid2"
    assert run_prep.inject_display_server({"x": {"class": "DM"}}, "s", 0) is None


def test_write_run_files(tmp_path):
    ov = tmp_path / "ov.yml"
    ov.write_text("dm_override:\n  nmodes: 3\n")
    base = {"main": {"class": "SimulParams", "gui_pos": [1, 2]},
            "_overrides_metadata": {"overrides": []}}
    files = run_prep.write_run_files(str(tmp_path), base, [str(ov)], sim_name="s",
                                     session_id="sid", display_port=0)
    assert len(files) == 2 and files[0].endswith("s_simul.yml")
    data = yaml.safe_load(open(files[0]))
    assert "_overrides_metadata" not in data and "gui_pos" not in data["main"]
    # keys SPECULA reads from the YAML itself are filled in
    assert data["main"]["root_dir"] == "." and data["main"]["time_step"] == 0.001
    assert any(v.get("class") == "DisplayServer" for v in data.values())
    assert open(files[1]).read() == ov.read_text()


def test_cli_and_remote_command():
    args = run_prep.specula_cli_args(["a b.yml", "o.yml"], stepping=True, nsimul=2,
                                     cpu=True, target=1, precision=0, log_level="DEBUG")
    assert args == ["a b.yml", "o.yml", "--stepping", "--nsimul", "2", "--cpu",
                    "--target", "1", "--precision", "0", "--log-level", "DEBUG"]
    assert "--profile" in run_prep.specula_cli_args(["a.yml"], profile=True)
    assert "--diagram" in run_prep.specula_cli_args(["a.yml"], diagram=True)
    cmd = run_prep.remote_shell_command("/tmp/d x", args)
    outer = shlex.split(cmd)
    assert outer[:2] == ["bash", "-ic"]
    inner = shlex.split(outer[2])
    assert inner[:4] == ["cd", "/tmp/d x", "&&", "specula"] and inner[4] == "a b.yml"
