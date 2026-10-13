"""SshTunnel: command construction + config defaults (no real ssh spawned)."""

from __future__ import annotations

from pathlib import Path

import pytest

from muze.config import TunnelConfig, TunnelForward, WebConfig
from muze.tunnel import SshTunnel


def test_tunnel_config_defaults():
    t = TunnelConfig()
    assert t.enabled is False and t.host == "" and t.user == "root"
    assert t.forwards == [] and t.remote_port == 0
    w = WebConfig()
    assert w.tunnel.enabled is False
    assert w.port == 8300


def test_tunnel_requires_host():
    with pytest.raises(ValueError, match="host is empty"):
        SshTunnel(TunnelConfig(enabled=True), 8300, Path("/tmp/x"))


def _mkkey(tmp_path: Path) -> Path:
    key = tmp_path / ".ssh" / "muze_tunnel"
    key.parent.mkdir(parents=True)
    key.write_text("fake")
    return key


def test_tunnel_webui_only_default(tmp_path):
    _mkkey(tmp_path)
    tun = SshTunnel(TunnelConfig(enabled=True, host="r"), 8300, tmp_path)
    assert [ (f.local_port, f.remote_port) for f in tun.forwards ] == [(8300, 8300)]
    tun2 = SshTunnel(TunnelConfig(enabled=True, host="r", remote_port=18000), 8300, tmp_path)
    assert tun2.forwards[0].remote_port == 18000


def test_tunnel_duplicate_remote_ports_rejected(tmp_path):
    _mkkey(tmp_path)
    cfg = TunnelConfig(
        enabled=True, host="r",
        forwards=[
            TunnelForward(local_port=8300, remote_port=18300),
            TunnelForward(local_port=22, remote_port=18300),
        ],
    )
    with pytest.raises(ValueError, match="duplicate remote ports"):
        SshTunnel(cfg, 8300, tmp_path)


def test_tunnel_command_shape(tmp_path):
    _mkkey(tmp_path)
    cfg = TunnelConfig(
        enabled=True, host="relay.example.com", port=2222, user="op",
        forwards=[
            TunnelForward(local_port=8300, remote_port=18300),
            TunnelForward(local_port=22, remote_port=18301),
        ],
    )
    tun = SshTunnel(cfg, 8300, tmp_path)
    cmd = tun.command()
    assert cmd[0] == "ssh" and "-N" in cmd and "-T" in cmd
    for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "ExitOnForwardFailure=yes",
                "ServerAliveInterval=30", "StrictHostKeyChecking=accept-new"):
        assert opt in cmd
    rs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-R"]
    assert rs == ["127.0.0.1:18300:127.0.0.1:8300",
                  "127.0.0.1:18301:127.0.0.1:22"]
    assert cmd[cmd.index("-p") + 1] == "2222"
    assert cmd[-1] == "op@relay.example.com"


def test_tunnel_missing_key_hint(tmp_path):
    tun = SshTunnel(TunnelConfig(enabled=True, host="r"), 8300, tmp_path)
    with pytest.raises(FileNotFoundError, match="permitlisten"):
        tun.command()
