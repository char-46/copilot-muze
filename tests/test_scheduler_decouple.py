"""Task/process decoupling: scheduled-task lifetime lives in the DB behind
a claim/lease protocol so restarts never lose or permanently wedge a task
(live postmortem 2026-10-18: a /restart mid-digest killed the run and the
box relied on luck — an overdue row — to re-fire)."""

import asyncio
import time

import pytest

from muze.chatlog import ChatLog
from muze.core.service import CommandOutcome, ControlContext
from muze.scheduler import RETRY_SECONDS, Scheduler
from muze.store.db import open_db
from muze.store.repo import Repository
from tests.test_plugins import MuzeConfig


class GatedSvc:
    """Fake ControlService whose execute() can be held open / failed."""

    def __init__(self):
        self.cfg = MuzeConfig()
        self.gate = asyncio.Event()
        self.gate.set()  # open by default
        self.executed: list[str] = []
        self.entered = 0
        self.delivered: list[str] = []
        self.fail = False

    async def execute(self, prompt, ctx):
        self.entered += 1
        self.executed.append(prompt)
        await self.gate.wait()
        if self.fail:
            raise RuntimeError("boom")
        return CommandOutcome("ok: " + prompt)

    async def deliver(self, ctx, outcome):
        self.delivered.append(outcome.text)


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "s.db"))
    repo = Repository(conn)
    yield repo, conn
    await conn.close()


def _ctx():
    return ControlContext(source="cli", chat_key="t", scene="friend", peer_id=42)


async def test_claim_prevents_double_fire(env):
    """Two live schedulers share one DB: the atomic claim means the second
    cannot re-fire a task the first is currently executing."""
    repo, conn = env
    svc = GatedSvc()
    s1, s2 = Scheduler(repo, svc, ChatLog()), Scheduler(repo, svc, ChatLog())
    row = await s1.create(prompt="/digest", delay_sec=0, ctx=_ctx())

    svc.gate.clear()  # hold the run open
    assert await s1.tick() == 1
    await asyncio.sleep(0.05)
    claimed = await repo.get_task(row["id"])
    assert claimed["status"] == "running" and claimed["lease_until"] > int(time.time())
    assert await s2.tick() == 0  # claimed: not due for anyone else

    svc.gate.set()
    await s1.drain()
    after = await repo.get_task(row["id"])
    assert after["status"] == "done" and after["lease_until"] is None


async def test_shutdown_resets_inflight_to_rerun_from_scratch(env):
    """/restart mid-task: stop() cancels the run and resets the row to
    due-now; the successor process re-runs it from the beginning."""
    repo, conn = env
    svc = GatedSvc()
    s1 = Scheduler(repo, svc, ChatLog())
    row = await s1.create(prompt="/digest", delay_sec=0, ctx=_ctx())

    svc.gate.clear()
    await s1.tick()
    await asyncio.sleep(0.05)
    await s1.stop()  # graceful shutdown while the run is mid-flight

    after = await repo.get_task(row["id"])
    assert after["status"] == "active"
    assert after["next_run_at"] <= int(time.time()) + 1
    assert svc.entered == 1 and not svc.delivered  # interrupted, nothing sent

    # successor process: fresh scheduler picks the row up and completes it
    svc.gate.set()
    s2 = Scheduler(repo, svc, ChatLog())
    assert await s2.tick() == 1
    await s2.drain()
    assert svc.entered == 2 and svc.delivered == ["ok: /digest"]


async def test_startup_recovers_expired_lease(env):
    """Hard death (power loss / kill -9) leaves the row 'running'; the next
    process's start() adopts it once the lease has expired."""
    repo, conn = env
    svc = GatedSvc()
    s1 = Scheduler(repo, svc, ChatLog())
    row = await s1.create(prompt="/digest", delay_sec=0, ctx=_ctx())
    # simulate: previous process claimed it then died instantly
    await conn.execute(
        "UPDATE scheduled_tasks SET status='running', lease_until=? WHERE id=?",
        (int(time.time()) - 1, row["id"]),
    )
    await conn.commit()

    # hold the gate: recovery flips the row due-now and the loop's first
    # tick re-claims it, but the run itself stays parked in execute()
    svc.gate.clear()
    s2 = Scheduler(repo, svc, ChatLog())
    await s2.start()
    await asyncio.sleep(0.1)
    recovered = await repo.get_task(row["id"])
    assert recovered["status"] == "running"  # recovered AND re-claimed
    assert svc.entered == 1  # re-fired from scratch exactly once

    svc.gate.set()
    await s2.drain()
    assert svc.executed == ["/digest"]
    assert (await repo.get_task(row["id"]))["status"] == "done"
    await s2.stop()


async def test_live_lease_is_left_alone(env):
    """A row with an unexpired lease may still have a live owner — startup
    recovery must NOT steal it (would duplicate the run)."""
    repo, conn = env
    svc = GatedSvc()
    row = await svc_cfg_create(Scheduler(repo, svc, ChatLog()))
    await conn.execute(
        "UPDATE scheduled_tasks SET status='running', lease_until=? WHERE id=?",
        (int(time.time()) + 600, row["id"]),
    )
    await conn.commit()

    s2 = Scheduler(repo, svc, ChatLog())
    await s2.start()
    await asyncio.sleep(0.05)
    still = await repo.get_task(row["id"])
    assert still["status"] == "running"  # untouched
    assert await s2.tick() == 0  # lease live: not claimable
    await s2.stop()


async def svc_cfg_create(scheduler):
    return await scheduler.create(prompt="/digest", delay_sec=0, ctx=_ctx())


async def test_runs_execute_concurrently(env):
    """The dawn briefing no longer queues behind a multi-minute nightly
    digest: due tasks run as concurrent asyncio tasks."""
    repo, conn = env
    svc = GatedSvc()
    s = Scheduler(repo, svc, ChatLog())
    slow = await s.create(prompt="/slow-night", delay_sec=0, ctx=_ctx())
    await s.create(prompt="/fast-morning", delay_sec=0, ctx=_ctx())

    svc.gate.clear()  # first-entered run blocks
    await s.tick()
    await asyncio.sleep(0.1)
    assert svc.entered == 2  # both started, neither blocked the other
    assert len(s._inflight) == 2

    svc.gate.set()
    await s.drain()
    for tid in (slow["id"],):
        assert (await repo.get_task(tid))["status"] == "done"
    assert sorted(svc.delivered) == ["ok: /fast-morning", "ok: /slow-night"]


async def test_interval_transient_failure_retries(env):
    """One LLM hiccup used to flip an interval task to status='error'
    FOREVER (the nightly digest would silently stop). Now it retries."""
    repo, conn = env
    svc = GatedSvc()
    s = Scheduler(repo, svc, ChatLog())
    row = await s.create(prompt="/nightly", interval_sec=86400, delay_sec=0, ctx=_ctx())
    # interval branch schedules +86400: force it due like a real 01:00 slot
    await conn.execute(
        "UPDATE scheduled_tasks SET next_run_at=? WHERE id=?", (int(time.time()) - 1, row["id"])
    )
    await conn.commit()

    svc.fail = True
    await s.tick()
    await s.drain()
    failed = await repo.get_task(row["id"])
    assert failed["status"] == "active"  # NOT 'error'
    assert 0 < failed["next_run_at"] - int(time.time()) <= RETRY_SECONDS + 2

    svc.fail = False  # retry succeeds
    await conn.execute(
        "UPDATE scheduled_tasks SET next_run_at=? WHERE id=?", (int(time.time()) - 1, row["id"])
    )
    await conn.commit()
    await s.tick()
    await s.drain()
    ok = await repo.get_task(row["id"])
    assert ok["status"] == "active" and ok["last_result"].startswith("ok:")
    assert ok["next_run_at"] > int(time.time())  # rescheduled to +86400


async def test_once_failure_is_terminal(env):
    repo, conn = env
    svc = GatedSvc()
    s = Scheduler(repo, svc, ChatLog())
    row = await s.create(prompt="/oneshot", delay_sec=0, ctx=_ctx())
    svc.fail = True
    await s.tick()
    await s.drain()
    assert (await repo.get_task(row["id"]))["status"] == "error"
