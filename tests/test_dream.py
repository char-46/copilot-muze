"""Dream (daily digest + catch-up + weekly/monthly rollups) and memory tests."""

import time
from datetime import datetime, timedelta

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.digest import (
    day_range,
    dream_night,
    extract_memories,
    missing_dates,
    missing_weeks,
    month_key,
    run_digest,
    run_monthly,
    run_weekly,
    week_days,
    week_key,
)
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
        elif "层级做梦" in prompt:
            content = "压缩报告：本周主线是测试。"
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


async def test_digest_covers_every_conversation(repo):
    """Default digest covers ALL active conversations, not just the busiest."""
    llm = FakeLLM()
    date = datetime.now().astimezone().strftime("%Y-%m-%d")
    t0 = local_midnight(0) + 200
    for i in range(1, 9):  # 8 conversations with different volumes
        n = 30 - i  # friend/1 busiest ... friend/8 quietest
        for s in range(n):
            await repo.upsert_message(
                mk_msg("friend", i, s + 1, t0 + s, f"会话{i}第{s}条"),
                f"会话{i}第{s}条", "event",
            )
    await run_digest(repo, llm, MuzeConfig(), ChatLog(), date=date)
    prompt = llm.calls[-1]
    for i in range(1, 9):
        assert f"friend/{i}," in prompt  # every conversation has a section


async def test_digest_budget_samples_but_keeps_first_and_last(repo):
    """Over budget: sections shrink by even sampling; first+last messages
    of every conversation survive; total transcript respects the budget."""
    from muze.digest import _pick_fitting, _spread

    lines = [f"[12:00] [7] 消息编号{i:03d}" + "内" * 40 for i in range(200)]
    picked = _pick_fitting(lines, 2000)
    assert 2 < len(picked) < 200
    assert picked[0] == lines[0] and picked[-1] == lines[-1]
    assert sum(len(x) + 1 for x in picked) <= 2000
    assert _spread([1, 2, 3], 5) == [1, 2, 3]  # no-op under length

    llm = FakeLLM()
    date = datetime.now().astimezone().strftime("%Y-%m-%d")
    t0 = local_midnight(0) + 200
    for s in range(120):
        await repo.upsert_message(
            mk_msg("friend", 1, s + 1, t0 + s, f"第{s:03d}条" + "水" * 60),
            f"第{s:03d}条", "event",
        )
    cfg = MuzeConfig.model_validate({"digest": {"transcript_chars": 6000}})
    await run_digest(repo, llm, cfg, ChatLog(), date=date)
    prompt = llm.calls[-1]
    assert "全天均匀采样" in prompt
    assert "第000条" in prompt and "第119条" in prompt  # first + last survive
    transcript = prompt.split("# 档案", 1)[1]
    assert len(transcript) < 9000  # budget respected (headers + slack)


async def test_digest_uses_prior_day_context(repo):
    """The digest prompt carries prior-day reports for follow-ups."""
    llm = FakeLLM()
    now = datetime.now().astimezone()
    date = now.strftime("%Y-%m-%d")
    ydate = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    await repo.upsert_digest(ydate, "需要处理: 等张三确认会议时间", 24.0, {})
    await repo.upsert_message(
        mk_msg("friend", 1, 1, local_midnight(0) + 100, "张三说周三三点"),
        "张三说周三三点", "event",
    )

    await run_digest(repo, llm, MuzeConfig(), ChatLog(), date=date)
    prompt = llm.calls[-1]
    assert "往日背景" in prompt and "等张三确认会议时间" in prompt
    assert prompt.index("往日背景") < prompt.index("# 档案")

    # prior_days=0 -> isolated day again
    llm2 = FakeLLM()
    cfg = MuzeConfig.model_validate({"digest": {"prior_days": 0}})
    await run_digest(repo, llm2, cfg, ChatLog(), date=date)
    assert "等张三确认会议时间" not in llm2.calls[-1]


async def test_digest_prior_walks_past_missing_days(repo):
    """Yesterday missing -> falls back to the day before; long texts truncate."""
    llm = FakeLLM()
    now = datetime.now().astimezone()
    date = now.strftime("%Y-%m-%d")
    d2 = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    await repo.upsert_digest(d2, "旧" * 3000, 24.0, {})
    await repo.upsert_message(
        mk_msg("friend", 1, 1, local_midnight(0) + 100, "今天的消息"),
        "今天的消息", "event",
    )
    await run_digest(repo, llm, MuzeConfig(), ChatLog(), date=date)
    prompt = llm.calls[-1]
    assert f"[{d2}]" in prompt and "…(截断)" in prompt
    assert prompt.count("旧") <= 1601


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
    # one day digest; a month/week rollup may also appear around period bounds
    assert len([r for r in rows if r["period"] == "day"]) == 1


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


# ------------------------------------------------------------ weekly/monthly
def test_week_and_month_keys():
    assert week_key("2026-09-28") == "2026-W40"  # Monday of ISO week 40
    days = week_days("2026-W40")
    assert days[0] == "2026-09-28" and len(days) == 7 and days[-1] == "2026-10-04"
    assert month_key("2026-09-28") == "2026-09"
    assert week_key(time.time()) == week_key(datetime.now().strftime("%Y-%m-%d"))  # ts overload


async def test_weekly_rollup_and_night_chain(repo):
    llm = FakeLLM()
    cfg = MuzeConfig()
    # three day-reports spread over last week (guaranteed completed week)
    monday = datetime.now().astimezone() - timedelta(days=7)
    monday = monday - timedelta(days=monday.weekday())  # last week's monday
    for i in range(3):
        d = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
        await repo.upsert_digest(d, f"{d} 报告内容", 24.0, {}, period="day")

    wk = week_key(monday.strftime("%Y-%m-%d"))
    report = await run_weekly(repo, llm, cfg, ChatLog(), week=wk)
    assert "压缩报告" in report
    row = await repo.get_digest(wk)
    assert row["period"] == "week" and row["report"] == report
    # the week rollup also fed the memory pass
    assert any("大学同学" in m["content"] for m in await repo.list_memories())

    # missing_weeks no longer lists it, but lists nothing else either
    assert wk not in await missing_weeks(repo)


async def test_dream_night_rolls_up_completed_week(repo):
    llm = FakeLLM()
    cfg = MuzeConfig()
    # no day digests -> nothing to do (no week rollup either)
    text = await dream_night(repo, llm, cfg, ChatLog())
    assert "补做 1 天" in text  # yesterday itself

    # give the completed last week day-reports only; next night should roll it up
    monday = datetime.now().astimezone() - timedelta(days=7)
    monday = monday - timedelta(days=monday.weekday())
    await repo.upsert_digest(
        monday.strftime("%Y-%m-%d"), "周一报告", 24.0, {}, period="day"
    )
    text = await dream_night(repo, llm, cfg, ChatLog())
    assert "周报" in text
    wk = week_key(monday.strftime("%Y-%m-%d"))
    assert await repo.get_digest(wk) is not None


async def test_monthly_rollup_prefers_days_for_sparse_months(repo):
    llm = FakeLLM()
    last_month = (datetime.now().astimezone().replace(day=1) - timedelta(days=1))
    key = last_month.strftime("%Y-%m")
    await repo.upsert_digest(last_month.strftime("%Y-%m-%d"), "月末报告", 24.0, {}, period="day")
    report = await run_monthly(repo, llm, MuzeConfig(), ChatLog(), month=key)
    row = await repo.get_digest(key)
    assert row["period"] == "month" and row["report"] == report
