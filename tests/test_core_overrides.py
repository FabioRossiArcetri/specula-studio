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


def test_override_list_in_comments_with_relative_paths(tmp_path):
    from specula_studio.core.overrides import overrides_comment, parse_overrides_comment
    ov = tmp_path / "ovs" / "calib.yml"
    ov.parent.mkdir()
    ov.write_text("main_override:\n  total_time: 0.1\n")
    om = OverrideManager()
    om.load_overrides([str(ov)])
    om.toggle_override(str(ov))
    entries = om.to_dict(str(tmp_path))["overrides"]
    assert entries == [{"path": "ovs/calib.yml", "enabled": True}]
    text = "main:\n  class: SimulParams\n" + overrides_comment(entries, "sim.yml")
    assert yaml.safe_load(text) == {"main": {"class": "SimulParams"}}   # SPECULA sees no extra key
    assert "specula sim.yml ovs/calib.yml" in text
    restored = OverrideManager()
    restored.from_dict({"overrides": parse_overrides_comment(text)}, str(tmp_path))
    assert restored.get_enabled_overrides() == [str(ov.resolve())]


def test_moving_an_added_object_is_not_an_unsaved_edit(caplog):
    base = {"main": {"class": "SimulParams"}}
    merged = {**base, "pp": {"class": "PushPullGenerator", "gui_pos": [0, 0]}}
    current = {**base, "pp": {"class": "PushPullGenerator", "gui_pos": [300, 40]}}
    out = rebase_edits(base, merged, current)
    assert "pp" not in out and "were not saved" not in caplog.text
    current["pp"]["nmodes"] = 5
    rebase_edits(base, merged, current)
    assert "were not saved" in caplog.text


def test_added_objects_keep_their_place(tmp_path):
    ov = tmp_path / "ov.yml"
    ov.write_text("pp:\n  class: PushPullGenerator\n")
    om = OverrideManager()
    om.load_overrides([str(ov)])
    om.toggle_override(str(ov))
    base = {"main": {"class": "SimulParams", "gui_pos": [0, 0]}}
    om.set_base_snapshot(base)
    merged = om.apply_overrides(base)
    assert om.place_added(merged) == ["pp"]              # new: to be placed
    om.set_merged_snapshot(merged)
    moved = {**merged, "pp": {**merged["pp"], "gui_pos": [500, 60]}}
    om.commit_edits(moved)                                # e.g. when toggled off
    again = om.apply_overrides(om.get_base_snapshot())
    assert om.place_added(again) == [] and again["pp"]["gui_pos"] == [500, 60]


def test_place_new_next_to_links_without_overlaps():
    from specula_studio.core.layout import place_new
    positions = {"a": (0, 0), "b": (300, 0), "c": (300, 200)}
    sizes = {"a": (200, 100), "b": (200, 100), "c": (200, 100), "n1": (150, 80), "n2": (150, 80)}
    out = place_new(["n1", "n2"], positions, sizes, [("a", "n1"), ("n1", "n2")], gap=50)
    boxes = {**positions, **out}

    def overlap(p, q):
        (px, py), (qx, qy) = boxes[p], boxes[q]
        return (px < qx + sizes[q][0] and qx < px + sizes[p][0]
                and py < qy + sizes[q][1] and qy < py + sizes[p][1])
    names = list(boxes)
    assert not any(overlap(p, q) for i, p in enumerate(names) for q in names[i + 1:])
    assert out["n1"][0] == 250                    # right of its source a
    assert out["n2"][0] == out["n1"][0] + 150 + 50  # and the chain follows it
