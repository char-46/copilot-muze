"""Archive query: date-range repo methods + /day /between /top /range."""

import time

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.milky.models import IncomingMessage
from muze.store.db import open_db
from muze.store.repo import Repository

from test_permissions import Recorder


def msg(scene, peer, seq, t, sender=7, text="hi"):
    return IncomingMessage.from_raw(
        {
            "message_scene": scene,
            "peer_id": peer,
            "message_seq": seq,
            "sender_id": sender,
            "time": t,
            "segments": [{"type": "text", "data": {"text": text}}],
        }
    )


DAY = 86400
NOW = int(time.time())
TODAY = NOW - (NOW % DAY)  # local-day boundary approx; tests use explicit ts


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "a.db"))
    repo = Repository(conn)
    adapter = Recorder()
    cfg = MuzeConfig()
    cfg.control.controller = [10001]
    service = ControlService(cfg, adapter, repo, ChatLog(), Runtime())
    yield service, repo
    await conn.close()


def local_ctx():
    return ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)


async def test_messages_between_scoping_and_order(env):
    _, repo = env
    base = 1758000000
    for i in range(10):
        await repo.upsert_message(msg("group", 1, i + 1, base + i * 600), "x", "event")
    await repo.upsert_message(msg("friend", 2, 1, base + 60), "x", "event")

    rows = await repo.messages_between("group", 1, base, base + 3600)
    assert [r["message_seq"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert all(base <= r["time"] < base + 3600 for r in rows)
    assert all(r["scene"] == "group" and r["peer_id"] == 1 for r in rows)
    # half-open: end is exclusive
    assert await repo.messages_between("group", 1, base + 3600, base + 3600) == []


async def test_day_counts_and_top(env):
    _, repo = env
    base = 1758000000
    for i in range(5):
        await repo.upsert_message(msg("group", 1, i + 1, base + i), "x", "event")
    for i in range(2):
        await repo.upsert_message(msg("group", 9, i + 1, base + i), "x", "event")
    counts = await repo.day_counts(base, base + 100)
    assert counts[0] == {"scene": "group", "peer_id": 1, "n": 5, "name": ""}
    top = await repo.top_conversations(0)
    assert top[0]["peer_id"] == 1 and top[0]["n"] == 5


def test_parse_day():
    from muze.core.service import ControlService

    s0, e0 = ControlService.parse_day("2026-09-30")
    assert e0 - s0 == DAY
    st, et = ControlService.parse_day("today")
    assert st == et - DAY
    with pytest.raises(ValueError):
        ControlService.parse_day("not-a-date")


async def test_cmd_day_scoped_and_overview(env):
    service, repo = env
    from datetime import datetime

    base = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) + 300
    await repo.upsert_message(msg("group", 1, 1, base, text="早上好"), "早上好", "event")
    await repo.upsert_message(msg("group", 1, 2, base + 60, text="开会"), "开会", "event")
    await repo.upsert_message(msg("friend", 8, 1, base, text="私聊"), "私聊", "event")

    out = await service.execute("/day today", local_ctx())
    assert "共 3 条" in out.text and "2 个会话" in out.text

    scoped = await service.execute("/day today group/1", local_ctx())
    assert "group/1" in scoped.text and "早上好" in scoped.text and "开会" in scoped.text


async def test_cmd_top_and_range(env):
    service, repo = env
    base = int(time.time()) - 3600
    for i in range(4):
        await repo.upsert_message(msg("group", 1, i + 1, base + i), "x", "event")
    out = await service.execute("/top 7", local_ctx())
    assert "group/1" in out.text and "4条" in out.text

    rng = await service.execute("/range group/1", local_ctx())
    assert "4 条" in rng.text and "最早" in rng.text and "最新" in rng.text
