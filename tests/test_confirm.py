"""ControlService: send confirmation flow and target parsing."""

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.base import Caps, ProtocolAdapter
from muze.provider.milky.models import SendMessageResult
from muze.store.db import open_db
from muze.store.repo import Repository


class SendRecorder(ProtocolAdapter):
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
    conn = await open_db(str(tmp_path / "c.db"))
    repo = Repository(conn)
    adapter = SendRecorder()
    cfg = MuzeConfig()
    cfg.control.controller = [10001]
    cfg.control.control_group = 20002
    service = ControlService(cfg, adapter, repo, ChatLog(), Runtime())
    yield service, repo, adapter, cfg
    await conn.close()


def qq_ctx(service_cfg):
    return ControlContext(
        source="qq", chat_key="qq:friend:10001", scene="friend",
        peer_id=10001, user_label="admin", auto_confirm=False,
    )


async def test_auto_confirm_sends_immediately(env):
    service, repo, adapter, cfg = env
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    out = await service.request_send("friend", 555, [{"type": "text", "data": {"text": "hi"}}], "hi", ctx, "cli")
    assert len(adapter.sent) == 1 and adapter.sent[0][1] == 555
    assert "已发送" in out.text and out.is_error is False
    assert await repo.list_pending_actions() == []


async def test_confirm_flow_via_pending(env):
    service, repo, adapter, cfg = env
    # request comes from a NON-control conversation -> routed to the controller
    src = ControlContext(
        source="qq", chat_key="qq:group:999", scene="group", peer_id=999, user_label="random"
    )
    out = await service.request_send(
        "group", 30003, [{"type": "text", "data": {"text": "代发内容"}}], "代发内容", src, "agent"
    )
    assert all(t[1] != 30003 for t in adapter.sent)  # not sent yet
    assert "待控制者确认" in out.text
    rows = await repo.list_pending_actions()
    assert len(rows) == 1
    assert (rows[0]["control_scene"], rows[0]["control_peer_id"]) == ("group", 20002)

    confirm_ctx = ControlContext(
        source="qq", chat_key="qq:group:20002", scene="group", peer_id=20002, user_label="admin"
    )
    confirmed = await service.confirm_action(rows[0]["id"], confirm_ctx)
    assert "已发送" in confirmed.text
    assert any(t[0] == "group" and t[1] == 30003 for t in adapter.sent)
    assert await repo.list_pending_actions() == []


async def test_parse_target(env):
    service, *_ = env
    assert service.parse_target("group/123") == ("group", 123)
    assert service.parse_target("456") == ("friend", 456)
    with pytest.raises(ValueError):
        service.parse_target("space/1")


async def test_group_trigger_rules(env):
    service, *_ = env
    T = service.qq_should_trigger
    # --- group: only /cmd or ::llm ever trigger, never plain chatter ---
    assert T("group", 20002, 10001, True, "/status") is True      # @bot + cmd
    assert T("group", 20002, 10001, True, "::帮我总结") is True   # @bot + llm
    assert T("group", 20002, 10001, True, "你好") is False        # plain chat, even @bot
    assert T("group", 20002, 10001, False, "/status") is True     # bare cmd, primary bot
    assert T("group", 20002, 10001, False, "::继续") is True      # bare llm, primary bot
    assert T("group", 20002, 10001, False, "随便聊聊") is False   # bare plain text
    # non-primary bot in the same group: only answers when explicitly @-ed
    service.runtime.is_primary = False
    assert T("group", 20002, 10001, True, "/status") is True      # @me -> I answer
    assert T("group", 20002, 10001, False, "/status") is False    # bare -> primary answers
    assert T("group", 20002, 10001, False, "::hi") is False
    service.runtime.is_primary = True
    # stranger in control group -> never
    assert T("group", 20002, 99999, True, "/status") is False
    # --- friend: same /cmd + ::llm rule ---
    assert T("friend", 10001, 10001, False, "/status") is True
    assert T("friend", 10001, 10001, False, "::你好") is True
    assert T("friend", 10001, 10001, False, "hi") is False        # plain text: archived only
    # non-authority private chat -> never
    assert T("friend", 88888, 88888, False, "/status") is False
    # --- self-send isolation (controller == one of the bots) ---
    # this account sending to ANOTHER bot: outgoing, never a command here;
    # the receiving bot answers alone
    assert T("friend", 77777, 10001, False, "/status", is_self=True) is False
    assert T("group", 20002, 10001, False, "/status", is_self=True) is False
    # true self-chat (peer == self, controller is self) still works
    service.runtime.self_id = 10001
    assert T("friend", 10001, 10001, False, "::记一下", is_self=True) is True
    service.runtime.self_id = 0


async def test_llm_prefix_routing(env):
    service, *_ = env
    from muze.core.service import is_llm_invocation, strip_llm_prefix

    assert is_llm_invocation("::hi") and is_llm_invocation("：：你好")
    assert is_llm_invocation(":：x")  # mixed half/full width colons
    assert not is_llm_invocation(":hi") and not is_llm_invocation("/cmd")
    assert strip_llm_prefix("::你好") == "你好"
    assert strip_llm_prefix("：：  空格 ") == "空格"

    # execute(): plain text is refused with a hint, not routed to the agent
    ctx = ControlContext(source="web", chat_key="web:x", scene="", peer_id=0)
    out = await service.execute("普通文本", ctx)
    assert out.is_error and "::" in out.text
