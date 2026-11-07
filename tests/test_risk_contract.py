"""Contract-level outbound risk control (user direction 2026-11-07: 连续发
相同消息会被风控). The PUBLIC send surface of every ProtocolAdapter enforces
identical-content suppression, per-conversation pacing and a rolling hourly
budget, so no app layer above the contract can spam the wire even when its
own retry logic is wrong (live 2026-11-07: 11 identical digests overnight).
ProtocolError also gained contract kinds: refused / network / risk."""

import asyncio
import time

import pytest

from muze.core.service import ControlService
from muze.provider.base import ProtocolAdapter, ProtocolError, RiskPolicy
from muze.provider.milky.models import (
    Friend,
    Group,
    HistoryResult,
    IncomingMessage,
    LoginInfo,
    SendMessageResult,
)


def _seg(text):
    return [{"type": "text", "data": {"text": text}}]


class NullAdapter(ProtocolAdapter):
    """Minimal contract implementation (also the template a future
    OneBot11 / OneBot12 / Lagrange adapter would follow)."""

    def __init__(self):
        super().__init__()
        self.wire: list = []          # (scene, peer, text) actually sent
        self.fail_with: Exception | None = None

    async def start(self):
        return LoginInfo(user_id=1)

    async def close(self):
        pass

    async def events(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def get_login_info(self):
        return LoginInfo(user_id=1)

    async def get_friend_list(self):
        return []

    async def get_group_list(self):
        return []

    async def get_group_member_name(self, group_id, user_id):
        return ""

    async def get_history_messages(self, scene, peer_id, *, start_message_seq=None, limit=30):
        return HistoryResult()

    async def get_message(self, scene, peer_id, message_seq):
        return None

    async def get_resource_url(self, resource_id):
        return ""

    async def get_private_file_url(self, user_id, file_id, file_hash):
        return ""

    async def get_group_file_url(self, group_id, file_id):
        return ""

    async def _send_private_raw(self, user_id, segments):
        if self.fail_with:
            raise self.fail_with
        self.wire.append(("friend", user_id, segments[0]["data"]["text"]))
        return SendMessageResult(message_seq=len(self.wire))

    async def _send_group_raw(self, group_id, segments):
        if self.fail_with:
            raise self.fail_with
        self.wire.append(("group", group_id, segments[0]["data"]["text"]))
        return SendMessageResult(message_seq=len(self.wire))


def _fast_policy(**kw):
    p = RiskPolicy(min_gap_sec=0.0, gap_jitter_sec=0.0, dupe_window_sec=600.0,
                   hourly_budget=1000)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


# ------------------------------------------------ identical suppression
async def test_identical_send_within_window_is_suppressed():
    ad = NullAdapter()
    ad.risk = _fast_policy()
    r1 = await ad.send_group_message(42, _seg("日报：今日无事"))
    r2 = await ad.send_group_message(42, _seg("日报：今日无事"))
    assert len(ad.wire) == 1                 # second never hit the wire
    assert r2.message_seq == 0               # suppressed marker result
    assert r1.message_seq == 1
    # a DIFFERENT conversation is of course unaffected
    await ad.send_private_message(7, _seg("日报：今日无事"))
    assert ad.wire[-1][:2] == ("friend", 7)
    # and different content in the same conversation goes through
    await ad.send_group_message(42, _seg("日报：有更新"))
    assert len(ad.wire) == 3


async def test_suppression_window_expires():
    ad = NullAdapter()
    p = _fast_policy(dupe_window_sec=0.05)
    ad.risk = p
    await ad.send_group_message(42, _seg("同文"))
    await asyncio.sleep(0.08)
    await ad.send_group_message(42, _seg("同文"))
    assert len(ad.wire) == 2                 # window passed -> real send


# ------------------------------------ network-unknown vs refused deaths
async def test_network_death_suppresses_blind_resend():
    """The exact live-2026-11-07 shape: a send that actually landed but whose
    response was lost. The guard keeps the fingerprint, so a blind re-send of
    the same content within the window is suppressed at the wire."""
    ad = NullAdapter()
    ad.risk = _fast_policy()
    ad.fail_with = ProtocolError("send_group_message: [WinError 64]", kind="network")
    with pytest.raises(ProtocolError):
        await ad.send_group_message(42, _seg("做梦完成：补了 3 天"))
    ad.fail_with = None
    await ad.send_group_message(42, _seg("做梦完成：补了 3 天"))
    assert ad.wire == []                     # suppressed -- may have landed


async def test_refused_death_allows_resend():
    ad = NullAdapter()
    ad.risk = _fast_policy()
    ad.fail_with = ProtocolError("send_group_message: retcode=500", kind="refused")
    with pytest.raises(ProtocolError):
        await ad.send_group_message(42, _seg("确定没发出去"))
    ad.fail_with = None
    await ad.send_group_message(42, _seg("确定没发出去"))
    assert len(ad.wire) == 1                 # provably undelivered -> resend


# ----------------------------------------------------------- pacing
async def test_per_conversation_pacing_with_gap():
    ad = NullAdapter()
    ad.risk = _fast_policy(min_gap_sec=0.08)
    t0 = time.monotonic()
    await ad.send_group_message(42, _seg("一"))
    await ad.send_group_message(42, _seg("二"))
    assert time.monotonic() - t0 >= 0.07     # paced, not back-to-back
    # a different conversation is not delayed by this one's pacing
    t1 = time.monotonic()
    await ad.send_private_message(9, _seg("别的会话"))
    assert time.monotonic() - t1 < 0.05


# ------------------------------------------------------------ budget
async def test_hourly_budget_blocks_the_third_send():
    ad = NullAdapter()
    ad.risk = _fast_policy(hourly_budget=2)
    await ad.send_group_message(1, _seg("a"))
    await ad.send_group_message(2, _seg("b"))
    with pytest.raises(ProtocolError) as ei:
        await ad.send_group_message(3, _seg("c"))
    assert ei.value.kind == "risk"
    assert len(ad.wire) == 2


# ------------------------------------------------- error kind plumbing
async def test_client_marks_network_vs_refused(monkeypatch):
    from muze.provider.milky.client import MilkyClient

    c = MilkyClient(base_url="http://127.0.0.1:3010")

    class DeadSession:
        closed = False

        def post(self, url, **kw):
            class Ctx:
                async def __aenter__(self_inner):
                    raise asyncio.TimeoutError()

                async def __aexit__(self_inner, *a):
                    return False

            return Ctx()

        async def close(self):
            self.closed = True

    c._session = DeadSession()
    with pytest.raises(ProtocolError) as net:
        await c.call("send_group_message", {})
    assert net.value.kind == "network" and not net.value.refused

    class RefusedSession:
        closed = False

        def post(self, url, **kw):
            class Resp:
                status = 200

                async def json(self, content_type=None):
                    return {"status": "failed", "retcode": 500, "message": "PMHQ"}

            class Ctx:
                async def __aenter__(self_inner):
                    return Resp()

                async def __aexit__(self_inner, *a):
                    return False

            return Ctx()

        async def close(self):
            self.closed = True

    c2 = MilkyClient(base_url="http://127.0.0.1:3010")
    c2._session = RefusedSession()
    with pytest.raises(ProtocolError) as refused:
        await c2.call("send_group_message", {})
    assert refused.value.kind == "refused" and refused.value.refused


def test_definitely_rejected_uses_contract_kind():
    assert ControlService._definitely_rejected(
        ProtocolError("x: retcode=500", kind="refused")
    )
    assert not ControlService._definitely_rejected(
        ProtocolError("x: [WinError 64]", kind="network")
    )
    # plain errors (non-contract fakes) keep the string fallback
    assert ControlService._definitely_rejected(ProtocolError("x: retcode=500"))
    assert not ControlService._definitely_rejected(ProtocolError("x: "))
