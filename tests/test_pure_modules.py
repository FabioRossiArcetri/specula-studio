"""Unit tests for modules that do not depend on DearPyGui."""

from specula_studio import render_scale
from specula_studio.core.overrides import OverrideManager, combine_params


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
