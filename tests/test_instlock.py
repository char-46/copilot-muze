"""Instance lock + sqlite busy resilience."""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from muze import instlock


def test_instance_lock_excludes_second_process(tmp_path):
    p = tmp_path / "serve.lock"
    l1 = instlock.acquire(p, timeout=0.3)
    assert l1 is not None
    # a second acquisition (same or another process) must fail fast
    t0 = time.monotonic()
    l2 = instlock.acquire(p, timeout=0.5)
    assert l2 is None
    assert time.monotonic() - t0 >= 0.4
    # releasing (closing the handle) lets the next one in
    l1._handle.close()
    l3 = instlock.acquire(p, timeout=1.0)
    assert l3 is not None


def test_instance_lock_reclaimed_after_holder_garbage_collected(tmp_path):
    p = tmp_path / "serve.lock"
    l1 = instlock.acquire(p, timeout=0.3)
    assert l1 is not None
    del l1  # handle closed on GC -> lock released by the OS
    import gc

    gc.collect()
    for _ in range(20):
        if instlock.acquire(p, timeout=0.1) is not None:
            return
        time.sleep(0.05)
    pytest.fail("lock not released after holder death")


async def test_busy_timeout_is_30s(tmp_path):
    from muze.store.db import open_db

    conn = await open_db(str(tmp_path / "m.db"))
    try:
        async with conn.execute("PRAGMA busy_timeout") as cur:
            v = (await cur.fetchone())[0]
        assert v == 30000
    finally:
        await conn.close()


async def test_upsert_message_retries_transient_busy(tmp_path, monkeypatch):
    from muze.store.db import open_db
    from muze.store.repo import Repository
    from muze.provider.milky.models import IncomingMessage

    conn = await open_db(str(tmp_path / "m.db"))
    try:
        repo = Repository(conn)
        msg = IncomingMessage(
            message_scene="friend",
            peer_id=123,
            message_seq=1,
            sender_id=456,
            time=1700000000,
            segments=[],
        )
        real = conn.execute
        calls = {"n": 0}

        async def flaky(sql, params=None):
            if "INSERT INTO messages" in sql and calls["n"] == 0:
                calls["n"] += 1
                raise aiosqlite.OperationalError("database is locked")
            return await real(sql, params or ())

        monkeypatch.setattr(conn, "execute", flaky)
        inserted = await repo.upsert_message(msg, "hi", "event")
        assert inserted is True
        assert calls["n"] == 1  # one busy, then success via retry
    finally:
        await conn.close()
