"""Regression for issue #1 (mattabate): with two account instances sharing
one pooled DB connection, shutdown() set self.conn = None after handing the
connection off -- and still evaluated self.conn.close() as an ARGUMENT to
_safe(), which runs before _safe's try/except. Every stop/restart of such a
setup raised AttributeError('NoneType' object has no attribute 'close') and
run() reported the account task as crashed."""

import asyncio
import types

from muze.main import _DB_POOL, App


class FakeConn:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeRepo:
    async def set_meta(self, key, value):
        pass


def _app(conn, db_path):
    a = App.__new__(App)
    a.spec = types.SimpleNamespace(name="test")
    a._stop = asyncio.Event()
    a.repo = FakeRepo()
    a.runtime = types.SimpleNamespace()
    a._mt_task = None
    a._wake_task = None
    a.backfiller = None
    a.batch = None
    a.pipeline = None
    a.adapter = None
    a.conn = conn
    a._db_path = db_path
    return a


async def test_first_sharer_shutdown_does_not_crash_or_close():
    _DB_POOL.clear()
    conn = FakeConn()
    _DB_POOL["shared.db"] = {"conn": conn, "refs": 2}
    a1 = _app(conn, "shared.db")
    a2 = _app(conn, "shared.db")

    await a1.shutdown()  # used to raise AttributeError (issue #1)
    assert not conn.closed              # handed off: the pool keeps it alive
    assert _DB_POOL["shared.db"]["refs"] == 1
    assert a1.conn is None and a2.conn is conn

    await a2.shutdown()                 # last sharer: actually closes
    assert conn.closed
    assert "shared.db" not in _DB_POOL


async def test_own_connection_still_closes():
    _DB_POOL.clear()
    conn = FakeConn()
    a = _app(conn, "solo.db")           # no pool entry: private connection
    await a.shutdown()
    assert conn.closed


async def test_shutdown_is_idempotent():
    _DB_POOL.clear()
    conn = FakeConn()
    a = _app(conn, "solo.db")
    await a.shutdown()
    await a.shutdown()                  # second call: no-op, no error
    assert conn.closed
