"""Non-interactive ssh helpers and remote host resolution."""

from specula_studio import sshutil
from specula_studio.sshutil import CommandResult


def test_explain_ssh_failure_messages():
    denied = CommandResult(255, "", "frossi@1.2.3.4: Permission denied (publickey,password).")
    msg = sshutil.explain_ssh_failure(denied)
    assert msg.startswith("Authentication failed") and "authorized_keys" in msg
    assert "Unknown host" in sshutil.explain_ssh_failure(
        CommandResult(255, "", "ssh: Could not resolve hostname nope"))
    assert "timed out" in sshutil.explain_ssh_failure(
        CommandResult(124, "", "timed out after 20 s: ssh"))
    assert "host key" in sshutil.explain_ssh_failure(
        CommandResult(255, "", "Host key verification failed."))


def test_check_login_is_batch_mode(monkeypatch):
    calls = []

    def fake(argv, timeout=0, input_text=None):
        calls.append(argv)
        return CommandResult(0, "ok:sauron\n", "")

    monkeypatch.setattr(sshutil, "run_command", fake)
    ok, msg = sshutil.check_login("frossi@sauron")
    assert ok and "sauron" in msg
    assert "BatchMode=yes" in calls[0] and calls[0][-2] == "frossi@sauron"


def test_resolve_alias_through_ssh_config_without_login(monkeypatch):
    calls = []

    def fake(argv, timeout=0, input_text=None):
        calls.append(argv)
        return CommandResult(0, "user frossi\nhostname 193.206.155.190\nport 22\n", "")

    monkeypatch.setattr(sshutil, "run_command", fake)
    assert sshutil.resolve_host("frossi@sauron") == "193.206.155.190"
    assert calls == [["ssh", "-G", "sauron"]]            # local config only, no login
    assert sshutil.resolve_host("10.0.0.1") == "10.0.0.1"
