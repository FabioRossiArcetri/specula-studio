"""Headless smoke test of the real UI construction (see helpers/real_dpg_smoke)."""

import subprocess
import sys

from tests.conftest import FIXTURES, REPO


def test_editor_builds_with_real_dearpygui():
    proc = subprocess.run(
        [sys.executable, "-m", "tests.helpers.real_dpg_smoke",
         str(FIXTURES / "params_scao_sh_test.yml")],
        cwd=REPO, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "@@STEPS@@" in proc.stdout, proc.stderr[-4000:]
    steps = proc.stdout.split("@@STEPS@@", 1)[1].strip().split(",")
    assert steps[-1] == "new", steps
