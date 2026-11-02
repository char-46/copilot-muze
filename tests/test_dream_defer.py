"""Dream deferral (user directions 2026-11-03):
1. 每天消息零点五分没拉完的时候做梦延期 -- scheduler ticks defer while a
   roaming pull is in flight;
2. 发版要等客户端做完梦 -- /restart & /full-restart queue behind a running
   nightly dream (hard cap 30min);
3. 过期链接不再重试 -- failed downloads stop being retried after
   ingest.media_max_retries attempts (schema v21 media.attempts)."""

import asyncio
import time
import types

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.scheduler import Scheduler
from muze.store.db import open_db
from muze.store.repo import Repository


def _ctx():
    return ControlContext(source="cli", chat_key="cli", scene="friend", peer_id=1)


class GatedSvc:
    """execute() blocks on a gate; counts entries."""

    def __init__(self):
        self.cfg = MuzeConfig()
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = 0
        self.delivered: list[str] = []
        from muze.core.service import CommandOutcome

        self._CommandOutcome = CommandOutcome

    async def execute(self, prompt, ctx):
        self.entered += 1
        await self.gate.wait()
        return self._CommandOutcome(f"ok: {prompt}")

    async def deliver(self, ctx, outcome):
        self.delivered.append(outcome.text)
        return True


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "s.db"))
    repo = Repository(conn)
    yield repo, conn
    await conn.close()


# ------------------------------------------------- 1. pull in flight -> defer
async def test_tick_defers_while_backfill_running(env):
    repo, conn = env
    svc = GatedSvc()
    await repo.create_task("once", "/digest", "friend", 1, int(time.time()) - 5,
                           0, "builtin", "")
    busy = asyncio.Event()  # not set = pull still running

    async def gate():
        return busy.is_set()

    s = Scheduler(repo, svc, ChatLog(), gate=gate)
    assert await s.tick() == 0  # claimed nothing: dream defers behind the pull
    assert svc.entered == 0
    assert s._gate_stalls == 1
    busy.set()  # pull landed
    assert await s.tick() >= 1
    await s.drain()
    assert svc.entered == 1
    await s.stop()


async def test_gate_probe_failure_never_stalls_the_schedule(env):
    repo, conn = env
    svc = GatedSvc()
    await repo.create_task("once", "/digest", "friend", 1, int(time.time()) - 5,
                           0, "builtin", "")

    async def broken():
        raise RuntimeError("probe died")

    s = Scheduler(repo, svc, ChatLog(), gate=broken)
    assert await s.tick() >= 1  # a broken gate opens the schedule
    await s.drain()
    await s.stop()


# ------------------------------------------------- 2. restart waits for dream
async def _dream_scheduler(repo, svc):
    s = Scheduler(repo, svc, ChatLog())
    gate = asyncio.Event()
    runner = asyncio.create_task(gate.wait())
    s._digest_runners[99] = (runner, time.time() - 120)  # "dreaming for 2min"
    return s, runner, gate


async def test_restart_defers_behind_running_dream(env, tmp_path):
    repo, conn = env

    class NoAdapter:
        pass

    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    calls: list[str] = []
    service.runtime.request_restart = lambda: calls.append("restart")
    svc = GatedSvc()
    s, runner, gate = await _dream_scheduler(repo, svc)
    service.runtime.scheduler = s

    out = await service.run_command("restart", _ctx())
    assert "已排队" in out.text and "做梦" in out.text
    assert calls == []  # nothing restarted mid-dream
    # a second request while queued says so and still doesn't restart
    out2 = await service.run_command("restart", _ctx())
    assert "已在队列" in out2.text and calls == []

    gate.set()  # dream finishes
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.02)
    assert calls == ["restart"]  # the queued flip fired
    await s.stop()


async def test_restart_proceeds_when_no_dream(env, tmp_path):
    repo, conn = env

    class NoAdapter:
        pass

    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    calls: list[str] = []
    service.runtime.request_restart = lambda: calls.append("restart")
    service.runtime.scheduler = Scheduler(repo, GatedSvc(), ChatLog())
    out = await service.run_command("restart", _ctx())
    assert "已请求重启" in out.text and calls == ["restart"]


# ------------------------------------------------- 3. expired links stop
async def test_media_retry_cap_parks_dead_rows(env):
    repo, conn = env
    for i, attempts in enumerate(((1, 2), (2, 3), (3, 0)), start=1):
        status, att = attempts
        await conn.execute(
            "INSERT INTO media (scene,peer_id,message_seq,seg_index,kind,status,error,"
            "attempts,created_at,updated_at) VALUES ('friend',1,?,0,'image','failed','',?,0,0)",
            (i, att),
        )
    await conn.commit()
    got = await repo.media_pending(10, max_attempts=3)
    atts = sorted(r["attempts"] for r in got)
    assert atts == [0, 2]  # 3-attempt row is parked; under-cap rows continue
    assert await repo.media_pending(10, max_attempts=0) and True  # 0 = forever


async def test_download_failure_bumps_attempts(env):
    """download_row counts each failure so the cap can see it."""
    repo, conn = env
    await conn.execute(
        "INSERT INTO media (scene,peer_id,message_seq,seg_index,kind,resource_id,status,"
        "created_at,updated_at) VALUES ('friend',1,7,0,'image','res7','pending',0,0)"
    )
    await conn.commit()
    row = (await repo.media_pending(5))[0]

    class ExplodingAdapter:
        async def call(self, *a, **kw):
            raise RuntimeError("nope")

        def cached_friend_name(self, uid):
            return ""

    from muze.store.media import MediaStore

    store = MediaStore.__new__(MediaStore)
    store.repo = repo
    store.adapter = ExplodingAdapter()
    store.blobs = types.SimpleNamespace(resolve=lambda ref: None)  # no stored file

    async def boom(row, hint_url=None):
        raise RuntimeError("ftn says nothing (empty error, live 2026-11-02)")

    store.resolve_url = boom
    await store.download_row(row)
    upd = await repo.media_by_id(row["id"])
    assert upd["status"] == "failed" and upd["attempts"] == 1
    # second failure -> 2; after the third the row leaves the retry queue
    await store.download_row(upd)
    await store.download_row(await repo.media_by_id(row["id"]))
    upd = await repo.media_by_id(row["id"])
    assert upd["attempts"] == 3
    assert all(r["id"] != row["id"] for r in await repo.media_pending(10, max_attempts=3))


async def test_v21_migration_adds_attempts_column(tmp_path):
    """Old DBs (schema v20) gain media.attempts on open; fresh DBs have it."""
    conn = await open_db(str(tmp_path / "m.db"))
    try:
        async with conn.execute("PRAGMA table_info(media)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "attempts" in cols
        await conn.execute(
            "INSERT INTO media (scene,peer_id,message_seq,seg_index,kind,status,"
            "created_at,updated_at) VALUES ('friend',1,1,0,'image','pending',0,0)"
        )
        await conn.commit()
        row = (await Repository(conn).media_pending(5))[0]
        assert row["attempts"] == 0  # default backfills cleanly
    finally:
        await conn.close()
