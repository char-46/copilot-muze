"""SshTunnel: command construction + config defaults (no real ssh spawned)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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
                "ServerAliveInterval=15", "ServerAliveCountMax=4",
                "TCPKeepAlive=yes", "StrictHostKeyChecking=accept-new"):
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


@pytest.mark.skipif(sys.platform != "win32", reason="icacls ACL fix is win32-only")
def test_tunnel_key_acl_auto_tightened(tmp_path):
    key = _mkkey(tmp_path)
    # simulate the copy-from-elsewhere state: explicit group ACEs, exactly
    # what /inheritance:r alone would NOT remove
    for sid in ("*S-1-5-11", "*S-1-5-32-545"):
        subprocess.run(["icacls", str(key), "/grant", f"{sid}:R"],
                       capture_output=True, check=False)
    tun = SshTunnel(TunnelConfig(enabled=True, host="r"), 8300, tmp_path)
    tun._tighten_key_acl()
    acl = subprocess.run(["icacls", str(key)], capture_output=True,
                         text=True, check=False).stdout
    assert "S-1-5-11" not in acl
    assert (os.environ.get("USERNAME") or "").lower() in acl.lower()


def test_tunnel_status_fields(tmp_path):
    _mkkey(tmp_path)
    tun = SshTunnel(TunnelConfig(enabled=True, host="r", remote_port=18300), 8300, tmp_path)
    s = tun.status()
    assert s["connected"] is False and s["restarts"] == 0
    assert s["forwards"] == ["127.0.0.1:18300 -> 127.0.0.1:8300"]
    assert "r" in s["host"]
    # a fake live process reports connected with uptime
    class _P:
        returncode = None
    tun._proc = _P()
    tun._connected_at = 100.0
    import time as _t
    s2 = tun.status()
    assert s2["connected"] is True
    assert s2["uptime_sec"] >= _t.time() - 100.0 - 5


async def test_tunnel_restart_cycle(tmp_path, monkeypatch):
    """restart() stops and respawns even after a full stop()."""
    _mkkey(tmp_path)
    tun = SshTunnel(TunnelConfig(enabled=True, host="r"), 8300, tmp_path)
    started = []

    class _FakeProc:
        returncode = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return 0

    async def _spawn(*a, **k):
        started.append(1)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    await tun.start()
    await asyncio.sleep(0.05)  # let the supervisor spawn once
    assert tun.status()["connected"] is True
    await tun.restart()
    await asyncio.sleep(0.05)
    assert len(started) == 2  # respawned
    assert tun._stopping is False
    await tun.stop()


async def test_tun_command(tmp_path):
    from muze.chatlog import ChatLog
    from muze.config import MuzeConfig
    from muze.core.service import ControlService, Runtime
    from muze.store.db import open_db
    from muze.store.repo import Repository
    from test_permissions import Recorder

    conn = await open_db(str(tmp_path / "t.db"))
    try:
        repo = Repository(conn)
        svc = ControlService(MuzeConfig(), Recorder(), repo, ChatLog(), Runtime())

        class _FakeTun:
            def status(self):
                return {"host": "root@r:22", "connected": True, "uptime_sec": 42.0,
                        "restarts": 1, "last_error": "", "forwards": ["127.0.0.1:18300 -> 127.0.0.1:8300"]}
            async def restart(self):
                self.restarted = True
            async def stop(self):
                pass
            async def start(self):
                pass
        ft = _FakeTun()
        svc.runtime.tunnel = ft
        out = await svc.execute("/tun", ctx := type("C", (), {"source": "cli", "is_local": True, "user_label": "t", "chat_key": "cli", "scene": "", "peer_id": 0, "auto_confirm": True, "qq_reachable": False, "reply_seq": 0})())
        assert "已连接" in out.text and "18300" in out.text
        out2 = await svc.execute("/tun restart", ctx)
        assert "已重启" in out2.text and getattr(ft, "restarted", False)
    finally:
        await conn.close()
