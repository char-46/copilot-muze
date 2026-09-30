"""Multi-account isolation: data dir binds to one owner uin."""

import pytest

from muze.store.db import open_db
from muze.store.repo import Repository


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "o.db"))
    yield Repository(conn)
    await conn.close()


async def test_bind_owner_first_wins(repo):
    assert await repo.bind_owner(10000) is None      # first bind records
    assert await repo.bind_owner(10000) is None      # same account ok
    assert await repo.bind_owner(20000) == 10000     # different account refused
    assert await repo.owner_uin() == 10000           # binding unchanged


async def test_bind_owner_survives_reopen(tmp_path):
    conn = await open_db(str(tmp_path / "o2.db"))
    repo = Repository(conn)
    await repo.bind_owner(111)
    await conn.close()
    conn2 = await open_db(str(tmp_path / "o2.db"))
    repo2 = Repository(conn2)
    assert await repo2.owner_uin() == 111
    assert await repo2.bind_owner(222) == 111
    await conn2.close()
