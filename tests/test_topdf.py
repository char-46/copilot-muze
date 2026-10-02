"""/topdf (quoted message -> PDF file), /sess getpdf, /embed unload."""

from types import SimpleNamespace

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.store.db import open_db
from muze.store.repo import Repository

from test_reply_send import Recorder


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "t.db"))
    repo = Repository(conn)
    adapter = Recorder()
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    service = ControlService(cfg, adapter, repo, ChatLog(), Runtime())
    yield service, adapter, repo, tmp_path
    await conn.close()


@pytest.fixture
def fake_pdf(monkeypatch):
    """Patch muze.render.render_pdf (commands import it at call time);
    records every rendered body."""
    from muze import render

    calls: list[str] = []

    def _pdf(body, font_dir, **kw):
        calls.append(body)
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(render, "render_pdf", _pdf)
    return calls


def qq_ctx(peer=1, reply_seq=0):
    return ControlContext(
        source="qq", chat_key=f"qq:friend:{peer}", scene="friend",
        peer_id=peer, reply_seq=reply_seq,
    )


async def seed_message(repo, seq=10, text="一段长文本内容", segments=None):
    from muze.provider.milky.models import IncomingMessage

    msg = IncomingMessage.from_raw(
        {
            "message_scene": "friend",
            "peer_id": 1,
            "message_seq": seq,
            "sender_id": 2,
            "time": 1759000000,
            "segments": segments or [{"type": "text", "data": {"text": text}}],
        }
    )
    await repo.upsert_message(msg, text, "event")


def sent_files(adapter):
    return [(scene, peer, name) for scene, peer, name in getattr(adapter, "files", [])]


async def test_topdf_quoted_text_message(env, fake_pdf):
    service, adapter, repo, _ = env
    await seed_message(repo, seq=10, text="# 标题\n\n长**文本**内容")
    out = await service.execute("/topdf", qq_ctx(peer=1, reply_seq=10))
    assert not out.is_error, out.text
    assert fake_pdf and "长**文本**内容" in fake_pdf[0]  # markdown kept for render
    files = sent_files(adapter)
    assert files and files[0][2].endswith(".pdf")


async def test_topdf_without_quote_errors(env):
    service, *_ = env
    out = await service.execute("/topdf", qq_ctx(peer=1, reply_seq=0))
    assert out.is_error and "引用" in out.text


async def test_topdf_forward_record_expands(env, fake_pdf):
    service, adapter, repo, _ = env
    await seed_message(
        repo, seq=11, text="[转发] 记录",
        segments=[{"type": "forward", "data": {"forward_id": "F1", "title": "群聊精华"}}],
    )

    class FA(Recorder):
        async def get_forwarded_messages(self, forward_id):
            from muze.provider.milky.models import ForwardedMessage, TextSeg

            return [
                ForwardedMessage(sender_id=7, sender_name="张三", time=1,
                                 segments=[TextSeg(text="第一条")]),
                ForwardedMessage(sender_id=8, sender_name="李四", time=2,
                                 segments=[TextSeg(text="第二条")]),
            ]

    service.adapter = FA()
    out = await service.execute("/topdf", qq_ctx(peer=1, reply_seq=11))
    assert not out.is_error, out.text
    assert fake_pdf and "张三" in fake_pdf[0] and "第一条" in fake_pdf[0]
    assert sent_files(service.adapter)  # the swapped-in FA recorder


async def test_embed_unload_command(env):
    service, *_ = env
    out = await service.execute("/embed unload", qq_ctx(peer=1))
    assert out.is_error and "未启用" in out.text

    unloaded = []

    class FakeEmbedder:
        async def force_unload(self):
            unloaded.append(True)
            return True

    service.runtime.rag = SimpleNamespace(embedder=FakeEmbedder())
    out = await service.execute("/embed unload", qq_ctx(peer=1))
    assert not out.is_error and unloaded and "已卸载" in out.text


async def test_sess_getpdf(env, fake_pdf):
    service, adapter, *_ = env

    class FakeAgent:
        sessions = SimpleNamespace(latest=lambda n: [])

        @staticmethod
        async def get_session(sid):
            return SimpleNamespace(
                id="ab12", title="测试", prompt="问题？",
                messages=[
                    {"role": "user", "content": "问题？"},
                    {"role": "assistant", "content": "最终**答案**"},
                ],
            )

    service.agent = FakeAgent()
    out = await service.execute("/sess getpdf ab12", qq_ctx(peer=1))
    assert not out.is_error, out.text
    assert fake_pdf and "最终**答案**" in fake_pdf[0]
    files = sent_files(adapter)
    assert files and "ab12" in files[0][2]
