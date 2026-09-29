"""Dream (daily digest + catch-up) and long-term memory tests."""

import time
from datetime import datetime, timedelta

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.digest import day_range, dream_night, extract_memories, missing_dates, run_digest
from muze.provider.milky.models import IncomingMessage
from muze.store.db import open_db
from muze.store.repo import Repository


class FakeLLM:
    configured = True

    def __init__(self):
        self.calls: list[str] = []

    async def chat(self, messages, tools=None):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        if "记忆提炼" in prompt:
            content = (
                '[{"action":"add","subject":"owner","kind":"identity","content":"主人是明日方舟玩家"},'
                '{"action":"add","subject":"1000000001","kind":"relation","content":"是主人的大学同学"}]'
            )
        else:
            content = f"当日概览：测试报告 ({prompt[:20]}...)"
        return {"role": "assistant", "content": content}


def mk_msg(scene, peer, seq, ts, text):
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


def local_midnight(offset_days=0) -> int:
    d = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=offset_days)
    return int(d.timestamp())


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "dream.db"))
    yield Repository(conn)
    await conn.close()


def test_day_range_and_missing():
    s, e = day_range("2026-09-28")
    assert e - s == 86400
    assert missing_dates("2026-09-26", "2026-09-28") == ["2026-09-27", "2026-09-28"]
    assert missing_dates(None, "2026-09-28") == ["2026-09-28"]
    assert missing_dates("2026-09-28", "2026-09-28") == []


async def test_run_digest_uses_only_that_day(repo):
    # messages: yesterday and today (around local midnight)
    y0 = local_midnight(-1)
    t0 = local_midnight(0)
    await repo.upsert_message(mk_msg("friend", 1, 1, y0 + 100, "昨天的消息"), "昨天的消息", "event")
    await repo.upsert_message(mk_msg("friend", 1, 2, t0 + 100, "今天的消息"), "今天的消息", "event")

    llm = FakeLLM()
    date = datetime.now().astimezone().strftime("%Y-%m-%d")
    ydate = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")

    r_y = await run_digest(repo, llm, MuzeConfig(), ChatLog(), date=ydate)
    assert "昨天的消息" in llm.calls[-1] and "今天的消息" not in llm.calls[-1]
    assert (await repo.get_digest(ydate))["report"] == r_y

    await run_digest(repo, llm, MuzeConfig(), ChatLog(), date=date)
    assert "今天的消息" in llm.calls[-1]


async def test_dream_night_catches_up_multiple_days(repo):
    llm = FakeLLM()
    cfg = MuzeConfig()
    from muze.digest import yesterday_date

    # seed a digest 4 days ago -> catch-up must fill [old+1 .. yesterday]
    old = (datetime.now().astimezone() - timedelta(days=4)).strftime("%Y-%m-%d")
    await repo.upsert_digest(old, "旧报告", 24.0, {})
    text = await dream_night(repo, llm, cfg, ChatLog())
    todo = missing_dates(old, yesterday_date())
    assert len(todo) == 3
    assert f"补做 {len(todo)} 天" in text
    dates = {r["date"] for r in await repo.list_digests()}
    assert set(todo) <= dates and old in dates

    # memories distilled from the freshest report
    mems = await repo.list_memories()
    assert any("大学同学" in m["content"] for m in mems)
    assert any("明日方舟" in m["content"] for m in mems)

    # nothing missing now -> idempotent
    text2 = await dream_night(repo, llm, cfg, ChatLog())
    assert "已存在" in text2


async def test_dream_night_first_run(repo):
    llm = FakeLLM()
    text = await dream_night(repo, llm, MuzeConfig(), ChatLog())
    assert "补做 1 天" in text
    rows = await repo.list_digests()
    assert len(rows) == 1


async def test_memory_crud_and_retire(repo):
    mid = await repo.add_memory("identity", "owner", "喜欢喝乌龙茶", source="dream:test")
    rows = await repo.list_memories()
    assert rows[0]["id"] == mid
    assert await repo.retire_memory(mid) is True
    assert await repo.list_memories() == []
    assert await repo.retire_memory(mid) is False


async def test_extract_memories_bad_json(repo):
    class BadLLM(FakeLLM):
        async def chat(self, messages, tools=None):
            return {"content": "not json at all"}

    n = await extract_memories(
        repo, BadLLM(), MuzeConfig(), date="2026-09-28", report="r", chatlog=ChatLog()
    )
    assert n == 0 and await repo.list_memories() == []
