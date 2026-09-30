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
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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
