"""Prompt injection of dream background + relevance-ordered memories,
and the memory retire path."""

import pytest

from muze.ai.agent import Agent
from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.digest import extract_memories
from muze.provider.base import Caps, ProtocolAdapter
from muze.store.db import open_db
from muze.store.repo import Repository


class StubAdapter(ProtocolAdapter):
    def __init__(self):
        self.caps = Caps(impl_name="stub")

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
        raise NotImplementedError

    async def send_group_message(self, group_id, segments):
        raise NotImplementedError

    async def get_history_messages(self, scene, peer_id, *, start_message_seq=None, limit=30):
        raise NotImplementedError

    async def get_message(self, scene, peer_id, message_seq):
        return None

    async def get_resource_url(self, resource_id):
        return ""

    async def get_private_file_url(self, user_id, file_id, file_hash):
        return ""

    async def get_group_file_url(self, group_id, file_id):
        return ""


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "p.db"))
    yield Repository(conn)
    await conn.close()


def make_agent(repo, cfg=None):
    cfg = cfg or MuzeConfig()
    service = ControlService(cfg, StubAdapter(), repo, ChatLog(), Runtime())
    agent = Agent(service, repo, llm=None, chatlog=ChatLog())
    service.agent = agent
    return agent, service


async def test_prompt_injects_dream_background_and_orders_memories(repo):
    await repo.upsert_digest("2026-09-28", "昨天:和张三对齐了周报格式", 24, {}, period="day")
    await repo.upsert_digest("2026-W39", "本周主线:项目A联调", 24, {}, period="week")
    await repo.upsert_digest("2026-09", "本月:求职准备中", 24, {}, period="month")
    # memories: unrelated, owner, and the very conversation in ctx
    await repo.add_memory("fact", "999", "无关联系人", source="t")
    await repo.add_memory("identity", "owner", "主人是夜猫子", source="t")
    await repo.add_memory("fact", "1000000001", "该好友在准备考研", source="t")

    agent, _ = make_agent(repo)
    ctx = ControlContext(
        source="qq", chat_key="qq:friend:1000000001",
        scene="friend", peer_id=1000000001,
    )
    prompt = await agent._system_prompt(ctx)

    assert "周报格式" in prompt
    assert "项目A联调" in prompt
    assert "求职准备中" in prompt
    assert "做梦报告背景" in prompt
    # relevance: conversation-specific memory ranks above unrelated one
    assert prompt.index("该好友在准备考研") < prompt.index("无关联系人")
    # owner memory also present
    assert "夜猫子" in prompt


async def test_prompt_injection_respects_budget(repo):
    cfg = MuzeConfig()
    cfg.digest.inject_day_chars = 50
    await repo.upsert_digest("2026-09-28", "x" * 500, 24, {}, period="day")
    agent, _ = make_agent(repo, cfg)
    ctx = ControlContext(source="qq", chat_key="k", scene="friend", peer_id=1)
    prompt = await agent._system_prompt(ctx)
    assert "x" * 50 in prompt and "x" * 80 not in prompt  # truncated to ~50
    assert "…" in prompt


async def test_prompt_injection_disabled(repo):
    cfg = MuzeConfig()
    cfg.digest.inject_context = False
    await repo.upsert_digest("2026-09-28", "不应出现", 24, {}, period="day")
    agent, _ = make_agent(repo, cfg)
    ctx = ControlContext(source="qq", chat_key="k", scene="friend", peer_id=1)
    assert "不应出现" not in await agent._system_prompt(ctx)


async def test_extract_memories_retires_contradicted(repo):
    class RetireLLM:
        configured = True

        async def chat(self, messages, tools=None):
            return {
                "content": (
                    '[{"action":"retire","id":1,"content":"旧信息已过时"},'
                    '{"action":"add","subject":"owner","kind":"fact","content":"主人已换新工作"}]'
                )
            }

    old_id = await repo.add_memory("fact", "owner", "在旧公司任职", source="dream:t")
    n = await extract_memories(
        repo, RetireLLM(), MuzeConfig(), date="2026-09-28", report="r", chatlog=ChatLog()
    )
    assert n == 1
    remaining = await repo.list_memories()
    assert len(remaining) == 1 and "新工作" in remaining[0]["content"]
    assert old_id == 1  # the retired id matched
