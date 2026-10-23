"""HPC windows: editor-side commands and events over the window link."""

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from specula_studio import control_link, hpc
from specula_studio.hpc_control import JOBS, SUBMIT, HpcControl
from tests.conftest import REPO


class _Prefs(dict):
    pass


def _editor():
    saved = []
    om = SimpleNamespace(any_enabled=lambda: True,
                         get_enabled_overrides=lambda: ["/x/ov_a.yml"])
    graph = SimpleNamespace(nodes={"u1": {"type": "SimulParams",
                                          "values": {"root_dir": "/data/calib"}}})
    return SimpleNamespace(preferences=_Prefs(hpc={}, hpc_jobs=[]),
                           _save_settings=lambda: saved.append(1),
                           override_manager=om, graph=graph,
                           current_simulation_name="scao",
                           current_simulation_path="/sims/scao.yml",
                           sim_control=None, theme_manager=None, saved=saved)


def _recv(conn, kind, timeout=5.0):
    """Next message of *kind* from the editor (others skipped)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if conn.poll(0.05):
            msg = conn.recv()
            if msg.get("type") == kind:
                return msg
    raise AssertionError(f"no {kind!r} event")


def _link(hc, window):
    server = control_link.ControlServer(on_connect=lambda: hc._on_window_connected(window))
    hc._links[window] = server
    return server, control_link.connect(server.address, server.authkey)


def test_sim_info_lists_the_files_passed_to_specula():
    info = HpcControl(_editor()).sim_info()
    assert info["name"] == "scao" and info["n_overrides"] == 1
    assert info["run_files"] == ["scao_simul.yml", "override1_ov_a.yml",
                                 "zz_studio_hpc_override.yml"]
    assert info["local_calib"].replace("\\", "/").endswith("/data/calib")


def test_submit_window_roundtrip():
    ed = _editor()
    hc = HpcControl(ed)
    server, conn = _link(hc, SUBMIT)
    try:
        init = _recv(conn, "init")
        assert init["profile"]["login_host"] == "login.leonardo.cineca.it"
        assert init["sim_info"]["name"] == "scao"

        profile = dict(init["profile"], login_host="frossi@login.leonardo.cineca.it")
        conn.send({"type": "save_profile", "profile": profile})
        deadline = time.time() + 5
        while not ed.saved and time.time() < deadline:
            hc.poll()
            time.sleep(0.01)
        assert ed.preferences["hpc"]["login_host"] == "frossi@login.leonardo.cineca.it"
        hc.poll()
        assert _recv(conn, "log")["text"] == "Settings saved"

        ed.current_simulation_name = "mcao"           # the editor opened another file
        hc._sim_info_checked = 0.0
        hc.poll()
        assert _recv(conn, "sim_info")["run_files"][0] == "mcao_simul.yml"
    finally:
        conn.close()
        server.close()


def test_jobs_window_gets_the_jobs_and_forget_works():
    ed = _editor()
    ed.preferences["hpc_jobs"] = [hpc.HpcJob(job_id="7", name="t", login_host="h",
                                             transfer_host="h", job_dir="/w",
                                             state="COMPLETED").to_dict()]
    hc = HpcControl(ed)
    server, conn = _link(hc, JOBS)
    try:
        assert [j["job_id"] for j in _recv(conn, "init")["jobs"]] == ["7"]
        conn.send({"type": "forget_job", "job_id": "7"})
        deadline = time.time() + 5
        while hc.jobs and time.time() < deadline:
            hc.poll()
            time.sleep(0.01)
        hc.poll()
        while _recv(conn, "jobs")["jobs"]:           # earlier updates came first
            pass
        assert ed.preferences["hpc_jobs"] == []
    finally:
        conn.close()
        server.close()


def test_job_inputs_from_files_or_the_editor(tmp_path):
    ed = _editor()
    ed.sim_control = SimpleNamespace(
        _run_inputs=lambda: ({"main": {"class": "SimulParams"}}, ["/x/ov_a.yml"]))
    hc = HpcControl(ed)
    # the editor's simulation and enabled overrides (missing override -> error)
    with pytest.raises(hpc.HpcError, match="ov_a.yml"):
        hc._job_inputs({"base": ""})
    base, overrides, name = hc._job_inputs({"base": "", "overrides": []})
    assert base == {"main": {"class": "SimulParams"}} and overrides == [] and name == "scao"

    sim = tmp_path / "params andes.yml"
    sim.write_text("main:\n  class: SimulParams\n  root_dir: /raid2/x\n")
    ov = tmp_path / "ov1.yml"
    ov.write_text("main_override:\n  total_time: 0.1\n")
    base, overrides, name = hc._job_inputs({"base": str(sim), "overrides": [str(ov)]})
    assert base["main"]["root_dir"] == "/raid2/x"
    assert overrides == [str(ov)] and name == "params_andes"
    with pytest.raises(hpc.HpcError, match="not found"):
        hc._job_inputs({"base": str(tmp_path / "nope.yml")})


def test_unknown_command_is_harmless():
    hc = HpcControl(_editor())
    hc.handle_command({"type": "nonsense"})
    hc.handle_command({"type": "job_output", "job_id": "missing"})


@pytest.mark.parametrize("kind", ["control", "hpc_submit", "hpc_jobs"])
def test_tool_window_builds_with_real_dearpygui(kind):
    proc = subprocess.run(
        [sys.executable, "-m", "tests.helpers.tool_window_smoke", kind],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert f"@@OK {kind}" in proc.stdout, proc.stdout[-2000:] + proc.stderr[-2000:]
