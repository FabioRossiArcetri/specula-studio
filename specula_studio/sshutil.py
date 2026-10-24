"""
Running the system ``ssh`` / ``scp`` from the GUI.

A GUI cannot answer password, passphrase or host-key prompts (ssh would wait
on a console nobody sees), so every call uses ``BatchMode=yes``: it either
logs in with a key (or an ssh-agent / certificate) or fails at once, and the
failure is turned into an actionable message.
"""

from __future__ import annotations

import logging
import socket
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]

KEY_AUTH_HINT = (
    "ssh must log in without prompts: install your public key on the server "
    "once, e.g. from cmd: type %USERPROFILE%\\.ssh\\id_ed25519.pub | ssh <host> "
    "\"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys\" (PowerShell: "
    "$env:USERPROFILE instead of %USERPROFILE%; Linux/macOS: ssh-copy-id <host>), "
    "or load a passphrase-protected key into ssh-agent with ssh-add."
)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(argv: list[str], timeout: float = 120, input_text: str | None = None
                ) -> CommandResult:
    """Run a local command (ssh, scp, step …) and capture its output."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              input=input_text, stdin=None if input_text is not None
                              else subprocess.DEVNULL)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:.0f} s: {argv[0]}")


def explain_ssh_failure(result: CommandResult, auth_hint: str = KEY_AUTH_HINT) -> str:
    """A short, actionable explanation of a failed ssh/scp call."""
    err = (result.stderr or result.stdout).strip()
    low = err.lower()
    if "permission denied" in low or "no supported authentication" in low:
        detail = err.splitlines()[-1].rstrip(".") if err else "denied"
        return f"Authentication failed ({detail}). {auth_hint}"
    if "could not resolve" in low or "name or service not known" in low:
        return f"Unknown host: {err}"
    if "timed out" in low:
        return f"Connection timed out: {err}"
    if "host key verification failed" in low:
        return ("Host key verification failed: connect once with ssh from a terminal "
                "to accept (or update) the host key.")
    return err or f"exit code {result.returncode}"


def ssh(target: str, command: str, timeout: float = 60) -> CommandResult:
    return run_command(["ssh", *SSH_OPTIONS, target, command], timeout=timeout)


def check_login(target: str) -> tuple[bool, str]:
    """(ok, message) for a non-interactive login to *target*."""
    res = ssh(target, "echo ok:$(hostname)", timeout=40)
    if res.ok:
        host = res.stdout.strip().partition(":")[2] or target
        return True, f"Logged in to {host} without prompts"
    return False, explain_ssh_failure(res)


def resolve_host(hostname: str) -> str:
    """IP address of *hostname* ([user@]host or ssh alias) as seen from here."""
    try:
        socket.inet_aton(hostname)
        return hostname                       # already an IPv4 address
    except (OSError, ValueError):
        pass

    # ssh aliases (~/.ssh/config): the HostName ssh would connect to.  `ssh -G`
    # only evaluates the local configuration — no network, no login.
    target = hostname.rpartition("@")[2]
    result = run_command(["ssh", "-G", target], timeout=10)
    if result.ok:
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            if key == "hostname" and value.strip():
                target = value.strip()
                break
    try:
        socket.inet_aton(target)
        log.info("Resolved '%s' -> %s (ssh config)", hostname, target)
        return target
    except (OSError, ValueError):
        hostname = target

    try:
        ip = socket.gethostbyname(hostname)
        log.info("Resolved '%s' -> %s (DNS)", hostname, ip)
        return ip
    except OSError:
        log.warning("Could not resolve '%s', using it as-is", hostname)
        return hostname
