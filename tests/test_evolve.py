"""Self-evolution pass + capability wishlist + prompt inventory pinning."""

import pytest

from muze.ai.agent import SYSTEM_PROMPT
from muze.changelog import RELEASE_NOTES, recent_notes, validate_notes
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.ai.tools import ToolRunner, WRITE_TOOLS
from muze.chatlog import ChatLog
from muze.evolve import EVOLVE_PROMPT, evolve_if_due
from muze.store.db import open_db
from muze.store.repo import Repository
from muze.version import BASE_VERSION


class EvolveLLM:
    configured = True

    def __init__(self, out: str = "[]"):
        self.out = out
        self.prompts: list[str] = []

    async def chat(self, messages, tools=None):
        self.prompts.append(messages[-1]["content"])
        return {"role": "assistant", "content": self.out}


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "ev.db"))
    yield Repository(conn)
    await conn.close()


# ------------------------------------------------------------- changelog
def test_release_notes_newest_first_and_rendered():
    assert RELEASE_NOTES[0]["version"] == BASE_VERSION  # every release appends
    txt = recent_notes(2)
    assert RELEASE_NOTES[0]["version"] in txt and "changes" not in txt
    assert validate_notes() == []  # shape guard for hand-edited entries


# ---------------------------------------------------------------- evolve
async def test_evolve_retires_stale_and_adds_habit(repo):
    stale = await repo.add_memory("fact", "owner", "待办关闭用数据库id，如 /done 12")
    await repo.add_memory("relation", "6080790", "吴与同 是同学")  # unrelated, must survive
    llm = EvolveLLM(
        '[{"action":"retire","id":%d,"content":"被 a.148 替换：序号写法"},'
        '{"action":"add","subject":"muze","kind":"habit","content":"待办引用一律用 /todo 的稠密序号"}]'
        % stale
    )
    changed = await evolve_if_due(repo, llm, MuzeConfig(), ChatLog())
    assert changed == 2
    # the prompt carried the delta notes + existing memories with ids
    p = llm.prompts[0]
    assert BASE_VERSION in p and str(stale) in p and "吴与同" in p
    # old habit retired, relation untouched, new habit stored under muze
    assert await repo.retire_memory(stale) is False  # already inactive
    mems = {m["subject"]: m for m in await repo.list_memories(limit=20)}
    assert "6080790" in mems
    habits = [m for m in await repo.list_memories(limit=20) if m["subject"] == "muze"]
    assert habits and habits[0]["kind"] == "habit" and "稠密序号" in habits[0]["content"]
    # version recorded -> due check short-circuits next boot
    assert await repo.get_meta("evolved_version") == BASE_VERSION
    assert await evolve_if_due(repo, EvolveLLM(), MuzeConfig(), ChatLog()) == 0


async def test_evolve_llm_failure_leaves_version_unset(repo):
    class Boom(EvolveLLM):
        async def chat(self, messages, tools=None):
            raise RuntimeError("api down")

    assert await evolve_if_due(repo, Boom(), MuzeConfig(), ChatLog()) == 0
    assert await repo.get_meta("evolved_version") is None  # retried next boot


async def test_evolve_garbage_is_safe(repo):
    assert await evolve_if_due(repo, EvolveLLM("我不输出 JSON"), MuzeConfig(), ChatLog()) == 0
    assert await repo.get_meta("evolved_version") is None


def test_evolve_prompt_teaches_scope():
    assert "无关的记忆" in EVOLVE_PROMPT and "habit" in EVOLVE_PROMPT


# ---------------------------------------------------- prompt inventory
def test_prompt_registry_pins_full_inventory():
    import muze.prompts as P

    # the 2026-10-28 audit found the registry incomplete at birth; this
    # count is the full known inventory -- update it ONLY deliberately
    assert len(P.__all__) == 10
    assert P.PROFILE_PROMPT.startswith("你在维护")
    assert "标题" in P.AUTO_TITLE_PROMPT


def test_hot_path_has_no_changelog_and_teaches_wishes():
    # update cost must NOT ride along every call; wishes reporting must
    from muze.ai.agent import Agent

    src = Agent._system_prompt.__doc__ or ""
    assert "系统近期变更" not in SYSTEM_PROMPT  # not in the static prompt
    assert "capability_wish" in SYSTEM_PROMPT  # self-reporting is taught


# ------------------------------------------------------- wishes command
@pytest.fixture
async def svc(tmp_path):
    conn = await open_db(str(tmp_path / "w.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)

    class NoAdapter:
        pass

    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    yield service, repo
    await conn.close()


async def test_wishes_command_list_and_close(svc):
    service, repo = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    assert await repo.add_wish("本地音频剪辑", "把一段语音截半段再发", "让用户自己录") == 1
    assert await repo.add_wish("本地音频剪辑", "x", "y") is None  # dedupe
    assert await repo.add_wish("网页转PDF", "存档公众号文章", "fetch_url 后贴纯文本") == 2

    out = await service.run_command("wishes", ctx)
    assert not out.is_error and "#1" in out.text and "#2" in out.text
    assert "本地音频剪辑" in out.text and "现绕法" in out.text

    out = await service.run_command("wishes close 1", ctx)
    assert "已关闭" in out.text
    # dense numbering: the survivor renumbers to #1 (closed ones leave holes nowhere)
    out = await service.run_command("wishes", ctx)
    assert "#1" in out.text and "网页转PDF" in out.text and "本地音频剪辑" not in out.text
    out = await service.run_command("wishes close 9", ctx)
    assert out.is_error


async def test_capability_wish_tool_gated_and_files(svc):
    import json

    service, repo = svc
    outside = ControlContext(source="qq", chat_key="qq:group/999", scene="group", peer_id=999)
    runner = ToolRunner(service, repo, service.adapter, None, outside)
    out = json.loads(await runner.run(
        "capability_wish", {"title": "本地音频剪辑", "context": "截半段语音", "workaround": "无"}
    ))
    assert out.get("needs_approval") is True  # WRITE-gated outside control
    assert await repo.list_wishes() == []

    inside = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    runner = ToolRunner(service, repo, service.adapter, None, inside)
    out = json.loads(await runner.run(
        "capability_wish", {"title": "本地音频剪辑", "context": "截半段语音", "workaround": "无"}
    ))
    assert out.get("added") is True and "capability_wish" in WRITE_TOOLS
    rows = await repo.list_wishes()
    assert len(rows) == 1 and rows[0]["title"] == "本地音频剪辑"
    assert rows[0]["context"] == "截半段语音"


async def test_evolve_command_uses_agent_llm(svc):
    """/evolve must not assume ControlService owns an llm (live error
    2026-10-28: 'ControlService' object has no attribute 'llm')."""
    service, repo = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    stale = await repo.add_memory("fact", "muze", "待办用数据库id 关闭")

    class AgentStub:
        def __init__(self, llm):
            self.llm = llm

    service.agent = AgentStub(EvolveLLM(f'[{{"action":"retire","id":{stale},"content":"旧写法"}}]'))
    out = await service.run_command("evolve", ctx)
    assert not out.is_error and "习惯已同步" in out.text and "1 条变更" in out.text
    assert await repo.retire_memory(stale) is False  # retired by the pass


async def test_agent_prompt_injects_habits_not_changelog(svc):
    from muze.ai.agent import Agent

    service, repo = svc
    service.runtime.rag = None
    await repo.add_memory("habit", "muze", "待办引用一律用稠密序号，不用数据库id")
    agent = Agent(service, repo, None, ChatLog())
    ctx = ControlContext(source="qq", chat_key="qq:group/99", scene="group", peer_id=99)
    prompt = await agent._system_prompt(ctx, query="随便")
    assert "你自己沉淀的操作习惯" in prompt and "稠密序号" in prompt
    assert "系统近期变更" not in prompt  # hot path stays lean + cache-stable
