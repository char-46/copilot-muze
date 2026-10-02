"""Legacy session compatibility: pre-alpha.82 sessions recover their final
answer for the webui (archived bot reply > 200-char preview)."""

import time

from muze.ai.sessions import DONE, RUNNING, Session, legacy_final_tail
from muze.provider.milky.models import IncomingMessage
from muze.store.db import open_db
from muze.store.repo import Repository


async def _mk_repo(tmp_path):
    conn = await open_db(str(tmp_path / "lg.db"))
    return Repository(conn), conn


def mk_session(**kw):
    base = dict(
        id="aa01",
        chat_key="qq:friend:5",
        prompt="问题",
        status=DONE,
        result_preview="完整答案的开头部分 " + "x" * 180,
        messages=[{"role": "user", "content": "问题"}, {"role": "tool", "tool_call_id": "1", "content": "{}"}],
        created=time.time() - 600,
        updated=time.time() - 540,
    )
    base.update(kw)
    return Session(**base)


async def seed_self_reply(repo, text, t, seq=10, sender=99):
    msg = IncomingMessage.from_raw(
        {
            "message_scene": "friend",
            "peer_id": 5,
            "message_seq": seq,
            "sender_id": sender,
            "time": int(t),
            "segments": [{"type": "text", "data": {"text": text}}],
        }
    )
    await repo.upsert_message(msg, text, "event")


async def test_recovers_full_text_from_archive(tmp_path):
    repo, conn = await _mk_repo(tmp_path)
    s = mk_session()
    full = s.result_preview[:20] + "……中间省略大量内容……结尾"
    await seed_self_reply(repo, full, s.updated + 5)
    tail = await legacy_final_tail(repo, s, self_id=99)
    assert tail and tail["role"] == "assistant"
    assert "中间省略大量内容" in tail["content"]
    await conn.close()


async def test_falls_back_to_preview_with_note(tmp_path):
    repo, conn = await _mk_repo(tmp_path)
    s = mk_session()
    tail = await legacy_final_tail(repo, s, self_id=99)
    assert tail and s.result_preview[:20] in tail["content"]
    assert "未存档" in tail["content"]
    await conn.close()


async def test_noop_when_final_already_present(tmp_path):
    repo, conn = await _mk_repo(tmp_path)
    s = mk_session(messages=[{"role": "user", "content": "问"},
                             {"role": "assistant", "content": "答"}])
    assert await legacy_final_tail(repo, s, 99) is None
    await conn.close()


async def test_noop_for_suspended_or_running(tmp_path):
    repo, conn = await _mk_repo(tmp_path)
    s = mk_session(status=RUNNING)
    assert await legacy_final_tail(repo, s, 99) is None
    s2 = mk_session(status="suspend")
    assert await legacy_final_tail(repo, s2, 99) is None
    await conn.close()


async def test_wrong_sender_not_matched(tmp_path):
    repo, conn = await _mk_repo(tmp_path)
    s = mk_session()
    await seed_self_reply(repo, s.result_preview[:20] + "别人说的", s.updated + 5, sender=7)
    tail = await legacy_final_tail(repo, s, self_id=99)
    assert tail and "别人说的" not in tail["content"]
    await conn.close()
