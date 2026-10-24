"""Parsing host aliases from the OpenSSH client configuration."""

import textwrap

from specula_studio.core import ssh_config


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_lists_concrete_aliases_with_their_options(tmp_path):
    cfg = _write(tmp_path / "config", """
        # comment
        Host gpu1 gpu1-alt
            HostName gpu1.example.org
            User fabio
            Port 2222

        Host cluster
          HostName=cluster.example.org
          ProxyJump gw
          User first
          User second          # first value wins, as in ssh

        Host *.internal !bad
            User nobody

        Host *
            ServerAliveInterval 30
        """)
    hosts = {h.alias: h for h in ssh_config.list_ssh_hosts(cfg)}
    assert list(hosts) == ["gpu1", "gpu1-alt", "cluster"]       # patterns skipped
    assert hosts["gpu1"].describe() == "fabio@gpu1.example.org:2222"
    assert hosts["gpu1-alt"].hostname == "gpu1.example.org"
    assert hosts["cluster"].user == "first"
    assert hosts["cluster"].describe() == "first@cluster.example.org via gw"


def test_match_ends_host_block_and_include_is_followed(tmp_path):
    inc_dir = tmp_path / "conf.d"
    inc_dir.mkdir()
    _write(inc_dir / "a.conf", """
        Host included
            HostName inc.example.org
        """)
    cfg = _write(tmp_path / "config", f"""
        Include "{inc_dir.as_posix()}/*.conf"
        Host first
            HostName one
        Match user root
            HostName ignored
        """)
    hosts = {h.alias: h for h in ssh_config.list_ssh_hosts(cfg)}
    assert hosts["included"].hostname == "inc.example.org"
    assert hosts["first"].hostname == "one"


def test_missing_config_gives_no_hosts(tmp_path):
    assert ssh_config.list_ssh_hosts(tmp_path / "nope") == []
