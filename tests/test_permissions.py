"""Permission model: unconditional reads, controller-only writes, and
routing of out-of-session writes to the controller for approval."""

import pytest

from muze.ai.tools import ToolRunner
from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.base import Caps, ProtocolAdapter
from muze.provider.milky.models import SendMessageResult
from muze.store.db import open_db
from muze.store.repo import Repository


class Recorder(ProtocolAdapter):
    def __init__(self):
        self.caps = Caps(impl_name="fake")
        self.sent: list[tuple[str, int, list]] = []

    async def start(self):
        raise NotImplementedError

    async def close(self):
        pass

    def events(self):
        raise NotImplementedError

    async def get_login_info(self):
        raise NotImplementedError

    async def get_friend_list(self):
        return []

    async def get_group_list(self):
        return []

    async def get_group_member_name(self, group_id, user_id):
        return ""

    async def send_private_message(self, user_id, segments):
        self.sent.append(("friend", user_id, segments))
        return SendMessageResult(message_seq=1, time=1759000000)

    async def send_group_message(self, group_id, segments):
        self.sent.append(("group", group_id, segments))
        return SendMessageResult(message_seq=2, time=1759000000)

    async def get_history_messages(self, scene, peer_id, *, start_message_seq=None, limit=30):
        raise NotImplementedError

    async def get_message(self, scene, peer_id, message_seq):
        return None

    async def get_resource_url(self, resource_id):
        return "http://x"

    async def get_private_file_url(self, user_id, file_id, file_hash):
        return "http://x"

    async def get_group_file_url(self, group_id, file_id):
        return "http://x"


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "p.db"))
    repo = Repository(conn)
    adapter = Recorder()
    cfg = MuzeConfig()
    cfg.control.controller = [10001]
    cfg.control.control_group = 20002
    runtime = Runtime()
    service = ControlService(cfg, adapter, repo, ChatLog(), runtime)
    yield service, repo, adapter, cfg
    await conn.close()


def ctx_qq(scene, peer, source="qq"):
    return ControlContext(source=source, chat_key=f"{source}:{scene}:{peer}", scene=scene, peer_id=peer)


def test_control_ctx_classification(env):
    service, *_ = env
    assert service.is_control_ctx(ctx_qq("friend", 10001)) is True   # authority DM
    assert service.is_control_ctx(ctx_qq("group", 20002)) is True    # control group
    assert service.is_control_ctx(ctx_qq("group", 999)) is False     # random group
    assert service.is_control_ctx(ctx_qq("friend", 888)) is False    # stranger DM
    # local fronts are the operator
    assert service.is_control_ctx(ctx_qq("", 0, source="cli")) is True
    assert service.is_control_ctx(ctx_qq("", 0, source="web")) is True
    # requires_approval is the inverse
    assert service.requires_approval(ctx_qq("group", 999)) is True
    assert service.requires_approval(ctx_qq("group", 20002)) is False


async def test_send_in_control_session_is_immediate(env):
    service, repo, adapter, _ = env
    out = await service.request_send(
        "friend", 555, [{"type": "text", "data": {"text": "hi"}}], "hi",
        ctx_qq("friend", 10001), "agent",
    )
    assert len(adapter.sent) == 1 and adapter.sent[0][1] == 555
    assert "已发送" in out.text
    assert await repo.list_pending_actions() == []


async def test_send_outside_control_routes_to_controller(env):
    service, repo, adapter, _ = env
    out = await service.request_send(
        "group", 999, [{"type": "text", "data": {"text": "hi"}}], "hi",
        ctx_qq("group", 999), "agent",
    )
    # nothing was sent to the target group...
    assert all(t[1] != 999 for t in adapter.sent)
    # ...but the controller was notified in the control group
    assert adapter.sent and adapter.sent[0][0] == "group" and adapter.sent[0][1] == 20002
    # a pending action exists, addressed to the controller
    rows = await repo.list_pending_actions()
    assert len(rows) == 1
    assert rows[0]["kind"] == "send"
    assert (rows[0]["control_scene"], rows[0]["control_peer_id"]) == ("group", 20002)
    assert "待控制者确认" in out.text


async def test_generic_write_tool_gated_then_approved(env):
    service, repo, adapter, _ = env
    runner = ToolRunner(service, repo, adapter, None, ctx_qq("group", 999))
    import json

    res = json.loads(await runner.run("remember", {"content": "喜欢乌龙茶", "subject": "owner"}))
    assert res.get("needs_approval") is True
    assert await repo.list_memories(active_only=False) == []  # not written yet
    rows = await repo.list_pending_actions()
    assert rows and rows[0]["kind"] == "op"

    # controller approves -> the op re-runs and the memory lands
    outcome = await service.confirm_action(rows[0]["id"], ctx_qq("group", 20002))
    assert "已执行" in outcome.text
    mems = await repo.list_memories(active_only=False)
    assert len(mems) == 1 and "乌龙茶" in mems[0]["content"]


async def test_read_tool_never_gated(env):
    service, repo, adapter, _ = env
    runner = ToolRunner(service, repo, adapter, None, ctx_qq("group", 999))
    import json

    res = json.loads(await runner.run("get_recent_messages", {"scene": "group", "peer_id": 999}))
    assert "messages" in res  # ran immediately, no approval


async def test_cli_write_command_gated_read_allowed(env):
    service, repo, adapter, _ = env
    runner = ToolRunner(service, repo, adapter, None, ctx_qq("group", 999))
    import json

    gated = json.loads(await runner.run("cli", {"command": "send friend/1 hello"}))
    assert gated.get("needs_approval") is True
    assert await repo.list_pending_actions() != []

    read = json.loads(await runner.run("cli", {"command": "help"}))
    assert "命令列表" in read.get("output", "")


async def test_memory_tools_gated_and_work(env):
    import json

    service, repo, adapter, _ = env
    mid = await repo.add_memory("fact", "owner", "喜欢乌龙茶", source="chat")

    outside = ToolRunner(service, repo, adapter, None, ctx_qq("group", 999))
    # search = read -> allowed anywhere
    res = json.loads(await outside.run("memory_search", {"query": "乌龙"}))
    assert res["count"] == 1 and res["memories"][0]["id"] == mid
    # update outside the control session -> gated
    g = json.loads(await outside.run("memory_update", {"id": mid, "content": "喜欢普洱"}))
    assert g.get("needs_approval") is True
    rows = await repo.list_pending_actions()
    assert rows and rows[0]["kind"] == "op"

    # controller approves -> applied
    await service.confirm_action(rows[0]["id"], ctx_qq("group", 20002))
    assert (await repo.list_memories())[0]["content"] == "喜欢普洱"

    # in the control session, deletes run directly
    inside = ToolRunner(service, repo, adapter, None, ctx_qq("friend", 10001))
    d = json.loads(await inside.run("memory_delete", {"id": mid}))
    assert d.get("deleted") is True
    assert await repo.list_memories() == []  # active-only listing empty
