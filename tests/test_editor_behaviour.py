"""Regression tests for bugs fixed in the Phase 1 clean-up."""

import json
import logging
import subprocess
import sys

import pytest

from specula_studio.core import syntax as gm
from specula_studio import logs, settings
from tests.conftest import FIXTURES, REPO


@pytest.fixture(scope="module")
def scenarios():
    proc = subprocess.run(
        [sys.executable, "-m", "tests.helpers.ui_scenarios", str(FIXTURES)],
        cwd=REPO, capture_output=True, text=True, timeout=600,
    )
    assert "@@JSON@@" in proc.stdout, proc.stderr[-4000:]
    return json.loads(proc.stdout.split("@@JSON@@", 1)[1])


def test_new_node_does_not_export_required_marker(scenarios):
    res = scenarios["new_node_required"]
    assert "exception" not in res, res
    assert "REQUIRED" not in json.dumps(res["export"])
    assert res["complete"] is False


def test_delinking_list_reference_after_load(scenarios):
    res = scenarios["list_ref_delink"]
    assert "exception" not in res, res
    assert res["before"] == ["on_axis_source"]
    assert res["after"] is None
    assert "out_on_axis_source_ef" not in res["outputs_extra"]


def test_generated_names_skip_existing(scenarios):
    # a=aIntegrator0 is renamed to aIntegrator1, so the next free names follow
    assert scenarios["unique_names"] == ["aIntegrator1", "aIntegrator2", "aIntegrator3"]


def test_rename_validates_and_propagates(scenarios):
    res = scenarios["rename"]
    assert "exception" not in res, res
    assert all(res["errors"].values()), res["errors"]
    assert res["ok"] is None
    assert res["prop_sources"] == ["ngs"]
    assert res["prop_outputs"] == ["out_ngs_ef"]
    assert res["sh_in_ef"] == "prop.out_ngs_ef"
    assert res["psf_in_ef"] == "prop.out_ngs_ef"


def test_override_preview_edit_save_disable(scenarios):
    res = scenarios["override_preview"]
    assert "exception" not in res, res
    assert res["preview_total_time"] == 0.08 and res["preview_has_im_calibrator"]
    # the file keeps the base values plus the user's edit, never override values
    assert res["saved_total_time"] == 0.01
    assert res["saved_psf_wl"] == 750
    assert not res["saved_has_im_calibrator"]
    assert res["saved_meta"]["overrides"][0]["enabled"] is True
    # the override list is in comment lines: the file stays a SPECULA file
    assert res["saved_specula_keys"] == []
    assert res["saved_meta"]["overrides"][0]["path"] == "params_scao_sh_test_rec.yml"
    # disabling the override keeps the edit
    assert res["off_total_time"] == 0.01 and res["off_psf_wl"] == 750
    assert not res["off_has_im_calibrator"]
    # reopening restores and re-applies the override
    assert res["reopen_total_time"] == 0.08 and len(res["reopen_enabled"]) == 1


def test_undo_redo_and_dirty_flag(scenarios):
    res = scenarios["undo_dirty"]
    assert "exception" not in res, res
    assert res["dirty_after_open"] is False
    assert res["dirty_after_edit"] is True
    assert res["value_after_undo"] == 1650 and res["dirty_after_undo"] is False
    assert res["value_after_redo"] == 999
    assert res["added"] == 1 and res["canvas_nodes"]
    assert res["after_undo_paste"] == 0


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_list_ref_helpers():
    values = {"layer_list": "a"}                 # legacy scalar is upgraded
    gm.add_ref_value(values, "layer_list", "b")
    assert values["layer_list"] == ["a", "b"]
    gm.add_ref_value(values, "simul_params_ref", "main")
    assert values["simul_params_ref"] == "main"
    gm.remove_ref_value(values, "layer_list", "a")
    gm.remove_ref_value(values, "layer_list", "b")
    gm.remove_ref_value(values, "simul_params_ref", "main")
    assert values == {}


def test_validate_node_name():
    assert gm.validate_node_name("dm1") is None
    assert gm.validate_node_name("dm1", {"dm1"})
    for bad in ("", "a.b", "a-b", "a:b", "a b", "_x"):
        assert gm.validate_node_name(bad), bad


def test_settings_roundtrip_and_recent_files(tmp_path):
    path = tmp_path / "s.json"
    s = settings.Settings(path, migrate=False)
    for i in range(15):
        s.add_recent_file(str(tmp_path / f"f{i}.yml"))
    s.add_recent_file(str(tmp_path / "f3.yml"))
    s["theme"] = "light"
    s.save()
    again = settings.Settings(path, migrate=False)
    assert again["theme"] == "light"
    assert len(again["recent_files"]) == settings.MAX_RECENT_FILES
    assert again["recent_files"][0].endswith("f3.yml")
    assert len(set(again["recent_files"])) == len(again["recent_files"])


def test_settings_migrates_legacy_files(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"render_size": "LARGE", "bogus": 1}))
    theme = tmp_path / "user_prefs.json"
    theme.write_text(json.dumps({"theme": "light"}))
    monkeypatch.setattr(settings, "LEGACY_SETTINGS_PATH", legacy)
    monkeypatch.setattr(settings, "LEGACY_THEME_PATHS", [theme])
    s = settings.Settings(tmp_path / "new" / "settings.json")
    assert s["render_size"] == "LARGE" and s["theme"] == "light"
    assert "bogus" not in s.data
    assert (tmp_path / "new" / "settings.json").exists()


def test_ring_buffer_logging():
    ring = logs.setup_logging("DEBUG")
    log = logging.getLogger("specula_studio.test")
    log.debug("hello-debug")
    log.warning("hello-warning")
    assert any("hello-debug" in line for line in ring.lines())
    assert not any("hello-debug" in line for line in ring.lines(logging.WARNING))
    logs.set_level("INFO")


def test_cli_accepts_a_file_and_options():
    from specula_studio.app import parse_args
    args = parse_args(["sim.yml", "--log-level", "DEBUG"])
    assert args.file == "sim.yml" and args.log_level == "DEBUG"
    assert parse_args([]).file is None


def test_graph_issues_report_missing_inputs_and_simulparams():
    from specula_studio.core import validation, yaml_io
    from specula_studio.core.templates import load_bundled_templates

    d, p = load_bundled_templates()
    g = yaml_io.load_graph({"rec": {"class": "Modalrec"}}, {**d, **p})
    msgs = [i.describe(g) for i in validation.graph_issues(g)]
    assert any("recmat" in m for m in msgs)
    assert any("SimulParams" in m for m in msgs)


def test_monitors_open_without_a_run_and_follow_its_kind(scenarios):
    res = scenarios["monitors_without_run"]
    assert "exception" not in res, res
    assert res["before"] == [["out_sr", True]]              # opened, waiting for data
    assert res["remote"] == {"in_editor": 0, "queued": ["out_sr"]}
    assert res["local"] == [["out_sr", True]] and res["queued_after"] == 0


def test_control_window_link_drives_the_editor(scenarios):
    res = scenarios["control_window_link"]
    assert "exception" not in res, res
    init = res["init"]
    assert init["type"] == "init" and init["terminal"] == "before\n"
    assert init["status"] == "idle" and "recent_hosts" in init
    assert res["live"] == {"type": "terminal", "text": "live\n"}
    kinds = [(e["type"], e.get("text")) for e in res["events"]]
    assert ("status", "aborted") in kinds
    assert kinds[-1] == ("terminal_reset", "")
    assert res["backlog_after_clear"] == ""
    assert res["quit"] == {"type": "quit"}
