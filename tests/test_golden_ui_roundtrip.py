"""
Regression net: loading a SPECULA config through the editor and exporting it
again must reproduce the golden files captured from the pre-refactor code.
"""

import json
import subprocess
import sys

import pytest
import yaml

from tests.conftest import FIXTURES, GOLDEN, REPO

BASE_FIXTURES = sorted(
    p for p in FIXTURES.glob("*.yml")
    if p.name not in {"params_scao_sh_test_rec.yml", "params_ov_scao_mpi.yml",
                      "calib_scao_rec.yml"}
)


@pytest.fixture(scope="module")
def ui_exports():
    proc = subprocess.run(
        [sys.executable, "-m", "tests.helpers.ui_export", *map(str, BASE_FIXTURES)],
        cwd=REPO, capture_output=True, text=True, timeout=600,
    )
    assert "@@JSON@@" in proc.stdout, proc.stderr[-4000:]
    return json.loads(proc.stdout.split("@@JSON@@", 1)[1])


@pytest.mark.parametrize("fixture", BASE_FIXTURES, ids=lambda p: p.stem)
@pytest.mark.parametrize("variant", ["minimal", "defaults"])
def test_ui_roundtrip_matches_golden(ui_exports, fixture, variant):
    golden = yaml.safe_load((GOLDEN / f"{fixture.stem}.{variant}.yml").read_text())
    assert ui_exports[str(fixture)][variant] == golden
