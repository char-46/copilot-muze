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
    assert llm.calls == 1 and (await repo.get_digest(yesterday_date())) is not None


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


async def test_ensure_morning_disabled(env):  # noqa: F811
    service, repo, _adapter, cfg = env
    cfg.digest.morning_enabled = False
    sched = Scheduler(repo, service, ChatLog())
    assert await sched.ensure_morning_digest() is None
