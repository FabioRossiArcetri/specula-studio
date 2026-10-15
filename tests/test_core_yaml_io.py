"""Pure-core load/export tests (no DearPyGui involved)."""

import pytest
import yaml

from specula_studio.core import syntax, yaml_io
from specula_studio.core.templates import load_bundled_templates
from tests.conftest import FIXTURES, GOLDEN
from tests.helpers.regen_golden import base_fixtures, core_export


@pytest.mark.parametrize("fixture", base_fixtures(), ids=lambda p: p.stem)
@pytest.mark.parametrize("variant", ["minimal", "defaults"])
def test_core_roundtrip_matches_golden(fixture, variant):
    golden = yaml.safe_load((GOLDEN / f"{fixture.stem}.{variant}.yml").read_text())
    assert core_export(fixture, variant == "defaults") == golden


@pytest.mark.parametrize("fixture", base_fixtures(), ids=lambda p: p.stem)
def test_roundtrip_is_semantically_lossless(fixture):
    """Everything in the source survives, except values equal to a default."""
    src = yaml.safe_load(fixture.read_text())
    out = core_export(fixture, include_defaults=False)
    assert list(out) == list(src)
    for name, obj in src.items():
        for key, val in obj.items():
            if key not in out[name]:
                continue          # dropped because it equals the template default
            got = out[name][key]
            if key == "inputs":
                norm = {k: v if isinstance(v, list) else [v] for k, v in val.items()}
                got = {k: v if isinstance(v, list) else [v] for k, v in got.items()}
                assert got == norm, (name, key)
            else:
                assert got == val, (name, key)


def test_export_is_stable_after_reload():
    data, proc = load_bundled_templates()
    templates = {**data, **proc}
    src = yaml_io.read_yaml_file(FIXTURES / "params_scao_pyr_test.yml")
    once = yaml_io.export_graph(yaml_io.load_graph(src, templates))
    twice = yaml_io.export_graph(yaml_io.load_graph(once, templates))
    assert yaml_io.dump_yaml(once) == yaml_io.dump_yaml(twice)


def test_dump_yaml_is_safe_loadable():
    import numpy as np
    text = yaml_io.dump_yaml({"a": {"x": np.float32(1.5), "t": (1, 2), "inf": float("inf"),
                                    "arr": np.arange(3)}})
    assert yaml.safe_load(text) == {"a": {"x": 1.5, "t": [1, 2], "inf": float("inf"),
                                          "arr": [0, 1, 2]}}


@pytest.mark.parametrize("text,expected", [
    ("dm.out_layer:-1", ("dm", "out_layer", -1, None)),
    ("res_sr-psf.out_sr", ("psf", "out_sr", 0, "res_sr")),
    ("pupilstop", ("pupilstop", "ref", 0, None)),
    ("a.b.c", ("a", "b.c", 0, None)),
])
def test_parse_and_format_source(text, expected):
    src = syntax.parse_source(text)
    assert tuple(src) == expected
    assert syntax.format_source(*src) == text


def test_unique_name():
    assert syntax.unique_name("dm", set()) == "dm"
    assert syntax.unique_name("dm", {"dm", "dm_1"}) == "dm_2"
