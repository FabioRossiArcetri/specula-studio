"""Unit tests for modules that do not depend on DearPyGui."""

import math

from specula_studio import render_scale
from specula_studio.core import syntax
from specula_studio.core.overrides import OverrideManager, combine_params


def test_typed_values_of_union_hints_are_not_text():
    # the installed catalog has hints like "float | List[float]" (WaveGenerator.constant)
    for hint in ("float | List[float]", "int | List[int]", "Optional[float]", "Any"):
        assert syntax.accepts_literal(hint)
    for hint in ("str", "List[str]", "DM", "SimulParams"):
        assert not syntax.accepts_literal(hint)
    assert syntax.parse_literal("1.0") == 1.0
    assert syntax.parse_literal(" [1, 2.5] ") == [1, 2.5]
    assert syntax.parse_literal("(1, 2)") == [1, 2]
    assert syntax.parse_literal("-inf") == -math.inf
    assert syntax.parse_literal("[inf, 0]") == [math.inf, 0]
    assert syntax.parse_literal("circular") is None
    assert syntax.parse_literal("'text'") is None
    assert syntax.parse_literal("") is None


def test_combine_params_override_remove_add():
    base = {"a": {"class": "X", "p": 1}, "b": {"class": "Y"}}
    extra = {"a_override": {"p": 2, "q": 3}, "remove": ["b"], "c": {"class": "Z"}}
    out = combine_params(base, extra)
    assert out == {"a": {"class": "X", "p": 2, "q": 3}, "c": {"class": "Z"}}
    assert base["a"]["p"] == 1  # base untouched


def test_override_manager_toggle_and_apply(tmp_path):
    f = tmp_path / "ov.yml"
    f.write_text("a_override:\n  p: 5\n")
    om = OverrideManager()
    assert om.load_overrides([str(f)]) == 1
    assert not om.any_enabled()
    om.toggle_override(str(f))
    assert om.get_enabled_overrides() == [str(f.resolve())]
    assert om.apply_overrides({"a": {"class": "X", "p": 1}})["a"]["p"] == 5
    restored = OverrideManager()
    restored.from_dict(om.to_dict())
    assert restored.is_enabled(str(f))


def test_render_scale_sizes():
    for size in render_scale.RENDER_SIZES:
        render_scale.set_size(size)
        assert render_scale.get_size() == size
        assert render_scale.font_size() > 0
    render_scale.set_size("NOPE")
    assert render_scale.get_size() == render_scale.RENDER_SIZES[-1]
    render_scale.set_size(render_scale.DEFAULT_RENDER_SIZE)
