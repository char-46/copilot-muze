"""Crash-loop anti-spam (live 2026-11-04 06:00-07:40): an interrupted task
reset to due-now on EVERY boot re-delivered the morning digest ~19 times
because the 5-minute autostart watch kept resurrecting the process. An
interval task may re-fire due-now at most MAX_DUE_NOW_RESETS times per
day, then it re-anchors to its natural next slot."""

import asyncio
import time

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext
from muze.scheduler import Scheduler
from muze.store.db import open_db
from muze.store.repo import Repository


def _ctx():
    return ControlContext(source="cli", chat_key="cli", scene="friend", peer_id=1)


class HangSvc:
    """execute() hangs on a gate so the run is always 'in flight' at stop()."""

    def __init__(self):
        self.cfg = MuzeConfig()

    async def execute(self, prompt, ctx):
        await asyncio.Event().wait()  # never completes on its own
        return ControlOutcome("no")


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "s.db"))
    repo = Repository(conn)
    yield repo, conn
    await conn.close()


async def _morning_row(repo, note=Scheduler.MORNING_NOTE):
    tid = await repo.create_task(
        "interval", "/dream morning", "friend", 1,
        int(time.time()) - 5, 86400, "builtin", note,
    )
    return await repo.get_task(tid)


async def _stop_with_interrupted(s):
    """stop() with an in-flight run -> returns after the reset/re-anchor."""
    t = asyncio.create_task(s.stop())
    while not t.done():
        await asyncio.sleep(0.02)
    await t


async def test_interrupted_interval_task_reresets_only_twice_per_day(env):
    repo, conn = env
    row = await _morning_row(repo)
    s = Scheduler(repo, HangSvc(), ChatLog())
    await s.tick()
    assert row["id"] in s._inflight
    # 1st + 2nd interruption today: due-now (a restart must not lose the task)
    await _stop_with_interrupted(s)
    t1 = await repo.get_task(row["id"])
    assert t1["next_run_at"] <= int(time.time()) + 2
    s2 = Scheduler(repo, HangSvc(), ChatLog())
    await s2.tick()
    await _stop_with_interrupted(s2)
    t2 = await repo.get_task(row["id"])
    assert t2["next_run_at"] <= int(time.time()) + 2

    # 3rd interruption: re-anchored to tomorrow's slot, NOT due-now
    s3 = Scheduler(repo, HangSvc(), ChatLog())
    await s3.tick()
    await _stop_with_interrupted(s3)
    t3 = await repo.get_task(row["id"])
    assert t3["next_run_at"] > int(time.time()) + 3600
    assert t3["status"] == "active"
    # ...and the crash-loop can no longer re-deliver via due-now today
    s4 = Scheduler(repo, HangSvc(), ChatLog())
    claimed = await s4.tick()
    assert claimed == 0  # not due until tomorrow's slot: nothing to re-fire
    assert (await repo.get_task(row["id"]))["status"] == "active"


async def test_reanchor_uses_the_configured_wall_clock_slot(env):
    repo, conn = env
    row = await _morning_row(repo)
    s = Scheduler(repo, HangSvc(), ChatLog())
    await s.tick()
    # burn the cap
    for _ in range(3):
        await _stop_with_interrupted(s)
        s = Scheduler(repo, HangSvc(), ChatLog())
        await s.tick()
    await _stop_with_interrupted(s)
    t = await repo.get_task(row["id"])
    from muze.digest import next_run_for_hhmm

    want = next_run_for_hhmm(MuzeConfig().digest.morning_at)
    assert abs(t["next_run_at"] - want) <= 60  # tomorrow 06:00, not +86400 drift
