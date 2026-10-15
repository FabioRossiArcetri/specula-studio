"""Regression tests for bugs fixed in the Phase 1 clean-up."""

import json
import logging
import subprocess
import sys

import pytest

from specula_studio import graph_manager as gm
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
