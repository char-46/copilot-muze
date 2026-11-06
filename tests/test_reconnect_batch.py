"""Reconnect batch (2026-11-06): poisoned-session recovery, catch-up scoped
for risk control, reconnect notice fired exactly once, protocol flag file
that update.ps1 reads."""

import asyncio

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.milky.client import MilkyClient
from muze.provider.base import ProtocolError
from muze.store.db import open_db
from muze.store.repo import Repository


# ------------------------------------------------- session poisoning (live)
async def test_network_error_drops_the_session(monkeypatch):
    """Live 2026-11-06: milky restarted; the app's pooled keep-alive sockets
    were half-open, so POSTs hung to timeout forever (empty error) while
    fresh probes got 200. Any network error must drop the session so the
    next call builds a fresh connector."""
    c = MilkyClient(base_url="http://127.0.0.1:3010")

    calls = []

    class DeadSession:
        closed = False

        def post(self, url, **kw):
            calls.append(url)

            class Ctx:
                async def __aenter__(self_inner):
                    raise asyncio.TimeoutError()

                async def __aexit__(self_inner, *a):
                    return False

            return Ctx()

        async def close(self):
            self.closed = True

    dead = DeadSession()
    c._session = dead
    with pytest.raises(ProtocolError):
        await c.call("get_login_info", {})
    assert calls and dead.closed  # session dropped -> next call reconnects


async def test_ok_call_keeps_the_session(monkeypatch):
    c = MilkyClient(base_url="http://127.0.0.1:3010")

    class OkResp:
        status = 200

        async def json(self, content_type=None):
            return {"status": "ok", "retcode": 0, "data": {"uin": 1}}

    class OkCtx:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *a):
            return False

    class OkSession:
        closed = False

        def post(self, url, **kw):
            return OkCtx(OkResp())

    ok = OkSession()
    c._session = ok
    assert await c.call("get_login_info", {}) == {"uin": 1}
    assert c._session is ok  # healthy: nothing rebuilt


# --------------------------------------------- catch-up scope (防风控)
async def test_catchup_targets_are_scoped_by_the_offline_window(tmp_path):
    """A 1.5-day outage must not burst-pull the whole roster right after
    login; targets come from the local DB, scoped to the window (user
    direction 2026-11-06: 补拉也要防止风控)."""
    import time

    from muze.ingest.backfill import Backfiller

    conn = await open_db(str(tmp_path / "b.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    cfg.control.controller = [777]
    cfg.control.control_group = 4242
    now = int(time.time())
    # active an hour ago vs idle for 4 days (direct rows: no pipeline needed)
    for scene, peer, ts in (("friend", 1, now - 3600), ("friend", 2, now - 4 * 86400)):
        await conn.execute(
            "INSERT INTO conversations (scene, peer_id, name, last_seq, last_active_at, last_op_at) "
            "VALUES (?, ?, '', ?, ?, ?)",
            (scene, peer, 10, ts, ts),
        )
    await conn.commit()

    class NoAdapter:
        async def get_group_list(self):
            raise AssertionError("catch-up must not call the live roster")

        async def get_friend_list(self):
            raise AssertionError("catch-up must not call the live roster")

    bf = Backfiller(NoAdapter(), repo, cfg, None, ChatLog())
    targets = await bf.catchup_targets(active_within_h=36.0)
    assert ("friend", 1) in targets  # active around the gap: pulled
    assert ("friend", 2) not in targets  # long idle: left for the 00:05 sweep
    assert ("friend", 777) in targets and ("group", 4242) in targets  # control always
    await conn.close()


# ------------------------------------------------ reconnect notice once
async def test_notify_controller_fires_exactly_once(tmp_path):
    """Live 2026-11-06: milky came up slow, the send was actually delivered
    but judged failed, and send_text's chunked fallback re-sent the whole
    reconnect notice back-to-back. System notices must never double."""
    conn = await open_db(str(tmp_path / "n.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    cfg.control.controller = [777]

    sent: list = []

    class NoAdapter:
        async def send_private_message(self, user_id, segments):
            sent.append((user_id, segments))
            raise ProtocolError("PMHQ send failed")

    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    await service.notify_controller("✅ 协议侧回来了")
    assert len(sent) == 1  # one attempt, no fallback re-send
    await conn.close()


# ------------------------------------------------ protocol flag file
async def test_protocol_flag_file_tracks_online_state(tmp_path):
    from muze.main import App

    a = App.__new__(App)
    a.cfg = MuzeConfig()
    a.cfg.storage.data_dir = str(tmp_path)
    flag = tmp_path / "protocol_online"

    a._write_protocol_flag(True)
    assert flag.exists()  # update.ps1 sees "online: updates allowed"
    a._write_protocol_flag(False)
    assert not flag.exists()  # offline: update.ps1 suspends
