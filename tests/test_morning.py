"""Morning briefing: target resolution, report body, fan-out delivery and the
builtin scheduler task (digest.morning_*)."""

from datetime import datetime, timedelta

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.digest import (
    DIGEST_PROMPT,
    morning_report,
    resolve_morning_targets,
    yesterday_date,
)
from muze.provider.milky.models import IncomingMessage
from muze.scheduler import Scheduler
from muze.store.db import open_db
from muze.store.repo import Repository
from tests.test_permissions import Recorder, env  # noqa: F401  (env fixture)


class FakeLLM:
    configured = True

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        return {"role": "assistant", "content": "需要处理: 无；会话要点: 测试内容。"}


def _mk_msg(scene, peer, seq, ts, text):
    return IncomingMessage.from_raw(
        {
            "message_scene": scene,
            "peer_id": peer,
            "message_seq": seq,
            "sender_id": 555,
            "time": ts,
            "segments": [{"type": "text", "data": {"text": text}}],
        }
    )


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "m.db"))
    yield Repository(conn)
    await conn.close()


# ---------------------------------------------------------------- targets
def test_morning_targets_default_controllers_and_group(env):  # noqa: F811
    _svc, _repo, _ad, cfg = env
    assert resolve_morning_targets(cfg) == [("friend", 10001), ("group", 20002)]


def test_morning_targets_explicit_parse_dedupe_skip_invalid():
    cfg = MuzeConfig.model_validate(
        {
            "control": {"controller": [1, 2], "control_group": 9},
            "digest": {"morning_targets": ["group/55", "77", "friend/55", "friend/55", "x/y", "-3"]},
        }
    )
    assert resolve_morning_targets(cfg) == [
        ("group", 55), ("friend", 77), ("friend", 55),
    ]


def test_morning_targets_empty_everything():
    assert resolve_morning_targets(MuzeConfig()) == []


# ---------------------------------------------------------------- report
async def test_morning_report_uses_stored_digest(repo):
    d = yesterday_date()
    await repo.upsert_digest(d, "已存报告", 24.0, {})
    llm = FakeLLM()
    text = await morning_report(repo, llm, MuzeConfig(), ChatLog())
    assert "已存报告" in text and d in text and llm.calls == 0


async def test_morning_report_catches_up_when_missing(repo):
    t0 = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    y = int((t0 - timedelta(days=1)).timestamp())
    await repo.upsert_message(_mk_msg("friend", 1, 1, y + 100, "昨天的消息"), "昨天的消息", "event")
    llm = FakeLLM()
    text = await morning_report(repo, llm, MuzeConfig(), ChatLog())
    assert llm.calls == 2 and (await repo.get_digest(yesterday_date())) is not None


async def test_morning_report_missing_no_llm(repo):
    text = await morning_report(repo, None, MuzeConfig(), ChatLog())
    assert "无做梦报告" in text


# ---------------------------------------------------------------- prompt
def test_digest_prompt_is_priority_biased():
    assert "轻重缓急" in DIGEST_PROMPT and "排序" in DIGEST_PROMPT
    assert "一句话" not in DIGEST_PROMPT.replace("不刻意压缩成一句话", "")


# ---------------------------------------------------------------- fan-out
async def test_morning_broadcast_hits_all_targets(env):  # noqa: F811
    service, _repo, adapter, _cfg = env
    sent, failed = await service.morning_broadcast("📰 晨报内容")
    assert (sent, failed) == (2, 0)
    assert [("friend", 10001), ("group", 20002)] == [(s, p) for s, p, _ in adapter.sent]


# ---------------------------------------------------------------- scheduler
async def test_ensure_and_execute_morning_task(env):  # noqa: F811
    service, repo, adapter, _cfg = env
    cfg = _cfg
    sched = Scheduler(repo, service, ChatLog())
    assert await sched.ensure_morning_digest() is not None
    assert await sched.ensure_morning_digest() is None  # idempotent

    row = next(
        r for r in await repo.list_tasks() if r.get("note") == Scheduler.MORNING_NOTE
    )
    assert row["kind"] == "interval" and row["interval_sec"] == 86400
    assert row["prompt"] == "/dream morning"

    # fire it: no digest + no agent -> skip message is still broadcast
    await sched.execute(row)
    targets = [(s, p) for s, p, _segs in adapter.sent]
    assert ("friend", 10001) in targets and ("group", 20002) in targets
    # interval task rescheduled, not finished
    after = next(r for r in await repo.list_tasks() if r["id"] == row["id"])
    assert after["status"] == "active"
    # re-anchored to the configured wall-clock slot (06:00), NOT fire+24h —
    # the drift that kept the live box's digests landing at boot-time noon
    from muze.digest import next_run_for_hhmm

    assert after["next_run_at"] == next_run_for_hhmm(cfg.digest.morning_at)


async def test_digest_task_reanchors_to_configured_slot(env):  # noqa: F811
    service, repo, _adapter, cfg = env
    sched = Scheduler(repo, service, ChatLog())
    assert await sched.ensure_daily_digest() is not None
    row = next(
        r for r in await repo.list_tasks() if r.get("note") == Scheduler.DIGEST_NOTE
    )
    await sched.execute(row)
    after = next(r for r in await repo.list_tasks() if r["id"] == row["id"])
    from muze.digest import next_run_for_hhmm

    assert after["next_run_at"] == next_run_for_hhmm(cfg.digest.at)  # next 01:00


async def test_ensure_morning_disabled(env):  # noqa: F811
    service, repo, _adapter, cfg = env
    cfg.digest.morning_enabled = False
    sched = Scheduler(repo, service, ChatLog())
    assert await sched.ensure_morning_digest() is None


# ----------------------------------------------------------- exit watchdog
async def test_exit_watchdog_preserves_rc(monkeypatch):
    """Live-box postmortem: after /restart the old serve WEDGED in
    interpreter teardown and stacked zombie generations. The watchdog must
    os._exit with the intended rc when shutdown overruns."""
    from muze import main as m

    fired = []
    monkeypatch.setattr(m.os, "_exit", lambda rc: fired.append(rc))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    m._force_exit_after(0.01, 100)
    assert fired == [100]  # rc preserved (restart contract: 100 -> relaunch)

    fired.clear()
    m._arm_exit_watchdog(3, seconds=0.01)
    import asyncio as _a

    await _a.sleep(0.2)  # let the daemon thread run
    assert fired == [3]
