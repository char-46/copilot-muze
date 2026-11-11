"""Post-incident batch (live 2026-11-10/11): (1) one network blip during a
relay storm permanently degraded forward chains -- every later digest arrived
as separate chunk messages; (2) a day-long network outage made the embedder
retry every ~13 min (flat 300s cooldown) and each failed load left allocator
residue until RSS hit 8G while every probe said "not loaded"; (3) the dream
summary header said 做梦完成 with a failed day listed inside it."""

import asyncio

import pytest

from muze.config import MuzeConfig, RagEmbedding
from muze.provider.base import NotSupported, ProtocolError
from muze.provider.milky.adapter import MilkyAdapter
from muze.provider.milky.models import SendMessageResult


def _adapter():
    ad = MilkyAdapter.__new__(MilkyAdapter)
    from muze.provider.base import Caps

    ad.caps = Caps()
    ad.caps.send_forward = True
    ad.self_id = 1
    ad._send_tracks = {}
    ad._wire_times = []
    return ad


# ------------------------------------------- forward degrade policy
async def test_network_failure_does_not_degrade_forward():
    ad = _adapter()
    err = ProtocolError("send_group_message: [WinError 64]", kind="network")

    async def raw(group_id, segments):
        raise err

    ad._send_group_raw = raw
    with pytest.raises(ProtocolError):
        await ad.send_forward_message("group", 42, [[{"type": "text", "data": {"text": "x"}}]])
    assert ad.caps.send_forward is True  # capability intact: it was the network


async def test_refusal_needs_two_strikes_to_degrade():
    ad = _adapter()
    n = 0

    async def raw(group_id, segments):
        nonlocal n
        n += 1
        raise ProtocolError("send_group_message: retcode=500", kind="refused")

    ad._send_group_raw = raw
    nodes = [[{"type": "text", "data": {"text": "x"}}]]
    with pytest.raises(NotSupported):
        await ad.send_forward_message("group", 42, nodes)
    assert ad.caps.send_forward is True  # first refusal: strike, not degrade
    with pytest.raises(NotSupported):
        await ad.send_forward_message("group", 42, nodes)
    assert ad.caps.send_forward is False  # second consecutive refusal: degrade


async def test_degraded_forward_rearms_after_an_hour(monkeypatch):
    ad = _adapter()
    ad.caps.send_forward = False
    ad._forward_degraded_at = 0.0  # "degraded long ago"

    async def raw(group_id, segments):
        return SendMessageResult(message_seq=1)

    ad._send_group_raw = raw
    res = await ad.send_forward_message(
        "group", 42, [[{"type": "text", "data": {"text": "x"}}]]
    )
    assert res.message_seq == 1 and ad.caps.send_forward is True  # re-armed + probe ok

    # fresh degrade: within the re-arm window it stays NotSupported
    ad2 = _adapter()
    ad2.caps.send_forward = False
    import time as _t

    ad2._forward_degraded_at = _t.monotonic()
    with pytest.raises(NotSupported):
        await ad2.send_forward_message(
            "group", 42, [[{"type": "text", "data": {"text": "x"}}]]
        )


# ------------------------------------------------- cooldown escalation
def test_cooldown_escalates_and_resets():
    from muze.ai.embeddings import Embedder

    e = Embedder(RagEmbedding())
    assert e.cfg.cooldown_sec > 0
    for _ in range(max(1, int(e.cfg.max_retries))):
        e._fail()
    first = e._cooldown_until - _now()
    for _ in range(max(1, int(e.cfg.max_retries))):
        e._fail()
    second = e._cooldown_until - _now()
    assert second > first  # doubled
    e._ok()
    for _ in range(max(1, int(e.cfg.max_retries))):
        e._fail()
    again = e._cooldown_until - _now()
    assert again <= first + 5  # streak reset by success -> base cooldown again


def _now():
    import time

    return time.time()


# ------------------------------------------------- dream summary wording
def test_dream_summary_admits_failed_days():
    """The header must never claim 做梦完成 while a day failed (live
    2026-11-11: relay storm at 02:36 failed one day; the digest still opened
    with 做梦完成 and the user read both phrases as a contradiction)."""
    from muze.digest import dream_summary_head

    out = dream_summary_head(
        ok_days=["2026-11-09"], bad_days=["2026-11-10"],
        stale=[], swept=21, rollups=[], profiled=10, healed=[],
        target="2026-11-10", new_memories=5,
    )
    assert out.startswith("做梦部分完成")
    assert "补做 1 天" in out and "失败 1 天" in out
    assert "2026-11-10" in out and "明晚自动补做" in out
    assert not out.startswith("做梦完成：")

    ok = dream_summary_head(
        ok_days=["2026-11-09", "2026-11-10"], bad_days=[],
        stale=[], swept=0, rollups=[], profiled=0, healed=[],
        target="2026-11-10", new_memories=0,
    )
    assert ok.startswith("做梦完成：补做 2 天")
