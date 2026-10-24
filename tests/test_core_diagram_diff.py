"""DOT export and semantic diff (pure)."""

from specula_studio.core import diagram, yaml_io
from specula_studio.core.templates import load_bundled_templates
from tests.conftest import FIXTURES


def _graph():
    d, p = load_bundled_templates()
    return yaml_io.load_graph(yaml_io.read_yaml_file(FIXTURES / "params_scao_sh_test.yml"),
                              {**d, **p})


def test_to_dot_structure():
    g = _graph()
    dot = diagram.to_dot(g, title="scao")
    assert dot.startswith("digraph specula {") and dot.rstrip().endswith("}")
    edges = [ln for ln in dot.splitlines() if '" -> "' in ln]
    assert len(edges) == len(g.connections)
    assert '"main\\n(SimulParams)"' in dot
    assert "style=dashed, color=red" in dot          # dm.out_layer:-1 feedback
    assert "style=dotted" in dot                     # simul_params_ref
    assert "shape=ellipse" in dot and "shape=box" in dot


def test_semantic_diff_ignores_positions_and_detects_changes():
    g = _graph()
    saved = yaml_io.export_graph(g)
    moved = yaml_io.export_graph(g)
    moved["psf"]["gui_pos"] = [10, 10]
    assert yaml_io.semantic_diff(saved, moved) == ""
    moved["psf"]["wavelengthInNm"] = 750
    diff = yaml_io.semantic_diff(saved, moved)
    assert "-  wavelengthInNm: 1650" in diff and "+  wavelengthInNm: 750" in diff
