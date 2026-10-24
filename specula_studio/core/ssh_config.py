"""
Host aliases defined in the OpenSSH client configuration.

The editor runs the system ``ssh`` / ``scp`` commands, so remote runs use
exactly the configuration those commands read: the user's
``~/.ssh/config`` (``%USERPROFILE%\\.ssh\\config`` on Windows).  Selecting an
alias passes it to ``ssh`` unchanged, so ``HostName``, ``User``, ``Port``,
``IdentityFile``, ``ProxyJump`` … all apply.

Only concrete aliases are listed: patterns (``*``, ``?``, ``!``) are skipped.
``Include`` directives are followed (relative paths are relative to
``~/.ssh``, globs allowed), ``Match`` blocks end the current ``Host`` block.
"""

from __future__ import annotations

import glob
import logging
import os
import pathlib
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_MAX_INCLUDE_DEPTH = 8


@dataclass
class SshHost:
    alias: str
    hostname: str = ""
    user: str = ""
    port: str = ""
    proxy_jump: str = ""
    source: str = ""
    options: dict = field(default_factory=dict)

    def describe(self) -> str:
        target = self.hostname or self.alias
        if self.user:
            target = f"{self.user}@{target}"
        if self.port:
            target += f":{self.port}"
        if self.proxy_jump:
            target += f" via {self.proxy_jump}"
        return target


def default_config_path() -> pathlib.Path:
    """The per-user OpenSSH client configuration file."""
    return pathlib.Path.home() / ".ssh" / "config"


def _split(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # "Keyword value" or "Keyword=value"
    for i, ch in enumerate(line):
        if ch.isspace() or ch == "=":
            key, rest = line[:i], line[i:].lstrip(" \t=").strip()
            return key.lower(), rest
    return line.lower(), ""


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _is_pattern(alias: str) -> bool:
    return any(c in alias for c in "*?!")


def list_ssh_hosts(path: os.PathLike | str | None = None) -> list[SshHost]:
    """Concrete host aliases of the ssh configuration, in file order."""
    path = pathlib.Path(path) if path else default_config_path()
    hosts: dict[str, SshHost] = {}
    _parse(path, hosts, depth=0)
    return list(hosts.values())


def _parse(path: pathlib.Path, hosts: dict, depth: int) -> None:
    if depth > _MAX_INCLUDE_DEPTH:
        log.warning("ssh config: Include nesting too deep at %s", path)
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    current: list[SshHost] = []
    for line in lines:
        kv = _split(line)
        if kv is None:
            continue
        key, value = kv
        if key == "include":
            for pattern in value.split():
                pattern = os.path.expanduser(_strip_quotes(pattern))
                if not os.path.isabs(pattern):
                    pattern = str(default_config_path().parent / pattern)
                for inc in sorted(glob.glob(pattern)):
                    _parse(pathlib.Path(inc), hosts, depth + 1)
            continue
        if key == "host":
            current = []
            for alias in value.split():
                alias = _strip_quotes(alias)
                if _is_pattern(alias):
                    continue
                # like ssh, the first definition of an alias wins
                current.append(hosts.setdefault(alias, SshHost(alias, source=str(path))))
            continue
        if key == "match":
            current = []
            continue
        for host in current:
            # first value wins, as in ssh
            host.options.setdefault(key, value)
            if key == "hostname" and not host.hostname:
                host.hostname = value
            elif key == "user" and not host.user:
                host.user = value
            elif key == "port" and not host.port:
                host.port = value
            elif key == "proxyjump" and not host.proxy_jump:
                host.proxy_jump = value
