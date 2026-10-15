"""
End-to-end runs with the installed SPECULA (skipped when it is missing):
in-process backend (probes, stepping, abort, overrides) and the local
`specula` subprocess with an injected DisplayServer.
"""

import importlib.util
import json
import shutil
import subprocess
import sys

import pytest

from tests.conftest import REPO

pytestmark = pytest.mark.skipif(importlib.util.find_spec("specula") is None,
                                reason="SPECULA not installed")


def _helper(module):
    proc = subprocess.run([sys.executable, "-m", module], cwd=REPO,
                          capture_output=True, text=True, timeout=600)
    assert "@@JSON@@" in proc.stdout, proc.stderr[-4000:]
    return json.loads(proc.stdout.split("@@JSON@@", 1)[1])


@pytest.fixture(scope="module")
def inprocess():
    return _helper("tests.helpers.inprocess_run")


def test_inprocess_free_run_sees_every_step(inprocess):
    res = inprocess["free_run"]
    assert res["finished"] and res["hooks_restored"], res["terminal"]
    assert res["values"] == [3.0 + i for i in range(10)], res["terminal"]


def test_inprocess_stepping_late_monitor_and_abort(inprocess):
    res = inprocess["stepping"]
    assert res["finished"] and res["hooks_restored"], res["terminal"]
    values = res["values"]
    assert values[-1] == "after_step3"
    assert len(values) >= 4 and values[0] == 4.0, res["terminal"]
    assert "aborted" in res["terminal"]


def test_inprocess_override_files_applied_by_specula(inprocess):
    res = inprocess["override"]
    assert res["finished"], res["terminal"]
    assert res["values"] and set(res["values"]) == {7.0}, res["terminal"]


@pytest.mark.skipif(shutil.which("specula") is None, reason="specula command not found")
def test_local_subprocess_run_announces_display_server_port():
    res = _helper("tests.helpers.subprocess_run")
    assert res["finished"], res["terminal"]
    assert res["ports"] and res["ports"][0] > 0, res["terminal"]
