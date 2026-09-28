import pytest

from muze.provider.milky.models import IncomingMessage
from muze.store.db import open_db
from muze.store.repo import Repository


def make_msg(scene="friend", peer=100, seq=1, sender=7, text="hello"):
    return IncomingMessage.from_raw(
        {
            "message_scene": scene,
            "peer_id": peer,
            "message_seq": seq,
            "sender_id": sender,
            "time": 1759000000 + seq,
            "segments": [{"type": "text", "data": {"text": text}}],
        }
    )


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "t.db"))
    yield Repository(conn)
    await conn.close()


async def test_upsert_dedupes(repo):
    assert await repo.upsert_message(make_msg(seq=1), "hello", "event") is True
    assert await repo.upsert_message(make_msg(seq=1), "hello", "event") is False
    assert (await repo.stats())["messages"] == 1


async def test_watermark_tracks_max_seq(repo):
    for seq in (1, 5, 3):
        await repo.upsert_message(make_msg(seq=seq), "x", "event")
    assert await repo.get_watermark("friend", 100) == 5


async def test_search_escapes_like(repo):
    await repo.upsert_message(make_msg(seq=1, text="100% 保证"), "100% 保证", "event")
    await repo.upsert_message(make_msg(seq=2, text="别的"), "别的", "event")
    hits = await repo.search_messages("100%")
    assert len(hits) == 1 and "保证" in hits[0]["text_content"]
    hits2 = await repo.search_messages("不存在的词")
    assert hits2 == []


async def test_pending_actions_lifecycle(repo):
    aid = await repo.create_action("send", "friend", 5, {"preview": "p"}, "friend", 1, "command")
    rows = await repo.list_pending_actions("friend", 1)
    assert len(rows) == 1 and rows[0]["id"] == aid
    assert await repo.set_action_status(aid, "executed") is True
    assert await repo.list_pending_actions("friend", 1) == []
    assert await repo.set_action_status(aid, "cancelled") is False  # already executed


async def test_media_unique_constraint(repo):
    row = {
        "scene": "friend", "peer_id": 1, "message_seq": 2, "seg_index": 0,
        "kind": "image", "resource_id": "r", "file_id": "", "file_name": "",
        "file_hash": "", "width": None, "height": None, "duration": None,
        "summary": "", "status": "pending",
    }
    assert await repo.add_media(row) is not None
    assert await repo.add_media(row) is None
    rows = await repo.media_pending()
    assert len(rows) == 1
