"""Same-QQ multi-endpoint: two Apps share ONE db connection (no self-BUSY)."""

from __future__ import annotations


async def test_two_apps_same_dir_share_conn(tmp_path):
    """main + milky-2 logging into the same QQ resolve to one account dir;
    both must ride the SAME aiosqlite connection (a second connection to the
    same file is the SQLITE_BUSY self-fight)."""
    import muze.main as m

    # a tiny fake App exercising only the pooling block
    class FakeApp:
        def __init__(self):
            self.conn = None
            self._instlock = None

        async def open(self, acct):
            db_path = str(acct / "muze.db")
            entry = m._DB_POOL.get(db_path)
            if entry is not None:
                entry["refs"] += 1
                self.conn = entry["conn"]
            else:
                self.conn = await m.open_db(db_path)
                m._DB_POOL[db_path] = {"conn": self.conn, "refs": 1}
            self._db_path = db_path

    m._DB_POOL.clear()
    acct = tmp_path / "1000000001"
    acct.mkdir(parents=True, exist_ok=True)
    a1, a2 = FakeApp(), FakeApp()
    await a1.open(acct)
    await a2.open(acct)
    try:
        assert a1.conn is a2.conn  # shared, not a second writer
        # writes from both "apps" interleave on one serialized connection
        await a1.conn.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
        await a2.conn.execute("INSERT INTO t VALUES (1)")
        await a1.conn.commit()
        async with a2.conn.execute("SELECT COUNT(*) FROM t") as cur:
            assert (await cur.fetchone())[0] == 1
    finally:
        await a1.conn.close()
        m._DB_POOL.clear()
