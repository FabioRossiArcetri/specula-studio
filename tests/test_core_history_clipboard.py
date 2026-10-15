"""Undo history and clipboard (pure core)."""

from specula_studio.core import clipboard, history, yaml_io
from specula_studio.core.templates import load_bundled_templates
from tests.conftest import FIXTURES


def _graph():
    d, p = load_bundled_templates()
    return yaml_io.load_graph(yaml_io.read_yaml_file(FIXTURES / "params_scao_sh_test.yml"),
                              {**d, **p})


def test_history_undo_redo():
    g = _graph()
    h = history.History()
    h.reset(g.snapshot(), history.fingerprint(g))
    assert not h.record(g.snapshot(), history.fingerprint(g))     # no change
    psf = g.find_by_name("psf")
    g.nodes[psf]["values"]["wavelengthInNm"] = 999
    assert h.record(g.snapshot(), history.fingerprint(g))
    g.restore(h.undo())
    assert g.nodes[psf]["values"]["wavelengthInNm"] == 1650
    g.restore(h.redo())
    assert g.nodes[psf]["values"]["wavelengthInNm"] == 999
    assert not h.can_redo() and h.can_undo()


def test_duplicate_keeps_outside_inputs_and_renames_internal_links():
    g = _graph()
    sel = [g.find_by_name(n) for n in ("control", "dm")]
    clip = clipboard.copy_nodes(g, sel)
    new = clipboard.paste_nodes(g, clip)
    names = sorted(g.nodes[u]["name"] for u in new)
    assert names == ["control_1", "dm_1"]
    out = yaml_io.export_graph(g)
    # internal link follows the renaming, outside input is kept
    assert out["dm_1"]["inputs"]["in_command"] == "control_1.out_comm"
    assert out["control_1"]["inputs"]["delta_comm"] == "rec.out_modes"
    assert out["dm_1"]["simul_params_ref"] == "main"
    # originals untouched
    assert out["dm"]["inputs"]["in_command"] == "control.out_comm"


def test_paste_renames_atmo_dynamic_outputs():
    g = _graph()
    sel = [g.find_by_name(n) for n in ("on_axis_source", "prop")]
    clipboard.paste_nodes(g, clipboard.copy_nodes(g, sel))
    out = yaml_io.export_graph(g)
    assert out["prop_1"]["source_dict_ref"] == ["on_axis_source_1"]
    assert out["prop_1"]["outputs"] == ["out_on_axis_source_1_ef"]
