"""
Regenerate tests/golden from the pure core round trip.

Only run this when a change to the export format is intentional, and review
the resulting diff:   python -m tests.helpers.regen_golden && git diff tests/golden
"""

import yaml

from specula_studio.core import yaml_io
from specula_studio.core.templates import load_bundled_templates
from tests.conftest import FIXTURES, GOLDEN

OVERRIDE_ONLY = {"params_scao_sh_test_rec.yml", "params_ov_scao_mpi.yml", "calib_scao_rec.yml"}


def base_fixtures():
    return [p for p in sorted(FIXTURES.glob("*.yml")) if p.name not in OVERRIDE_ONLY]


def plain(obj):
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    return obj


def core_export(path, include_defaults):
    data, proc = load_bundled_templates()
    graph = yaml_io.load_graph(yaml_io.read_yaml_file(path), {**data, **proc})
    out = plain(yaml_io.export_graph(graph, include_defaults=include_defaults))
    for node in out.values():
        node.pop("gui_pos", None)
    return out


def main():
    for fx in base_fixtures():
        for variant, incl in (("minimal", False), ("defaults", True)):
            with open(GOLDEN / f"{fx.stem}.{variant}.yml", "w") as f:
                yaml.safe_dump(core_export(fx, incl), f, sort_keys=False,
                               default_flow_style=None)
        print("regenerated", fx.stem)


if __name__ == "__main__":
    main()
