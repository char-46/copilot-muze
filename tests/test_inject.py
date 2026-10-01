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


class FakeLLM:
    """Minimal llm seam: pops scripted assistant replies."""

    configured = True

    class _Cfg:
        progress = False
        auto_title = False
        turn_timeout_sec = 0
        max_concurrent_turns = 4
        max_queued_turns = 8

    def __init__(self, replies):
        self.replies = list(replies)
        self.cfg = self._Cfg()

    async def chat(self, messages, tools=None, on_chunk=None):
        return {"role": "assistant", "content": self.replies.pop(0)}


async def _drain(agent):
    import asyncio

    tasks = [t for t in agent._tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks)


async def test_resume_persists_terminal_state(repo):
    """Regression: _run_resume's happy path never persisted the terminal
    state, so a resumed session's DB row stayed "running" forever and every
    restart resurrected the finished turn as crash debris and re-ran it."""
    import asyncio

    service = ControlService(MuzeConfig(), StubAdapter(), repo, ChatLog(), Runtime())
    agent = Agent(service, repo, llm=FakeLLM(["第一轮回答", "续跑回答"]), chatlog=ChatLog())
    service.agent = agent
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)

    await agent.ask("原问题", ctx)
    sid = next(iter(agent.sessions._sessions))
    assert (await repo.get_ai_session(sid))["status"] == "done"

    s = agent.sessions.get(sid)
    agent.resume(s, ctx, extra_text="继续")
    await _drain(agent)

    row = await repo.get_ai_session(sid)
    assert row["status"] == "done"  # was stuck "running" before the fix
    assert "续跑回答" in row["result_preview"]


async def test_resume_interrupted_ignores_post_boot_sessions(repo):
    """The 20s-delayed startup sweep must not pick up live turns that were
    created AFTER boot — only rows last touched before the process started
    are crash debris."""
    import time

    from muze.ai.sessions import RUNNING, Session

    service = ControlService(MuzeConfig(), StubAdapter(), repo, ChatLog(), Runtime())
    agent = Agent(service, repo, llm=FakeLLM(["僵尸复活回答"]), chatlog=ChatLog())
    service.agent = agent

    t0 = time.time()
    zombie = Session(id="zz01", chat_key="cli", prompt="旧问题")
    zombie.updated = t0 - 3600  # pre-boot crash debris
    agent.sessions.add(zombie)
    live = Session(id="live9", chat_key="cli", prompt="刚启动的新回合")
    live.updated = t0 + 10  # strictly after boot (clock ticks can collide)
    agent.sessions.add(live)  # created post-boot, legitimately RUNNING
    agent._boot = t0

    n = await agent.resume_interrupted()
    assert n == 1
    await _drain(agent)

    assert agent.sessions.get("zz01").status == "done"  # resumed + finalized
    fresh = agent.sessions.get("live9")
    assert fresh.status == RUNNING  # untouched: no double-run
    assert not any(k == "interrupted" for _, k, _ in fresh.events)


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


# ------------------------------------------------- restart auto-resume
async def test_ctx_rebuild_from_chat_key():
    from muze.ai.agent import Agent

    c = Agent._ctx_for_chat_key("qq:friend:1000000001")
    assert c.source == "qq" and c.scene == "friend" and c.peer_id == 1000000001
    assert c.qq_reachable and not c.is_local
    c = Agent._ctx_for_chat_key("qq:group:456")
    assert c.scene == "group" and c.peer_id == 456
    c = Agent._ctx_for_chat_key("web:default")
    assert c.is_local and not c.qq_reachable


async def test_resume_interrupted_picks_crash_and_recent_error(repo):
    import time as _t

    from muze.ai.sessions import Session

    agent, _ = make_agent(repo)

    class LLM:
        configured = True

    agent.llm = LLM
    calls = []
    agent.resume = lambda s, ctx: calls.append(
        (s.id, ctx.source, ctx.scene, ctx.peer_id)
    )

    def mk(sid, status, updated):
        s = Session(id=sid, chat_key="qq:friend:123", prompt="p")
        s.status = status
        s.updated = updated
        return s

    now = _t.time()
    agent._boot = _t.time() + 10_000  # everything below counts as pre-boot debris
    agent.sessions.add(mk("run1", "running", now))          # crash-interrupted
    agent.sessions.add(mk("err1", "error", now - 60))       # recent error
    agent.sessions.add(mk("old1", "error", now - 99_999))   # stale error
    agent.sessions.add(mk("sus1", "suspend", now))          # manual only
    agent.sessions.add(mk("ok1", "done", now))

    n = await agent.resume_interrupted()
    assert n == 2
    ids = [c[0] for c in calls]
    assert "run1" in ids and "err1" in ids
    assert "old1" not in ids and "sus1" not in ids and "ok1" not in ids
    # crash-interrupted sessions flip to suspend so resume() accepts them
    assert agent.sessions.get("run1").status == "suspend"
    # the rebuilt ctx targets the original conversation
    for c in calls:
        assert c[1] == "qq" and c[2] == "friend" and c[3] == 123

    # no LLM configured -> nothing happens
    agent.llm = None
    assert await agent.resume_interrupted() == 0


# --------------------------------------- resume message-list sanitization
async def test_resume_rebuilds_empty_message_list(repo):
    """A turn that crashed during its FIRST model call persisted an empty
    message list; resuming must rebuild [system, user(prompt)] instead of
    sending a system-only list (API 400 'messages 参数非法')."""
    from muze.ai.sessions import Session

    agent, _ = make_agent(repo)
    s = Session(id="zz1", chat_key="k", prompt="天阳的xp是什么")
    s.status = "suspend"
    s.messages = []
    agent.sessions.add(s)

    captured = {}

    async def fake_chat(messages, ctx, session):
        captured["messages"] = [dict(m) for m in messages]
        return {"content": "答了"}

    agent._chat = fake_chat
    ctx = ControlContext(source="cli", chat_key="k", scene="", peer_id=0)
    outcome = await agent._resume_inner(s, ctx, None)
    assert not outcome.is_error
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user"]
    assert captured["messages"][1]["content"] == "天阳的xp是什么"
    assert s.status == "done"  # completed after the rebuild


async def test_resume_answers_dangling_tool_calls(repo):
    """A persisted list ending on an assistant tool_calls message must get
    synthetic tool results before re-sending (API requires every call
    answered)."""
    from muze.ai.sessions import Session

    agent, _ = make_agent(repo)
    s = Session(id="zz2", chat_key="k", prompt="查一下")
    s.status = "suspend"
    s.messages = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "search", "arguments": "{}"}},
            {"id": "t2", "function": {"name": "profile_get", "arguments": "{}"}},
        ]},
    ]
    agent.sessions.add(s)

    captured = {}

    async def fake_chat(messages, ctx, session):
        captured["messages"] = [dict(m) for m in messages]
        return {"content": "查完了"}

    agent._chat = fake_chat
    ctx = ControlContext(source="cli", chat_key="k", scene="", peer_id=0)
    outcome = await agent._resume_inner(s, ctx, None)
    assert not outcome.is_error
    msgs = captured["messages"]
    tail = msgs[-2:]
    assert [m["role"] for m in tail] == ["tool", "tool"]
    assert {m["tool_call_id"] for m in tail} == {"t1", "t2"}
    assert all("重启" in m["content"] for m in tail)
