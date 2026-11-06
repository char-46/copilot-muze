"""Delivery never-doubles batch (live 2026-11-07): the dream digest was
re-delivered to the control group on every parked retry (try 1..11, hourly by
the end) because each "failed" send had actually landed with only the response
lost; and the morning briefing re-broadcast to everyone on every daemon
relaunch because one target's send crashed the process, resetting the task
due-now. Fixes: delivery-evidence probe before any parked re-send, no
synchronous re-send on network-ish failures, one morning attempt per target
per day, parked retries capped."""

import json
import time

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlService, Runtime
from muze.provider.base import NotSupported, ProtocolError
from muze.provider.milky.models import SendMessageResult
from muze.scheduler import Scheduler
from muze.store.db import open_db
from muze.store.repo import Repository

SELF = 7


async def _db(tmp_path):
    conn = await open_db(str(tmp_path / "d.db"))
    return Repository(conn), conn


async def _put_msg(repo, scene, peer, seq, sender, text, raw="", ts=None):
    await repo.conn.execute(
        "INSERT INTO messages (scene, peer_id, message_seq, sender_id, time,"
        " raw_json, text_content, recalled, source, ingested_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (scene, peer, seq, sender, ts or int(time.time()), raw or "{}", text, 0,
         "event", int(time.time())),
    )
    await repo.conn.commit()


def _svc(adapter, repo):
    rt = Runtime()
    rt.self_id = SELF
    return ControlService(MuzeConfig(), adapter, repo, ChatLog(), rt)


# ------------------------------------------------------- evidence probe
async def test_evidence_seen_for_plain_and_chain_echoes(tmp_path):
    repo, conn = await _db(tmp_path)
    # plain/chunk echo: self message whose text starts with the head
    await _put_msg(repo, "group", 42, 1, SELF, "做梦完成：补了 3 天，档案更新 3 个")
    # chain echo: text is a placeholder but raw payload carries the title
    await _put_msg(repo, "friend", 9, 1, SELF, "[转发:群聊的聊天记录]",
                   raw='{"title": "📰 2026-11-06 昨日总结"}')
    # someone else's message must not count
    await _put_msg(repo, "group", 42, 2, 999, "做梦完成：补了 3 天")
    # stale evidence (older than the window) must not count
    await _put_msg(repo, "group", 43, 1, SELF, "晨报头", ts=int(time.time()) - 3 * 86400)

    assert await repo.delivery_evidence_seen("group", 42, SELF, "做梦完成：补了 3 天", int(time.time() - 86400))
    assert await repo.delivery_evidence_seen("friend", 9, SELF, "📰 2026-11-06 昨日总结", int(time.time() - 86400))
    assert not await repo.delivery_evidence_seen("group", 43, SELF, "晨报头", int(time.time() - 86400))
    assert not await repo.delivery_evidence_seen("group", 44, SELF, "做梦完成", int(time.time() - 86400))
    await conn.close()


# ------------------------------------------- send_text failure policy
class NetAdapter:
    """Single-shot send dies with a network-ish error (no retcode=)."""

    def __init__(self, err):
        self.err = err
        self.calls = 0

    async def send_private_message(self, user_id, segments):
        self.calls += 1
        raise ProtocolError(self.err)

    async def send_group_message(self, group_id, segments):
        self.calls += 1
        raise ProtocolError(self.err)


async def test_network_failure_is_not_resynchronously_resent(tmp_path):
    repo, conn = await _db(tmp_path)
    ad = NetAdapter("send_private_message: [WinError 64] 指定的网络名不再可用。")
    svc = _svc(ad, repo)
    ok = await svc.send_text("friend", 5, "短消息一次投递")
    assert ok is False            # not confirmed -- but crucially:
    assert ad.calls == 1          # NO chunked re-send of possibly-delivered text
    await conn.close()


async def test_explicit_refusal_still_gets_chunk_recovery(tmp_path):
    repo, conn = await _db(tmp_path)

    class RefusedAdapter(NetAdapter):
        def __init__(self):
            super().__init__("")
            self.sent = 0

        async def send_private_message(self, user_id, segments):
            self.calls += 1
            if self.calls == 1:
                raise ProtocolError("send_private_message: retcode=500 message=PMHQ send failed")
            self.sent += 1
            return SendMessageResult(message_seq=self.sent)

    ad = RefusedAdapter()
    svc = _svc(ad, repo)
    ok = await svc.send_text("friend", 5, "definitely not delivered body")
    assert ok is True             # refusal = not delivered -> chunk recovery ran
    assert ad.calls >= 2
    await conn.close()


async def test_chain_sequential_fallback_does_not_duplicate_pieces(tmp_path):
    repo, conn = await _db(tmp_path)

    class ChainDownAdapter:
        def __init__(self):
            self.plain = 0

        async def send_forward_message(self, scene, peer_id, nodes, **kw):
            raise NotSupported("forward send marked unavailable on this protocol side")

        async def send_private_message(self, user_id, segments):
            self.plain += 1
            if self.plain == 2:  # the middle piece dies with a network error
                raise ProtocolError("send_private_message: ")
            return SendMessageResult(message_seq=self.plain)

    ad = ChainDownAdapter()
    svc = _svc(ad, repo)
    body = "\n\n".join(f"段落{i}" + "字" * 250 for i in range(3))
    ok = await svc.send_text("friend", 5, body)
    assert ok is False            # piece 2 unconfirmed
    assert ad.plain == 2          # each piece exactly once -- no full re-send
    await conn.close()


# ------------------------------------------------- morning one-per-day
async def test_morning_broadcast_attempts_each_target_once_per_day(tmp_path):
    repo, conn = await _db(tmp_path)

    class Ad:
        def __init__(self):
            self.calls = []

        async def send_private_message(self, user_id, segments):
            self.calls.append(user_id)
            if user_id == 2:
                raise RuntimeError("boom")  # crashed target (live 2026-11-07)
            return SendMessageResult(message_seq=len(self.calls))

    ad = Ad()
    svc = _svc(ad, repo)
    svc.morning_targets = lambda: [("friend", 1), ("friend", 2)]
    sent, failed = await svc.morning_broadcast("早报内容")
    assert (sent, failed) == (1, 1)
    assert ad.calls == [1, 2]

    # a due-now re-fire after a relaunch must NOT re-send to either target
    sent, failed = await svc.morning_broadcast("早报内容")
    assert (sent, failed) == (0, 0)
    assert ad.calls == [1, 2]
    await conn.close()


# --------------------------------------------- parked-delivery guards
class ParkSvc:
    def __init__(self):
        self.sent: list = []

        class _R:
            self_id = SELF

        self.runtime = _R()

    async def send_text(self, scene, peer, text):
        self.sent.append((scene, peer))
        return True


def _sched(repo):
    return Scheduler(repo, ParkSvc(), ChatLog())


async def _park(repo, tid, scene, peer, text, tries=0, head=None):
    await repo.set_meta(f"{Scheduler.DELIVERY_KEY}{tid}", json.dumps({
        "scene": scene, "peer": peer, "text": text,
        "head": head or Scheduler._delivery_head(text),
        "next_at": 0, "tries": tries,
    }, ensure_ascii=False))


async def test_parked_retry_drops_on_delivery_evidence(tmp_path):
    repo, conn = await _db(tmp_path)
    await _put_msg(repo, "group", 42, 5, SELF, "做梦完成：补了 3 天，后续正文……")
    await _park(repo, 1, "group", 42, "做梦完成：补了 3 天，后续正文……")
    s = _sched(repo)
    s._last_delivery_try = 0
    await s._retry_pending_deliveries()
    assert await repo.get_meta("pending_deliver:1") is None  # dropped, not resent
    assert s.service.sent == []                                # no re-send happened
    await conn.close()


async def test_parked_retry_resends_without_evidence(tmp_path):
    repo, conn = await _db(tmp_path)
    await _park(repo, 1, "group", 43, "晨报头：今日无事")
    s = _sched(repo)
    s._last_delivery_try = 0
    await s._retry_pending_deliveries()
    assert s.service.sent == [("group", 43)]                  # genuinely undelivered
    assert await repo.get_meta("pending_deliver:1") is None
    await conn.close()


async def test_parked_retry_is_capped(tmp_path):
    repo, conn = await _db(tmp_path)
    await _park(repo, 1, "group", 43, "晨报头：今日无事", tries=Scheduler.DELIVERY_MAX_TRIES)
    s = _sched(repo)
    s._last_delivery_try = 0
    await s._retry_pending_deliveries()
    assert s.service.sent == []                                # gave up, no send
    assert await repo.get_meta("pending_deliver:1") is None    # key dropped
    await conn.close()
