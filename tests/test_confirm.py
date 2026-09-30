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
    cfg.authority = [10001]
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
    ctx = qq_ctx(cfg)
    out = await service.request_send("group", 20002, [{"type": "text", "data": {"text": "代发内容"}}], "代发内容", ctx, "agent")
    assert adapter.sent == []  # nothing sent yet
    assert "待确认" in out.text
    rows = await repo.list_pending_actions("friend", 10001)
    assert len(rows) == 1

    confirmed = await service.confirm_action(rows[0]["id"], ctx)
    assert "已发送" in confirmed.text
    assert len(adapter.sent) == 1 and adapter.sent[0][0] == "group"
    assert await repo.list_pending_actions() == []


async def test_parse_target(env):
    service, *_ = env
    assert service.parse_target("group/123") == ("group", 123)
    assert service.parse_target("456") == ("friend", 456)
    with pytest.raises(ValueError):
        service.parse_target("space/1")


async def test_group_trigger_rules(env):
    service, *_ = env
    # admin in control group, mentions bot -> triggers
    assert service.qq_should_trigger("group", 20002, 10001, True, "你好") is True
    # admin without mention, no slash -> no trigger (at_or_slash mode)
    assert service.qq_should_trigger("group", 20002, 10001, False, "随便聊聊") is False
    # slash always triggers
    assert service.qq_should_trigger("group", 20002, 10001, False, "/status") is True
    # stranger in control group -> never
    assert service.qq_should_trigger("group", 20002, 99999, True, "/status") is False
    # admin via private chat -> always
    assert service.qq_should_trigger("friend", 10001, 10001, False, "hi") is True
    # non-admin private chat -> no
    assert service.qq_should_trigger("friend", 88888, 88888, False, "hi") is False
