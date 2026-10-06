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

    async def chat(self, messages, tools=None, on_chunk=None):
        self.calls += 1
        # copy: the agent mutates its request list in place (tool results,
        # final answer) and we assert on the REQUEST as sent
        self.last_messages = [dict(m) for m in messages]
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_chunk and item.get("content"):
            on_chunk(len(item["content"]), item["content"], False)
        return item


class SlowLLM(ScriptLLM):
    """Streams slowly but steadily — used for live-progress / heartbeat tests."""

    def __init__(self, cfg, delay: float = 10.0):
        super().__init__([])
        self.cfg = cfg
        self.delay = delay

    async def chat(self, messages, tools=None, on_chunk=None):
        import time as _t

        self.calls += 1
        self.last_messages = messages
        end = _t.monotonic() + self.delay
        while _t.monotonic() < end:
            await asyncio.sleep(0.02)
            if on_chunk:
                on_chunk(3)  # chunks keep flowing: slow, NOT stuck
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
    # the final answer lives in the session loop messages (the webui session
    # view renders exactly those; without it the full output was invisible)
    sess = agent.sessions.latest(1)[0]
    assert sess.messages[-1] == {"role": "assistant", "content": "done"}
    assert sess.messages[-2]["role"] == "tool"


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
    assert d2["friends"] == [] and "now" in d2


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


async def test_converse_does_not_send_tool_call_lines(env, monkeypatch):
    """Tool-call status lines are noise: they must NOT be pushed to the chat
    front (the webui session view shows live events instead)."""
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
                ],
            },
            {"role": "assistant", "content": "done"},
        ]
    )
    llm.cfg = _llm_cfg()
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("查一下", _qq_ctx())
    assert out.text == "done"
    # tool rounds no longer push anything to the chat front (heartbeats may)
    sent = _sent_texts(service)
    assert all("🔧" not in l for l in sent)
    # tool rounds are still visible as session events (webui)
    kinds = [e[1] for e in agent.sessions.latest(1)[0].events]
    assert "tool" in kinds and "tool_result" in kinds


async def test_converse_heartbeats_show_live_output(env):
    service, repo = env
    llm = SlowLLM(_llm_cfg(progress_wait_sec=0.05, progress_interval_sec=0.05), delay=0.3)
    agent = Agent(service, repo, llm, ChatLog(), 3)
    session = agent.sessions.create("qq:friend:1", "慢点答")
    out = await agent.converse("慢点答", _qq_ctx(), session=session)
    assert out.text == "late"
    beats = [l for l in _sent_texts(service) if "⏳" in l]
    assert beats  # user was told we are alive
    # chunks were flowing -> the heartbeat reports live output size
    assert any("模型输出中" in l for l in beats)


async def test_converse_tool_stall_becomes_tool_error(env, monkeypatch):
    """A hung tool no longer aborts the whole turn: its stall cap fires as a
    TOOL-LEVEL error the model sees and recovers from (slow-but-alive model
    output was never cut and still isn't)."""
    service, repo = env

    class HungRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            await asyncio.sleep(30)

    monkeypatch.setattr("muze.ai.agent.ToolRunner", HungRunner)
    llm = ScriptLLM(
        [
            {"role": "assistant",
             "tool_calls": [{"id": "1", "function": {"name": "cli", "arguments": "{}"}}]},
            {"role": "assistant", "content": "工具挂了但我还在"},
        ]
    )
    llm.cfg = _llm_cfg(turn_timeout_sec=0.05)
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("会卡住", _qq_ctx())
    assert not out.is_error and out.text == "工具挂了但我还在"
    # the hung tool is visible as a timed-out session event
    evs = [e[2] for e in agent.sessions.latest(1)[0].events]
    assert any("卡死" in e for e in evs)


async def test_submit_is_async_and_queryable_by_sess(env, monkeypatch):
    service, repo = env

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            return '{"ok": true}'

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    llm = ScriptLLM(
        [
            {"role": "assistant",
             "tool_calls": [{"id": "1", "function": {"name": "search_archive", "arguments": '{"query":"x"}'}}]},
            {"role": "assistant", "content": "答案在此"},
        ]
    )
    llm.cfg = _llm_cfg()
    agent = Agent(service, repo, llm, ChatLog(), 3)
    service.agent = agent

    session = agent.submit("查一下", _qq_ctx())
    assert session.status == "running"
    # the session is visible immediately, even before the turn finishes
    assert agent.sessions.get(session.id) is session
    await asyncio.sleep(0.1)  # let the background turn complete

    assert session.status == "done"
    sent = _sent_texts(service)
    assert any(t.startswith(f"#{session.id}") and "答案在此" in t for t in sent)
    # /sess shows the full trace: prompt, tool call, tool result, outcome
    out = await service.run_command(f"sess {session.id}", _qq_ctx())
    assert not out.is_error
    assert session.id in out.text and "查一下" in out.text
    assert "search_archive" in out.text and '{"ok": true}' in out.text
    # listing without an id
    listing = await service.run_command("sess", _qq_ctx())
    assert session.id in listing.text


async def test_same_conversation_sessions_run_in_parallel(env):
    """Two LLM sessions in ONE conversation must not queue behind each other;
    they share the conversation history but each commits atomically."""
    service, repo = env

    class GateLLM:
        configured = True

        def __init__(self):
            self.entered = 0
            self.release = asyncio.Event()

        async def chat(self, messages, tools=None, on_chunk=None):
            self.entered += 1
            if self.entered >= 2:
                self.release.set()
            # if turns were serialized, the 2nd would never enter and this
            # would time out, failing the assertions below
            await asyncio.wait_for(self.release.wait(), 2.0)
            return {"role": "assistant", "content": f"答:{messages[-1]['content']}"}

    agent = Agent(service, repo, GateLLM(), ChatLog(), 3)
    c = ctx()
    o1, o2 = await asyncio.gather(agent.ask("甲", c), agent.ask("乙", c))

    assert agent.llm.entered == 2  # both reached the model before either finished
    assert {o1.text, o2.text} == {"答:甲", "答:乙"}
    pairs = [(m["role"], m["content"]) for m in agent._histories["cli"]]
    assert ("user", "甲") in pairs and ("user", "乙") in pairs
    assert ("assistant", "答:甲") in pairs and ("assistant", "答:乙") in pairs


async def test_failed_turn_retracts_only_its_own_entry(env):
    """A failing turn drops its own user message and never removes a
    concurrent turn's entries."""
    service, repo = env

    class BoomLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            raise RuntimeError("boom")

    agent = Agent(service, repo, BoomLLM(), ChatLog(), 3)
    c = ctx()
    agent._history("cli").append({"role": "assistant", "content": "旧答复"})

    out = await agent.ask("会失败", c)

    assert out.is_error
    hist = agent._histories["cli"]
    assert {"role": "assistant", "content": "旧答复"} in hist
    assert not any(m.get("content") == "会失败" for m in hist)


async def test_concurrent_replies_commit_in_prompt_order(env):
    """A slow turn started first must still land BEFORE a fast turn started
    later — history reads in prompt order, not completion order."""
    service, repo = env

    class OrderLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            p = messages[-1]["content"]
            await asyncio.sleep(0.1 if p == "慢" else 0.0)
            return {"role": "assistant", "content": f"答:{p}"}

    agent = Agent(service, repo, OrderLLM(), ChatLog(), 3)
    c = ctx()
    await asyncio.gather(agent.ask("慢", c), agent.ask("快", c))

    hist = [(m["role"], m["content"]) for m in agent._histories["cli"]]
    assert hist == [
        ("user", "慢"), ("assistant", "答:慢"),
        ("user", "快"), ("assistant", "答:快"),
    ]


async def test_turn_cap_refuses_when_queue_full(env):
    """Beyond concurrency + queue, a turn is refused with 忙 instead of piling up."""
    service, repo = env
    service.cfg.llm.max_concurrent_turns = 1
    service.cfg.llm.max_queued_turns = 0
    release = asyncio.Event()

    class GateLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            await release.wait()
            return {"role": "assistant", "content": "ok"}

    agent = Agent(service, repo, GateLLM(), ChatLog(), 3)
    c = ctx()
    t1 = asyncio.create_task(agent.ask("一", c))
    await asyncio.sleep(0.05)  # let t1 grab the only slot
    out2 = await agent.ask("二", c)
    assert out2.is_error and "忙" in out2.text
    release.set()
    out1 = await t1
    assert not out1.is_error and out1.text == "ok"


async def test_turn_cap_queues_extra_turns(env):
    """Under the cap extra turns wait for a slot, then run — nothing is lost."""
    service, repo = env
    service.cfg.llm.max_concurrent_turns = 1
    service.cfg.llm.max_queued_turns = 4
    gate = asyncio.Event()
    entered: list[str] = []

    class GateLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            entered.append(messages[-1]["content"])
            if len(entered) < 2:
                await gate.wait()
            return {"role": "assistant", "content": f"答:{messages[-1]['content']}"}

    agent = Agent(service, repo, GateLLM(), ChatLog(), 3)
    c = ctx()
    t1 = asyncio.create_task(agent.ask("甲", c))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(agent.ask("乙", c))
    await asyncio.sleep(0.05)
    assert entered == ["甲"]  # 乙 is queued, not running yet
    gate.set()
    o1, o2 = await asyncio.gather(t1, t2)
    assert entered == ["甲", "乙"]
    assert not o1.is_error and not o2.is_error


async def test_reset_midturn_does_not_leak_context(env):
    """A reset while a turn is in flight discards that turn's history; the next
    turn starts clean instead of inheriting the aborted context."""
    service, repo = env
    release = asyncio.Event()
    seen: list[list[str]] = []

    class GateLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            seen.append([m["content"] for m in messages if m["role"] == "user"])
            await release.wait()
            return {"role": "assistant", "content": "答"}

    agent = Agent(service, repo, GateLLM(), ChatLog(), 3)
    c = ctx()
    t = asyncio.create_task(agent.ask("旧问题", c))
    await asyncio.sleep(0.05)
    agent.reset("cli")  # wipe context mid-turn
    release.set()
    await t
    assert agent._histories.get("cli", []) == []  # reply went to the dead list

    seen.clear()
    out = await agent.ask("新问题", c)
    assert not out.is_error
    assert seen == [["新问题"]]  # no leak from the pre-reset turn


async def test_aclose_cancels_inflight_session(env):
    """Shutdown cancels background turns and retracts their unanswered prompt."""
    service, repo = env
    never = asyncio.Event()

    class BlockLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            await never.wait()
            return {"role": "assistant", "content": "不会到这里"}

    agent = Agent(service, repo, BlockLLM(), ChatLog(), 3)
    session = agent.submit("挂着", _qq_ctx())
    await asyncio.sleep(0.05)
    assert session.status == "running"

    await agent.aclose()
    await asyncio.sleep(0.05)
    assert agent._histories.get("qq:friend:1", []) == []


# ------------------------------------------------- persistent sessions
def _tool_call(n: int) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{"id": str(n), "function": {"name": "cli", "arguments": "{}"}}],
    }


async def test_step_limit_suspends_and_sess_go_resumes(env, monkeypatch):
    service, repo = env

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    llm = ScriptLLM([_tool_call(1), _tool_call(2), {"role": "assistant", "content": "完成"}])
    agent = Agent(service, repo, llm, ChatLog(), 2)  # 2 rounds per run
    service.agent = agent
    c = _qq_ctx()

    session = agent.sessions.create(c.chat_key, "多步任务")
    out = await agent.converse("多步任务", c, session=session)
    assert session.status == "suspend"
    assert "/sess go" in out.text and "::继续" not in out.text
    assert session.messages  # loop state kept for resume

    res = await service.run_command(f"sess go {session.id}", c)
    assert "已继续" in res.text
    await asyncio.sleep(0.1)
    assert session.status == "done" and session.result_preview == "完成"


async def test_sessions_survive_restart_and_resume(env, monkeypatch):
    service, repo = env

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    a1 = Agent(service, repo, ScriptLLM([_tool_call(1)]), ChatLog(), 1)
    s = a1.sessions.create("qq:friend:1", "持久任务")
    await a1.converse("持久任务", _qq_ctx(), session=s)
    assert s.status == "suspend"
    sid = s.id

    # "restart": a fresh agent restores sessions from the DB
    a2 = Agent(service, repo, ScriptLLM([{"role": "assistant", "content": "续上"}]), ChatLog(), 1)
    await a2.load_sessions()
    restored = a2.sessions.get(sid)
    assert restored is not None and restored.status == "suspend"
    assert restored.messages  # saved loop state survived the restart

    a2.resume(restored, _qq_ctx())
    await asyncio.sleep(0.1)
    assert restored.status == "done" and restored.result_preview == "续上"


async def test_sess_id_message_continues_done_session(env):
    service, repo = env
    seen: list[list[str]] = []

    class RecLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            seen.append([m["content"] for m in messages if m["role"] == "user"])
            return {"role": "assistant", "content": f"答{len(seen)}"}

    agent = Agent(service, repo, RecLLM(), ChatLog(), 4)
    service.agent = agent
    c = _qq_ctx()
    session = agent.sessions.create(c.chat_key, "第一个问题")
    await agent.converse("第一个问题", c, session=session)
    assert session.status == "done"

    res = await service.run_command(f"sess {session.id} 第二个问题", c)
    assert "已继续" in res.text
    await asyncio.sleep(0.1)
    assert session.status == "done"
    assert seen[-1] == ["第一个问题", "第二个问题"]  # context carried over


async def test_sess_list_shows_title_and_detail(env):
    service, repo = env
    agent = Agent(service, repo, ScriptLLM([]), ChatLog(), 4)
    service.agent = agent
    s = agent.sessions.create("qq:friend:1", "帮我总结一下昨天的群聊记录")
    c = _qq_ctx()

    listing = await service.run_command("sess list", c)
    assert s.id in listing.text and "帮我总结" in listing.text
    detail = await service.run_command(f"sess {s.id}", c)
    assert "标题:" in detail.text and "帮我总结" in detail.text


def test_default_max_steps_and_heuristic_title():
    from muze.ai.sessions import heuristic_title
    from muze.config import LLMConfig

    assert LLMConfig().agent_max_steps == 48
    assert heuristic_title("第一行\n第二行") == "第一行"
    assert heuristic_title("   ") == "(无标题)"


# ------------------------------------------------------- new tool surface
async def test_tool_results_carry_current_time(env):
    import json

    from muze.ai.tools import ToolRunner

    service, repo = env
    runner = ToolRunner(service, repo, service.adapter, None, ctx())
    r = json.loads(await runner.run("directory", {"kind": "conversations"}))
    assert "now" in r and r["now"][4] == "-"  # YYYY-MM-DD ...
    # errors carry it too
    r2 = json.loads(await runner.run("directory", {"kind": "bogus"}))
    assert "now" in r2


async def test_subagent_tool_runs_readonly_child_loop(env):
    import json

    service, repo = env
    # script order: parent r1 (subagent call) -> child answer -> parent r2
    llm = ScriptLLM(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "1", "function": {"name": "subagent",
                                             "arguments": '{"prompt":"查上周群聊里关于X的讨论并汇总"}'}},
                ],
            },
            {"role": "assistant", "content": "子报告：X 被讨论了 3 次"},
            {"role": "assistant", "content": "综合子报告：结论"},
        ]
    )
    agent = Agent(service, repo, llm, ChatLog(), 6)
    service.agent = agent
    out = await agent.converse("帮我调研X", _qq_ctx())
    assert out.text == "综合子报告：结论"
    kids = [s for s in agent.sessions.latest(10) if s.title.startswith("子任务")]
    assert kids and kids[0].status == "done"
    assert kids[0].prompt.startswith("查上周群聊")
    # the child tool payload came back to the parent as the tool result
    tr = [m for m in agent.sessions.latest(10)[0].messages if m.get("role") == "tool"]
    # first session = the parent (latest sorted by updated; assert loosely)


async def test_subagent_depth_is_capped(env):
    import json

    from muze.ai.agent import MAX_SUB_DEPTH
    from muze.ai.tools import ToolRunner

    service, repo = env
    agent = Agent(service, repo, ScriptLLM([{"role": "assistant", "content": "孙辈完成"}]), ChatLog(), 3)  # noqa: E501
    service.agent = agent
    c = ctx()
    c.sub_depth = MAX_SUB_DEPTH  # last allowed level still runs
    runner = ToolRunner(service, repo, service.adapter, None, c)
    r = json.loads(await runner.run("subagent", {"prompt": "合法层级"}))
    assert "report" in r and "孙辈完成" in r["report"]

    c2 = ctx()
    c2.sub_depth = MAX_SUB_DEPTH + 1  # past the cap: clear error, no child
    runner2 = ToolRunner(service, repo, service.adapter, None, c2)
    r2 = json.loads(await runner2.run("subagent", {"prompt": "超限层级"}))
    assert "error" in r2 and "上限" in r2["error"]


async def test_subagent_spec_excludes_write_tools():
    from muze.ai.tools import SPEC, SUBAGENT_SPEC

    names = {s["function"]["name"] for s in SUBAGENT_SPEC}
    full = {s["function"]["name"] for s in SPEC}
    assert names <= full
    for banned in ("send_message", "send_media", "schedule_task",
                   "remember", "profile_update", "make_chart"):
        assert banned not in names
    assert "search_archive" in names and "read_document" in names
    assert "subagent" in names  # nesting is allowed, capped at runtime


async def test_subagent_nests_three_levels(env):
    """parent -> child -> grandchild all complete; depth cap enforced one
    level further (see test_subagent_depth_is_capped)."""
    llm = ScriptLLM(
        [
            {"role": "assistant",  # parent r1: spawn child
             "tool_calls": [{"id": "1", "function": {"name": "subagent",
                                                     "arguments": '{"prompt":"调研X"}'}}]},
            {"role": "assistant",  # child r1: spawn grandchild
             "tool_calls": [{"id": "2", "function": {"name": "subagent",
                                                     "arguments": '{"prompt":"查细节"}'}}]},
            {"role": "assistant", "content": "孙辈：细节查完"},  # grandchild final
            {"role": "assistant", "content": "子辈汇总"},  # child final
            {"role": "assistant", "content": "父辈结论"},  # parent final
        ]
    )
    service, repo = env
    agent = Agent(service, repo, llm, ChatLog(), 8)
    service.agent = agent
    out = await agent.converse("调研X", _qq_ctx())
    assert out.text == "父辈结论"
    kids = [s for s in agent.sessions.latest(10) if s.title.startswith("子任务")]
    assert len(kids) == 2  # child + grandchild
    assert all(k.status == "done" for k in kids)


async def test_subagent_watchdog_progress_beats_stall(env, monkeypatch):
    """No total-time cap: a sub-agent slower than the stall window survives
    as long as its session shows life; a silent one is killed within ~stall."""
    service, repo = env

    class SlowQuietRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            if name == "subagent":
                await asyncio.sleep(0.2)  # way over stall, no bumps
                return '{"report": "不应到达"}'
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", SlowQuietRunner)
    llm = ScriptLLM(
        [
            {"role": "assistant",
             "tool_calls": [{"id": "1", "function": {"name": "subagent",
                                                     "arguments": "{}"}}]},
            {"role": "assistant", "content": "超时后继续"},
        ]
    )
    llm.cfg = _llm_cfg(turn_timeout_sec=0.05)
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("派个哑崽", _qq_ctx())
    assert out.text == "超时后继续"
    evs = [e[2] for e in agent.sessions.latest(1)[0].events]
    assert any("无任何进展" in e for e in evs)

    # same long run, but a live child session keeps bumping -> survives
    from muze.ai.sessions import Session

    class SlowAliveRunner(SlowQuietRunner):
        def __init__(self, *a, **k):
            super().__init__()

        async def run(self, name, args):
            if name == "subagent":
                fake = Session(id="beat", chat_key="x", prompt="p")
                agent2._live_children[id(fake)] = fake

                async def _beat():
                    for _ in range(10):
                        await asyncio.sleep(0.03)
                        fake.bump(1)

                bt = asyncio.create_task(_beat())
                try:
                    await asyncio.sleep(0.2)  # >> stall, but alive
                    return '{"report": "活着跑完了"}'
                finally:
                    bt.cancel()
                    agent2._live_children.pop(id(fake), None)
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", SlowAliveRunner)
    llm2 = ScriptLLM(
        [
            {"role": "assistant",
             "tool_calls": [{"id": "1", "function": {"name": "subagent",
                                                     "arguments": "{}"}}]},
            {"role": "assistant", "content": "活着完成"},
        ]
    )
    llm2.cfg = _llm_cfg(turn_timeout_sec=0.05)
    agent2 = Agent(service, repo, llm2, ChatLog(), 3)
    out2 = await agent2.converse("派个活崽", _qq_ctx())
    assert out2.text == "活着完成"
    evs2 = [e[2] for e in agent2.sessions.latest(1)[0].events]
    assert any("活着跑完了" in e for e in evs2)


async def test_loop_messages_visible_while_running(env, monkeypatch):
    """The webui session view polls /api/sess/<id>: tool calls and results
    must already be in session.messages DURING the round, not only at end."""
    service, repo = env
    gate = asyncio.Event()
    seen: dict = {}

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            await gate.wait()  # hold the tool round open
            return '{"ok": 1}'

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    import muze.ai.agent as agent_mod

    orig = agent_mod.Agent._drive

    async def drive(self, session, ctx, messages, start, commit, rollback, **kw):
        task = asyncio.create_task(
            orig(self, session, ctx, messages, start, commit, rollback, **kw)
        )
        await asyncio.sleep(0.15)  # loop reached the pending tool call
        seen["roles"] = [m.get("role") for m in session.messages]
        gate.set()
        return await task

    monkeypatch.setattr(agent_mod.Agent, "_drive", drive)
    llm = ScriptLLM(
        [
            {"role": "assistant",
             "tool_calls": [{"id": "1", "function": {"name": "cli", "arguments": "{}"}}]},
            {"role": "assistant", "content": "done"},
        ]
    )
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("查", ctx())
    assert out.text == "done"
    # the assistant tool-call message was visible mid-round
    assert "assistant" in seen["roles"]


async def test_reasoning_kept_on_message_and_stripped_from_requests():
    """Thinking models stream reasoning_content: it must survive onto the
    message (webui 思考过程) but never be echoed back to the API."""
    from types import SimpleNamespace

    from muze.ai.llm import LLMClient
    from muze.config import LLMConfig

    llm = LLMClient(LLMConfig())

    def chunk(content=None, reasoning=None):
        d = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(delta=d, finish_reason=None)], usage=None)

    async def fake_create(**kwargs):
        sent = kwargs["messages"]
        # the sanitize step must have stripped provider extras from history
        for m in sent:
            assert "reasoning_content" not in m and "finish_reason" not in m
        async def gen():
            yield chunk(reasoning="先想一想…")
            yield chunk(reasoning="再想一点")
            yield chunk(content="答案")
        return gen()

    class FakeCompletions:
        async def create(self, **kwargs):
            return await fake_create(**kwargs)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    def fake_client():
        return FakeClient()

    monkeypatch_client = pytest.MonkeyPatch()
    monkeypatch_client.setattr(llm, "_chat_client", fake_client)
    try:
        chunks: list[int] = []
        msg = await llm.chat(
            [
                {"role": "user", "content": "问"},
                # a history entry carrying provider extras (as persisted)
                {"role": "assistant", "content": "旧答", "reasoning_content": "旧思考",
                 "finish_reason": "stop"},
            ],
            on_chunk=lambda n, piece="", reasoning=False: chunks.append(n),
        )
    finally:
        monkeypatch_client.undo()
    assert msg["content"] == "答案"
    assert msg["reasoning_content"] == "先想一想…再想一点"
    assert sum(chunks) > 0  # reasoning also fed the live progress counter



async def test_sess_stop_cancels_running_session(env):
    service, repo = env
    never = asyncio.Event()

    class BlockLLM:
        configured = True

        async def chat(self, messages, tools=None, on_chunk=None):
            await never.wait()
            return {"role": "assistant", "content": "x"}

    agent = Agent(service, repo, BlockLLM(), ChatLog(), 3)
    service.agent = agent
    session = agent.submit("跑不完", _qq_ctx())
    await asyncio.sleep(0.05)
    assert session.status == "running"

    res = await service.run_command(f"sess stop {session.id}", _qq_ctx())
    assert "已取消" in res.text
    await asyncio.sleep(0.05)
    assert session.status == "error" and "取消" in session.result_preview
    # a finished/absent session cannot be stopped
    res2 = await service.run_command("sess stop deadbeef", _qq_ctx())
    assert res2.is_error


async def test_sess_del_removes_session(env):
    service, repo = env
    agent = Agent(service, repo, ScriptLLM([]), ChatLog(), 3)
    service.agent = agent
    s = agent.sessions.create("qq:friend:1", "待删除")
    s.finish(True, "做完了")
    await agent._persist(s)

    res = await service.run_command(f"sess del {s.id}", _qq_ctx())
    assert "已删除" in res.text
    assert agent.sessions.get(s.id) is None
    assert await repo.get_ai_session(s.id) is None


async def test_describe_image_face_conditional_and_cache(env, tmp_path):
    import json

    from muze.ai.tools import ToolRunner

    service, repo = env
    calls: list[str] = []

    class FakeVision:
        configured = True

        async def vision_describe(self, path, question):
            calls.append(path)
            return "图里是报价表"

    async def add_media(seq, kind="image", summary=None):
        return await repo.add_media(
            {
                "scene": "friend", "peer_id": 9, "message_seq": seq, "seg_index": 0,
                "kind": kind, "file_path": str(tmp_path / f"m{seq}.png"),
                "file_name": f"m{seq}.png", "width": 800, "height": 600,
                "file_size": 90000, "status": "done", "summary": summary,
                "resource_id": None, "file_id": None, "file_hash": None,
                "duration": None,
            }
        )

    await add_media(1, kind="face")
    await add_media(2, summary="旧识别：发票金额200元")
    await add_media(3)

    runner = ToolRunner(service, repo, service.adapter, FakeVision(), ctx())
    # faces are skipped unless the purpose is about faces or force=true
    r = json.loads(await runner.run(
        "describe_image", {"scene": "friend", "peer_id": 9, "message_seq": 1,
                           "purpose": "看金额"},
    ))
    assert "跳过" in r["descriptions"][0] and calls == []
    r = json.loads(await runner.run(
        "describe_image", {"scene": "friend", "peer_id": 9, "message_seq": 1,
                           "purpose": "这个人常用的表情包风格", "force": True},
    ))
    assert calls == [str(tmp_path / "m1.png")]  # force recognizes it

    # cached summary reused without a vision call; unknown image described + cached
    calls.clear()
    r = json.loads(await runner.run(
        "describe_image", {"scene": "friend", "peer_id": 9, "message_seq": 2,
                           "purpose": "金额"},
    ))
    assert calls == [] and "复用" in r["descriptions"][0]
    r = json.loads(await runner.run(
        "describe_image", {"scene": "friend", "peer_id": 9, "message_seq": 3,
                           "purpose": "看金额"},
    ))
    assert r["descriptions"] == ["图里是报价表"]
    assert calls == [str(tmp_path / "m3.png")]
    # second look at seq 3 reuses the cached description
    calls.clear()
    r = json.loads(await runner.run(
        "describe_image", {"scene": "friend", "peer_id": 9, "message_seq": 3,
                           "purpose": "再确认"},
    ))
    assert calls == [] and "复用" in r["descriptions"][0]


async def test_agent_feeds_live_tail_without_qq_front(env, monkeypatch):
    """webui-initiated turns have NO QQ-reachable ctx (no heartbeat path) —
    the streamed text must STILL land in session.live for the webui."""
    service, repo = env

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, name, args):
            return "{}"

    monkeypatch.setattr("muze.ai.agent.ToolRunner", FakeRunner)
    llm = ScriptLLM(
        [
            {"role": "assistant",
             "tool_calls": [{"id": "1", "function": {"name": "cli", "arguments": "{}"}}]},
            {"role": "assistant", "content": "最终答案在此"},
        ]
    )
    llm.cfg = _llm_cfg()
    agent = Agent(service, repo, llm, ChatLog(), 3)
    out = await agent.converse("查", _qq_ctx())
    assert out.text == "最终答案在此"
    s = agent.sessions.latest(1)[0]
    assert s.live.endswith("最终答案在此")  # streamed tail visible
    # the FINAL round's assistant message is kept too (think view coverage)
    assert s.messages[-1].get("content") == "最终答案在此"
