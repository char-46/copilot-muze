"""Unified send_text policy: full text (never truncated), chains for long
replies, 图文 for markdown, sequential fallback when chains unsupported."""

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.base import Caps, NotSupported, ProtocolAdapter, ProtocolError
from muze.provider.milky.models import SendMessageResult
from muze.store.db import open_db
from muze.store.repo import Repository


class Recorder(ProtocolAdapter):
    def __init__(self, forward_ok=True):
        self.caps = Caps(impl_name="fake")
        self.caps.send_forward = forward_ok
        self.sent: list[tuple[str, int, list]] = []
        self.forwarded: list[tuple[str, int, list]] = []
        self.files: list[tuple[str, int, str]] = []

    async def send_file(self, scene, peer_id, path, name=""):
        self.files.append((scene, peer_id, name))
        return "fid-fake"

    async def start(self):
        raise NotImplementedError

    async def close(self):
        pass

    def events(self):
        raise NotImplementedError

    async def get_login_info(self):
        raise NotImplementedError

    async def get_friend_list(self):
        return []

    async def get_group_list(self):
        return []

    async def get_group_member_name(self, group_id, user_id):
        return ""

    async def send_private_message(self, user_id, segments):
        self.sent.append(("friend", user_id, segments))
        return SendMessageResult(message_seq=len(self.sent), time=1759000000)

    async def send_group_message(self, group_id, segments):
        self.sent.append(("group", group_id, segments))
        return SendMessageResult(message_seq=len(self.sent), time=1759000000)

    async def send_forward_message(self, scene, peer_id, nodes, **kw):
        if not self.caps.send_forward:
            raise NotSupported("forward off")
        self.forwarded.append((scene, peer_id, nodes, kw))
        return SendMessageResult(message_seq=990, time=1759000000)

    async def get_history_messages(self, scene, peer_id, *, start_message_seq=None, limit=30):
        raise NotImplementedError

    async def get_message(self, scene, peer_id, message_seq):
        return None

    async def get_resource_url(self, resource_id):
        return "http://x"

    async def get_private_file_url(self, user_id, file_id, file_hash):
        return "http://x"

    async def get_group_file_url(self, group_id, file_id):
        return "http://x"


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "s.db"))
    repo = Repository(conn)
    adapter = Recorder()
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    service = ControlService(cfg, adapter, repo, ChatLog(), Runtime())
    yield service, adapter, cfg, tmp_path
    await conn.close()


def fake_png(monkeypatch, service, path):
    async def _rp(_text):
        from pathlib import Path

        p = Path(str(path))
        p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return p

    monkeypatch.setattr(service, "_render_png", _rp)


def texts(segments):
    return [
        s["data"]["text"] for s in segments
        if s.get("type") == "text" and s.get("data", {}).get("text")
    ]


def chain_text(adapter):
    return "\n\n".join(
        t for _, _, nodes, _ in adapter.forwarded for n in nodes for t in texts(n)
    )


async def test_short_plain_sends_single_stripped(env):
    service, adapter, *_ = env
    await service.send_text("friend", 1, "普通一句话")
    assert len(adapter.sent) == 1
    assert texts(adapter.sent[0][2]) == ["普通一句话"]


async def test_short_markdown_is_tuwen_one_message(env, monkeypatch, tmp_path):
    service, adapter, *_ = env
    fake_png(monkeypatch, service, tmp_path / "a.png")
    await service.send_text("friend", 1, "# 标题\n\n**加粗**正文")
    assert len(adapter.sent) == 1
    segs = adapter.sent[0][2]
    assert texts(segs) == ["标题\n\n加粗正文"]  # md stripped in the text part
    assert any(s["type"] == "image" for s in segs)  # 图文 same message


async def test_long_goes_chain_plus_image(env, monkeypatch, tmp_path):
    service, adapter, cfg, _ = env
    cfg.reply.forward_chars = 2000
    fake_png(monkeypatch, service, tmp_path / "b.png")
    long_md = "# 长文档\n\n" + "\n\n".join(f"## 节{i}\n\n内容**加粗**{i}" for i in range(300))
    await service.send_text("friend", 1, long_md)
    # chain carried the COMPLETE plain text
    assert len(adapter.forwarded) == 1
    scene, peer, nodes, kw = adapter.forwarded[0]
    text = chain_text(adapter)
    assert "节299" in text and "**" not in text  # full + stripped
    assert kw.get("title")
    # image of the complete markdown followed
    assert len(adapter.sent) == 1
    assert adapter.sent[0][2][0]["type"] == "image"


async def test_chain_unavailable_falls_back_sequential(env):
    service, adapter, cfg, _ = env
    adapter.caps.send_forward = False
    long = "\n\n".join(f"第{i}段 " + "字" * 200 for i in range(60))
    await service.send_text("group", 5, long)
    assert not adapter.forwarded
    joined = "\n".join(t for _, _, segs in adapter.sent for t in texts(segs))
    assert "第59段" in joined  # nothing lost


async def test_forward_failure_degrades_and_still_delivers(env, monkeypatch):
    service, adapter, *_ = env

    async def boom(*a, **kw):
        raise NotSupported("protocol side refused")

    monkeypatch.setattr(service.adapter, "send_forward_message", boom)
    cfg = service.cfg.reply
    long = "行\n" * (cfg.forward_chars + 10)
    await service.send_text("friend", 2, long)
    assert adapter.sent  # sequential fallback ran
    joined = "".join(t for _, _, segs in adapter.sent for t in texts(segs))
    assert len(joined.replace("\n", "")) > cfg.forward_chars  # complete


async def test_image_disabled_no_image(env):
    service, adapter, cfg, _ = env
    cfg.reply.image_markdown = False
    await service.send_text("friend", 1, "# 标题\n正文")
    assert len(adapter.sent) == 1
    assert adapter.sent[0][2][0]["type"] == "text"


async def test_render_failure_never_blocks_text(env, monkeypatch):
    service, adapter, *_ = env

    async def none(_t):
        return None

    monkeypatch.setattr(service, "_render_png", none)
    await service.send_text("friend", 1, "# 标题\n正文" + "长" * 6000)
    assert adapter.forwarded  # chain still sent
    assert adapter.sent == []  # no image, no crash


async def test_tuwen_failure_keeps_text(env, monkeypatch, tmp_path):
    """图文合发被协议端拒绝时，文字必须照发（不能被图片拖死）。"""
    service, adapter, *_ = env
    fake_png(monkeypatch, service, tmp_path / "d.png")
    calls = {"n": 0}

    orig = service._send_segments

    async def flaky(scene, peer, segs):
        if any(s["type"] == "image" for s in segs) and calls["n"] == 0:
            calls["n"] += 1
            raise ProtocolError("send_private_message: retcode=-400 image shape")
        return await orig(scene, peer, segs)

    monkeypatch.setattr(service, "_send_segments", flaky)
    await service.send_text("friend", 1, "# 标题\n\n**加粗**正文")
    texts_sent = [t for _, _, segs in adapter.sent for t in texts(segs)]
    assert "标题\n\n加粗正文" in texts_sent  # text survived
    assert any(s["type"] == "image" for _, _, segs in adapter.sent for s in segs)


async def test_deliver_routes_long_text_through_chain(env, monkeypatch, tmp_path):
    service, adapter, *_ = env
    fake_png(monkeypatch, service, tmp_path / "c.png")
    ctx = ControlContext(source="qq", chat_key="qq:friend:7", scene="friend", peer_id=7)
    long = "# 报告\n\n" + "内容段落。\n\n" * 1500
    from muze.core.service import CommandOutcome

    await service.deliver(ctx, CommandOutcome(long))
    assert adapter.forwarded
    assert "内容段落。" in chain_text(adapter)


async def test_morning_broadcast_full_text(env):
    service, adapter, cfg, _ = env
    cfg.control.controller = [10001]
    cfg.digest.morning_targets = ["friend/10001"]
    long = "晨报内容。\n" * 1200
    sent, failed = await service.morning_broadcast(long)
    assert (sent, failed) == (1, 0)
    if adapter.forwarded:
        assert chain_text(adapter).count("晨报内容。") == 1200  # untruncated
    else:
        joined = "".join(t for _, _, segs in adapter.sent for t in texts(segs))
        assert joined.count("晨报内容。") == 1200

async def test_big_body_falls_back_to_human_chunks(env):
    """This LLBot build rejects the single-shot body ("build scratch failed
    pb=<empty>" on a ~1800-char digest) -- the content must arrive in pieces
    instead of being silently dropped (live incident 2026-10-24 01:01)."""
    service, adapter, cfg, _ = env
    cfg.reply.forward_chars = 0        # force the single-shot path
    cfg.reply.image_markdown = False
    adapter.caps.send_forward = False  # no merged forward available either
    service.TEXT_CHUNK_GAP = (0.0, 0.0)
    service.SAFE_SINGLE_CHARS = 100000  # pretend the single shot is allowed

    body = "\n".join(f"第{i}行 " + "内容" * 40 for i in range(20))
    assert len(body) > service.TEXT_CHUNK_CHARS

    calls = {"n": 0}
    orig = adapter.send_private_message

    async def flaky(user_id, segments):
        calls["n"] += 1
        if len(segments[0]["data"]["text"]) > service.TEXT_CHUNK_CHARS:
            raise ProtocolError("send_private_message: retcode=500 build scratch failed")
        return await orig(user_id, segments)

    adapter.send_private_message = flaky
    ok = await service.send_text("friend", 1, body)
    assert ok is True
    assert calls["n"] > 1              # first shot failed, then the chunks
    joined = "".join(s[2][0]["data"]["text"] for s in adapter.sent)
    assert "第0行" in joined and "第19行" in joined   # nothing lost


async def test_long_body_goes_out_as_one_merged_forward(env, monkeypatch, tmp_path):
    """User requirement 2026-10-24: content that does not fit one safe message
    must arrive as ONE merged-forward (合并转发) plus the rendered image, not
    as a burst of separate messages."""
    service, adapter, cfg, _ = env
    cfg.reply.forward_chars = 5000     # below the config threshold on purpose
    fake_png(monkeypatch, service, tmp_path / "m.png")
    body = "# 报告\n\n" + "\n".join(f"第{i}条 内容" for i in range(120))  # ~1k chars
    assert len(body) > service.SAFE_SINGLE_CHARS

    ok = await service.send_text("group", 5, body)
    assert ok is True
    assert len(adapter.forwarded) == 1              # ONE merged-forward message
    assert adapter.forwarded[0][0] == "group"
    chain = chain_text(adapter)
    assert "第0条" in chain and "第119条" in chain   # complete
    # the rendered image follows as its own message; no plain-text burst
    assert len(adapter.sent) == 1
    assert adapter.sent[0][2][0]["type"] == "image"

async def test_merged_forward_is_split_into_chat_sized_nodes(env):
    """User requirement 2026-10-24 (2nd round): inside the merged forward the
    content must be SEVERAL chat-sized items, not one giant node."""
    service, adapter, cfg, _ = env
    cfg.reply.image_markdown = False
    body = "\n\n".join(f"## 第{i}节\n" + "内容" * 150 for i in range(6))  # ~1.9k chars
    assert len(body) > service.SAFE_SINGLE_CHARS

    ok = await service.send_text("group", 5, body)
    assert ok is True
    assert len(adapter.forwarded) == 1          # still ONE merged-forward message
    nodes = adapter.forwarded[0][2]
    assert len(nodes) >= 3                      # ...but several items inside
    assert all(len(n[0]["data"]["text"]) <= cfg.reply.node_chars for n in nodes)
    joined = "".join(n[0]["data"]["text"] for n in nodes)
    assert "第0节" in joined and "第5节" in joined
