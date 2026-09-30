"""Agent loop: result compaction, transient retry, parallel tools, time ctx."""

import asyncio

import pytest

from muze.ai.agent import Agent, _compact, _brief
from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext
from muze.store.db import open_db
from muze.store.repo import Repository

from test_permissions import Recorder


class ScriptLLM:
    configured = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.last_messages = messages
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SlowLLM(ScriptLLM):
    """Never answers in time — used for heartbeat / timeout tests."""

    def __init__(self, cfg, delay: float = 10.0):
        super().__init__([])
        self.cfg = cfg
        self.delay = delay

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.last_messages = messages
        await asyncio.sleep(self.delay)
        return {"role": "assistant", "content": "late"}


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "ag.db"))
    repo = Repository(conn)
    from muze.core.service import ControlService, Runtime

    cfg = MuzeConfig()
    service = ControlService(cfg, Recorder(), repo, ChatLog(), Runtime())
    yield service, repo
    await conn.close()


def ctx():
    return ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)


def test_compact_and_brief():
    assert _compact("hello") == "hello"
    big = "x" * 10000
    out = _compact(big)
    assert len(out) < 10000 and "截断" in out
    assert len(_brief({"a": "y" * 999})) <= 160


async def test_system_prompt_injects_current_time(env):
    service, repo = env
    agent = Agent(service, repo, ScriptLLM([]), ChatLog(), 3)
    text = await agent._system_prompt(ctx())
    assert "当前时间:" in text and "今天" in text and "昨天" in text


async def test_chat_retries_transient_failures(env, monkeypatch):
    service, repo = env

    async def no_sleep(_s):
        return None

    monkeypatch.setattr("muze.ai.agent.asyncio.sleep", no_sleep)
    llm = ScriptLLM([RuntimeError("429"), RuntimeError("timeout"), {"content": "ok"}])
    agent = Agent(service, repo, llm, ChatLog(), 3)
    msg = await agent._chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok" and llm.calls == 3


async def test_converse_runs_tool_calls_in_parallel(env, monkeypatch):
    service, repo = env
    ran: list[str] = []

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            ran.append(name)
            await asyncio.sleep(0)
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    llm = ScriptLLM(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "1", "function": {"name": "get_recent_messages", "arguments": "{}"}},
                    {"id": "2", "function": {"name": "describe_image", "arguments": "{}"}},
                ],
            },
            {"role": "assistant", "content": "done"},
        ]
    )
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("do it", ctx())
    assert out.text == "done"
    assert ran == ["get_recent_messages", "describe_image"]


async def test_converse_uses_all_steps_then_notes(env, monkeypatch):
    service, repo = env

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    # every round asks for a tool -> loop exhausts max_steps
    tool_only = {
        "role": "assistant",
        "tool_calls": [{"id": "x", "function": {"name": "cli", "arguments": "{}"}}],
    }
    llm = ScriptLLM([dict(tool_only) for _ in range(3)])
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("loop", ctx())
    assert "步数上限" in out.text


async def test_converse_strips_control_prefix(env):
    service, repo = env
    llm = ScriptLLM([{"role": "assistant", "content": "hi"}])
    agent = Agent(service, repo, llm, ChatLog(), 3)
    await agent.converse("::你好", ctx())
    users = [m for m in llm.last_messages if m["role"] == "user"]
    assert users and users[-1]["content"] == "你好"
    assert not any(str(m.get("content", "")).startswith("::") for m in llm.last_messages)
    # full-width colons too
    llm.script = [{"role": "assistant", "content": "ok"}]
    await agent.converse("：：再问一次", ctx())
    assert llm.last_messages[-1]["content"] == "再问一次"


async def test_structured_query_tools(env):
    import json

    from muze.ai.tools import ToolRunner
    from muze.provider.milky.models import IncomingMessage

    service, repo = env
    m = IncomingMessage.from_raw(
        {
            "message_scene": "friend",
            "peer_id": 100,
            "message_seq": 1,
            "sender_id": 7,
            "time": 1758000000,
            "segments": [{"type": "text", "data": {"text": "报价单已发"}}],
        }
    )
    await repo.upsert_message(m, "报价单已发", "event")
    runner = ToolRunner(service, repo, service.adapter, None, ctx())

    r = json.loads(await runner.run("search_archive", {"query": "报价"}))
    assert r["count"] == 1 and r["matches"][0]["seq"] == 1

    a = json.loads(await runner.run("activity", {"mode": "top", "days": 0}))
    assert a["conversations"][0]["conv"] == "friend/100" and a["conversations"][0]["n"] == 1

    d = json.loads(await runner.run("directory", {"kind": "conversations"}))
    assert any(c["conv"] == "friend/100" for c in d["conversations"])

    d2 = json.loads(await runner.run("directory", {"kind": "friends"}))
    assert d2 == {"friends": []}


# ------------------------------------------------- progress + turn timeout
def _llm_cfg(**kw):
    from muze.config import LLMConfig

    cfg = LLMConfig()
    cfg.progress = kw.get("progress", True)
    cfg.progress_wait_sec = kw.get("progress_wait_sec", 0.01)
    cfg.progress_interval_sec = kw.get("progress_interval_sec", 0.01)
    cfg.turn_timeout_sec = kw.get("turn_timeout_sec", 30.0)
    return cfg


def _qq_ctx():
    return ControlContext(source="qq", chat_key="qq:friend:1", scene="friend", peer_id=1)


def _sent_texts(service):
    return [
        seg["data"]["text"]
        for (_scene, _peer, segs) in service.adapter.sent
        for seg in segs
        if seg.get("type") == "text"
    ]


async def test_converse_sends_tool_call_lines(env, monkeypatch):
    service, repo = env

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    llm = ScriptLLM(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "1", "function": {"name": "search_archive", "arguments": "{}"}},
                    {"id": "2", "function": {"name": "search_archive", "arguments": "{}"}},
                ],
            },
            {"role": "assistant", "content": "done"},
        ]
    )
    llm.cfg = _llm_cfg()
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("查一下", _qq_ctx())
    assert out.text == "done"
    lines = _sent_texts(service)
    # one compact line for the round, duplicate tool names collapsed
    assert any(l.startswith("🔧") and l.count("search_archive") == 1 for l in lines)
    # tool lines never enter the conversation history
    assert not any(m.get("role") == "assistant" and str(m.get("content", "")).startswith("🔧")
                   for m in llm.last_messages)


async def test_converse_heartbeats_while_llm_hangs(env):
    service, repo = env
    llm = SlowLLM(_llm_cfg(progress_wait_sec=0.05, progress_interval_sec=0.05), delay=0.3)
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("慢点答", _qq_ctx())
    assert out.text == "late"
    lines = _sent_texts(service)
    beats = [l for l in lines if l.startswith("⏳")]
    assert any("思考中" in l for l in beats)
    assert any("仍在等待" in l for l in beats)


async def test_converse_turn_timeout_frees_conversation(env):
    service, repo = env
    llm = SlowLLM(_llm_cfg(turn_timeout_sec=0.15), delay=30.0)
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("卡住", _qq_ctx())
    assert out.is_error and "超时" in out.text
    # the aborted turn must not poison the history
    assert agent._histories["qq:friend:1"] == []
