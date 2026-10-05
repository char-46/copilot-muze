"""Instance lock + sqlite busy resilience."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time

import aiosqlite
import pytest

from muze import instlock


def test_same_process_reacquire_shares_lock(tmp_path):
    """Two App instances in ONE serve process may share an account dir
    (same QQ via two Milky endpoints): the second acquire gets the shared
    handle instead of deadlocking on itself."""
    p = tmp_path / "serve.lock"
    l1 = instlock.acquire(p, timeout=0.5)
    assert l1 is not None
    l2 = instlock.acquire(p, timeout=0.5)
    assert l2 is not None and l2 is l1  # shared, instant (no 20s wait)
    # release once -> next acquire is fresh
    l1._handle.close()
    l3 = instlock.acquire(p, timeout=1.0)
    assert l3 is not None and l3 is not l1


def test_instance_lock_excludes_other_process(tmp_path):
    """A REAL other process holding the lock blocks us until it exits."""
    p = tmp_path / "serve.lock"
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            textwrap.dedent(f"""
            import time
            from muze import instlock
            l = instlock.acquire({str(p)!r}, timeout=1.0)
            assert l is not None
            print("held", flush=True)
            time.sleep(3)
            """),
        ],
        cwd=str(pytest.__file__ and __import__("pathlib").Path(__file__).parent.parent),
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        denied = instlock.acquire(p, timeout=0.4)  # holder alive -> blocked
        assert denied is None
        assert time.monotonic() - t0 >= 0.3
    finally:
        holder.kill()
        holder.wait()
    # after the holder dies the OS releases the lock
    ok = None
    for _ in range(40):
        ok = instlock.acquire(p, timeout=0.1)
        if ok is not None:
            break
        time.sleep(0.1)
    assert ok is not None


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
    """The connection-level retry wrapper (db.py open_db) makes any write
    survive a transient BUSY — not just upsert_message."""
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
        real = conn._muze_orig_execute
        calls = {"n": 0}

        def flaky(sql, params=None):
            if "INSERT INTO messages" in sql and calls["n"] == 0:
                calls["n"] += 1

                class _BusyCM:
                    def __await__(self):
                        async def _raise():
                            raise aiosqlite.OperationalError("database is locked")
                        return _raise().__await__()

                    async def __aenter__(self):
                        raise aiosqlite.OperationalError("database is locked")

                    async def __aexit__(self, *exc):
                        return None

                return _BusyCM()

            if params is None:
                return real(sql)
            return real(sql, params)

        monkeypatch.setattr(conn, "_muze_orig_execute", flaky)
        inserted = await repo.upsert_message(msg, "hi", "event")
        assert inserted is True
        assert calls["n"] == 1  # one busy, then success via the conn wrapper
    finally:
        await conn.close()
