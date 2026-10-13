"""SshTunnel: command construction + config defaults (no real ssh spawned)."""

from __future__ import annotations

from pathlib import Path

import pytest

from muze.config import TunnelConfig, WebConfig
from muze.tunnel import SshTunnel


def test_tunnel_config_defaults():
    t = TunnelConfig()
    assert t.enabled is False and t.host == "" and t.user == "root"
    w = WebConfig()
    assert w.tunnel.enabled is False
    assert w.port == 8300


def test_tunnel_requires_host():
    with pytest.raises(ValueError, match="host is empty"):
        SshTunnel(TunnelConfig(enabled=True), 8300, Path("/tmp/x"))


def test_tunnel_remote_port_defaults_to_web_port(tmp_path):
    key = tmp_path / ".ssh" / "muze_tunnel"
    key.parent.mkdir(parents=True)
    key.write_text("fake")
    tun = SshTunnel(
        TunnelConfig(enabled=True, host="relay.example.com"), 8300, tmp_path
    )
    assert tun.remote_port == 8300
    assert tun.key_file == key

    tun2 = SshTunnel(
        TunnelConfig(enabled=True, host="r", remote_port=18000), 8300, tmp_path
    )
    assert tun2.remote_port == 18000


def test_tunnel_command_shape(tmp_path):
    key = tmp_path / "k"
    key.write_text("fake")
    cfg = TunnelConfig(
        enabled=True, host="relay.example.com", port=2222, user="op",
        key_file=str(key), remote_port=18000,
    )
    tun = SshTunnel(cfg, 8300, tmp_path)
    cmd = tun.command()
    assert cmd[0] == "ssh" and "-N" in cmd and "-T" in cmd
    for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "ExitOnForwardFailure=yes",
                "ServerAliveInterval=30", "StrictHostKeyChecking=accept-new"):
        assert opt in cmd
    assert "-R" in cmd
    r = cmd[cmd.index("-R") + 1]
    assert r == "127.0.0.1:18000:127.0.0.1:8300"
    assert cmd[cmd.index("-p") + 1] == "2222"
    assert cmd[-1] == "op@relay.example.com"


def test_tunnel_missing_key_hint(tmp_path):
    tun = SshTunnel(TunnelConfig(enabled=True, host="r"), 8300, tmp_path)
    with pytest.raises(FileNotFoundError, match="permitlisten"):
        tun.command()
