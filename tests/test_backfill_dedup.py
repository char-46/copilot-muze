"""Shared-store pull dedup: one QQ behind several endpoints shares one repo;
pulls must not be duplicated across the sibling Backfillers."""

from __future__ import annotations

import asyncio

import muze.ingest.backfill as bf_mod
from muze.ingest.backfill import (
    Backfiller,
    _claim_pull,
    _release_pull,
    _RUN_STATE,
    _run_state,
)


def test_claim_release_and_expiry():
    conv = ("group", 123)
    assert _claim_pull(conv) is True
    assert _claim_pull(conv) is False  # in flight: second caller denied
    _release_pull(conv)
    assert _claim_pull(conv) is True  # released: claimable again
    _release_pull(conv)

    # a crashed holder cannot block forever: the expiry passing frees it
    import time as _t

    bf_mod._PULL_CLAIMS[conv] = _t.monotonic() - 1  # force into the past
    assert _claim_pull(conv) is True
    _release_pull(conv)


async def test_conversation_pull_skips_while_claimed(caplog):
    """A second pull of an in-flight conversation skips instead of racing
    (the bf cursor is written by one pager at a time)."""
    b = Backfiller.__new__(Backfiller)  # no __init__ dependencies needed
    called = []

    async def inner(scene, peer, reporter=None, force=False, since_ts=None):
        called.append((scene, peer))
        return 7

    b._backfill_conversation_inner = inner
    conv = ("group", 42)
    assert _claim_pull(conv)  # simulate a sibling already pulling
    try:
        n = await b._backfill_conversation("group", 42)
        assert n == 0 and called == []  # skipped, no inner call
    finally:
        _release_pull(conv)
    # free claim -> inner runs and the claim is released afterwards
    n = await b._backfill_conversation("group", 42)
    assert n == 7 and called == [("group", 42)]
    assert _claim_pull(conv) is True
    _release_pull(conv)


async def test_background_run_dedupes_across_sibling_backfillers(monkeypatch):
    """Boot roam / rescan (background=True) over the same shared repo:
    a running sibling or a very recent full run makes this one skip."""

    class FakeConn:
        pass

    class FakeRepo:
        def __init__(self):
            self.conn = FakeConn()

    class FakeCfg:
        class ingest:
            class backfill:
                concurrency = 1
                conv_delay = 0
                page_limit = 10
                max_pages_per_conv = 1
                page_delay = 0

    b = Backfiller.__new__(Backfiller)
    b.repo = FakeRepo()
    b.cfg = FakeCfg
    b.running = False
    b._stop = asyncio.Event()
    b._task = b._rescan_task = None
    b._reporter = None
    b.last_run_at = 0.0
    b.last_new_messages = 0

    async def boom_targets():
        raise AssertionError("must not even enumerate targets when skipping")

    b.target_conversations = boom_targets

    st = _run_state(b.repo.conn)
    try:
        st["running"] = True  # sibling pulling right now
        assert await b.run_once(background=True) is None

        st["running"] = False
        st["last_run_at"] = bf_mod.time.monotonic() - 10  # ran 10s ago
        assert await b.run_once(background=True) is None

        st["last_run_at"] = bf_mod.time.monotonic() - (
            bf_mod._RUN_FRESH_SEC + 60
        )  # stale: must proceed (fails at target_conversations)
        try:
            await b.run_once(background=True)
            raised = False
        except AssertionError:
            raised = True  # boom() hit => the run was NOT skipped
        assert raised
    finally:
        _RUN_STATE.pop(id(b.repo.conn), None)
