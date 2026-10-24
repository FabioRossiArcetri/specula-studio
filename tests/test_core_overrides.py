"""Override composition (SPECULA rules) and preview rebasing."""

import pytest
import yaml

from specula_studio.core.overrides import OverrideManager, combine_params, rebase_edits
from tests.conftest import FIXTURES

BASE = {
    "main": {"class": "SimulParams", "total_time": 1.0},
    "dm": {"class": "DM", "nmodes": 10, "inputs": {"in_command": "ctrl.out_comm"}},
    "ctrl": {"class": "Integrator", "inputs": {"delta_comm": "rec.out_modes"}},
    "psf": {"class": "PSF", "inputs": {"in_ef": ["prop.out_ef", "dm.out_layer:-1"]}},
}


def test_override_remove_and_add():
    out = combine_params(BASE, {
        "dm_override": {"nmodes": 20},
        "remove": ["ctrl"],
        "pushpull": {"class": "PushPullGenerator"},
    })
    assert out["dm"]["nmodes"] == 20
    assert "ctrl" not in out and "pushpull" in out
    # SPECULA also drops the inputs fed by a removed object
    assert "in_command" not in out["dm"]["inputs"]
    assert BASE["dm"]["nmodes"] == 10           # input untouched


def test_remove_cleans_list_inputs():
    out = combine_params(BASE, {"remove": ["dm"]})
    assert out["psf"]["inputs"]["in_ef"] == ["prop.out_ef"]


def test_simul_idx_suffix():
    extra = {"dm_override_1": {"nmodes": 11}, "dm_override_2": {"nmodes": 22}}
    assert combine_params(BASE, extra, simul_idx=0)["dm"]["nmodes"] == 10
    assert combine_params(BASE, extra, simul_idx=2)["dm"]["nmodes"] == 22


def test_strict_mode_raises_like_specula():
    with pytest.raises(ValueError):
        combine_params(BASE, {"nope_override": {"a": 1}}, strict=True)
    with pytest.raises(ValueError):
        combine_params(BASE, {"remove": ["nope"]}, strict=True)
    with pytest.raises(ValueError):
        combine_params(BASE, {"dm": {"class": "DM"}}, strict=True)


def test_real_specula_override_file():
    base = yaml.safe_load((FIXTURES / "params_scao_sh_test.yml").read_text())
    extra = yaml.safe_load((FIXTURES / "params_scao_sh_test_rec.yml").read_text())
    out = combine_params(base, extra, strict=True)
    assert out["main"]["total_time"] == 0.080
    assert out["dm"]["inputs"]["in_command"] == "pushpull.output"
    assert "im_calibrator" in out


def test_rebase_edits_keeps_override_values_out_of_base():
    merged = combine_params(BASE, {"dm_override": {"nmodes": 20}, "extra": {"class": "X"}})
    current = yaml.safe_load(yaml.safe_dump({k: dict(v) for k, v in merged.items()}))
    current["main"]["total_time"] = 5.0          # user edit on a base object
    current["dm"]["obsratio"] = 0.1              # new key on an overridden object
    current["extra"]["p"] = 1                     # edit on override-only object: dropped
    current["new_obj"] = {"class": "Y"}          # user-added object
    del current["ctrl"]                           # user deletion
    new_base = rebase_edits(BASE, merged, current)
    assert new_base["main"]["total_time"] == 5.0
    assert new_base["dm"]["nmodes"] == 10         # override value NOT folded in
    assert new_base["dm"]["obsratio"] == 0.1
    assert "extra" not in new_base
    assert "new_obj" in new_base and "ctrl" not in new_base


def test_manager_commit_edits(tmp_path):
    f = tmp_path / "ov.yml"
    f.write_text("dm_override:\n  nmodes: 99\n")
    om = OverrideManager()
    om.load_overrides([f])
    om.toggle_override(str(f))
    om.set_base_snapshot(BASE)
    merged = om.apply_overrides(om.get_base_snapshot())
    om.set_merged_snapshot(merged)
    edited = {k: dict(v) for k, v in merged.items()}
    edited["main"]["total_time"] = 2.0
    base = om.commit_edits(edited)
    assert base["main"]["total_time"] == 2.0 and base["dm"]["nmodes"] == 10
    assert om.get_base_snapshot()["main"]["total_time"] == 2.0
