"""directory(kind=members): single-group rosters, cross-group lookup and
intersection; adapter-side roster caching."""

import json

import pytest

from muze.ai.tools import ToolRunner
from muze.chatlog import ChatLog
from muze.config import MilkyEndpoint, MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.milky.adapter import MilkyAdapter
from muze.store.db import open_db
from muze.store.repo import Repository

from test_permissions import Recorder


class MembersAdapter(Recorder):
    """Fake with canned rosters; group 404 raises."""

    def __init__(self):
        super().__init__()
        self.list_calls: list[int] = []
        self._rosters = {
            101: [
                {"user_id": 1, "user_name": "alice", "card_name": "爱丽丝"},
                {"user_id": 2, "user_name": "bob"},
                {"user_id": 3, "user_name": "carol", "card_name": "卡罗"},
            ],
            202: [
                {"user_id": 2, "user_name": "bob"},
                {"user_id": 3, "user_name": "carol"},
                {"user_id": 9, "user_name": "dave"},
            ],
        }

    async def get_group_member_list(self, group_id):
        self.list_calls.append(group_id)
        from muze.provider.base import ProtocolError

        if group_id == 404:
            raise ProtocolError("no such group")
        from muze.provider.milky.models import GroupMember

        return [GroupMember.model_validate(x) for x in self._rosters.get(group_id, [])]

    async def get_friend_info(self, user_id):
        from muze.provider.milky.models import Friend

        return Friend(user_id=user_id, user_name="nick", user_remark="备注哥")


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "d.db"))
    repo = Repository(conn)
    adapter = MembersAdapter()
    service = ControlService(MuzeConfig(), adapter, repo, ChatLog(), Runtime())
    ctx = ControlContext(source="qq", chat_key="qq:friend:1", scene="friend", peer_id=1)
    runner = ToolRunner(service, repo, adapter, None, ctx)
    yield runner, adapter
    await conn.close()


async def run(runner, args):
    return json.loads(await runner.run("directory", args))


async def test_single_group_full_roster(env):
    runner, _ = env
    res = await run(runner, {"kind": "members", "group_id": 101})
    assert res["member_count"] == 3
    assert res["returned"] == 3
    assert {m["uin"] for m in res["members"]} == {1, 2, 3}
    assert res["members"][0]["card"] == "爱丽丝"


async def test_single_group_name_filter_and_truncation(env):
    runner, _ = env
    res = await run(runner, {"kind": "members", "group_id": 101, "name": "BOB"})
    assert res["returned"] == 1 and res["members"][0]["uin"] == 2
    res = await run(runner, {"kind": "members", "group_id": 101, "name": "卡罗"})
    assert res["returned"] == 1 and res["members"][0]["uin"] == 3
    res = await run(runner, {"kind": "members", "group_id": 101, "limit": 1})
    assert res["truncated"] is True and res["returned"] == 1


async def test_cross_group_uin_lookup(env):
    runner, adapter = env
    res = await run(runner, {"kind": "members", "group_ids": [101, 202, 404], "uin": 2})
    assert res["mode"] == "lookup"
    by = {g["group_id"]: g for g in res["groups"]}
    assert by[101]["found"] is True and by[202]["found"] is True
    assert "error" in by[404]  # partial failure tolerated
    assert adapter.list_calls == [101, 202, 404]


async def test_cross_group_intersection(env):
    runner, _ = env
    res = await run(runner, {"kind": "members", "group_ids": [101, 202]})
    assert res["mode"] == "intersection"
    assert res["common_count"] == 2
    assert {m["uin"] for m in res["common_members"]} == {2, 3}


async def test_too_many_groups_rejected(env):
    runner, _ = env
    res = await run(runner, {"kind": "members", "group_ids": list(range(11, 23))})
    assert "最多" in res["error"]


async def test_members_requires_group(env):
    runner, _ = env
    res = await run(runner, {"kind": "members"})
    assert "group_id" in res["error"]


async def test_whois_enriched_with_remark(env):
    runner, _ = env
    res = await run(runner, {"kind": "whois", "uin": 5})
    assert res["friend_name"] == "备注哥"  # display = remark first
    assert res["friend_remark"] == "备注哥"


async def test_adapter_roster_cache(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def call(self, method, params):
            self.calls.append((method, params))
            return {"members": [{"user_id": 7, "user_name": "zoe"}]}

    ad = MilkyAdapter(MilkyEndpoint())
    fc = FakeClient()
    ad.client = fc
    m1 = await ad.get_group_member_list(555)
    m2 = await ad.get_group_member_list(555)
    assert [m.user_id for m in m1] == [7]
    assert len(fc.calls) == 1  # second hit came from cache
    # per-member display cache pre-warmed
    assert ad._member_names[(555, 7)] == "zoe"
    # TTL expiry -> refetch
    ts, members = ad._member_lists[555]
    ad._member_lists[555] = (ts - ad.MEMBER_LIST_TTL - 1, members)
    await ad.get_group_member_list(555)
    assert len(fc.calls) == 2
