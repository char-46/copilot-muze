"""Structured follow-up tracking + digest personalization (alpha.138).

Two live complaints drove this (2026-10-24):
1. a finished task could never close because its "completion signal"
   never appeared in QQ — pending state lived only as prose in past
   digests, and the user had no way to say "done";
2. the report kept leading with undergrad affairs although the owner
   (a grad student + part-time counselor) had that fact in long-term
   memory — the digest prompt never saw the owner's memories.
"""

import pytest

from muze.ai.tools import ToolRunner, WRITE_TOOLS
from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.digest import owner_context, run_digest, sync_followups
from muze.store.db import open_db
from muze.store.repo import Repository


class SyncLLM:
    """Digest call -> fixed report; sync call -> scripted JSON."""

    configured = True

    def __init__(self, sync_json: str = "[]"):
        self.sync_json = sync_json
        self.digest_prompts: list[str] = []

    async def chat(self, messages, tools=None):
        prompt = messages[-1]["content"]
        if "待办同步" in prompt:
            return {"role": "assistant", "content": self.sync_json}
        self.digest_prompts.append(prompt)
        return {"role": "assistant", "content": "## 概览\n测试。\n## 需要处理\n无"}


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "fu.db"))
    yield Repository(conn)
    await conn.close()


async def _seed_day(repo, date_ts: int) -> None:
    from muze.provider.milky.models import IncomingMessage

    m = IncomingMessage.from_raw(
        {
            "message_scene": "group", "peer_id": 4242, "message_seq": 7,
            "sender_id": 555, "time": date_ts,
            "segments": [{"type": "text", "data": {"text": "周五前交材料"}}],
        }
    )
    await repo.upsert_message(m, "周五前交材料", "event")


# ------------------------------------------------------------ repo CRUD
async def test_add_dedupe_close_touch_expire(repo):
    assert await repo.add_followup("group/4242", "交材料", "周五前", "dream:d1", "2026-10-01") == 1
    assert await repo.add_followup("group/4242", "交材料", "周五前", "dream:d2", "2026-10-02") is None
    assert len(await repo.list_followups()) == 1

    hits = await repo.find_open_followups("交材料")
    assert [h["id"] for h in hits] == [1]
    assert await repo.find_open_followups("不存在的事") == []

    assert await repo.touch_followup(1, "2026-10-05") is True
    assert (await repo.list_followups())[0]["last_date"] == "2026-10-05"

    assert await repo.close_followup(1, source="user", closed_date="2026-10-06") is True
    assert await repo.close_followup(1) is False  # already closed
    assert await repo.list_followups() == []
    row = (await repo.list_followups(status="done"))[0]
    assert row["close_source"] == "user"


async def test_expire_only_stale_open(repo):
    await repo.add_followup("", "旧事", "", "dream:d1", "2026-09-01")  # stale
    fid2 = await repo.add_followup("", "新事", "", "dream:d1", "2026-10-20")
    await repo.touch_followup(fid2, "2026-10-24")  # alive

    stale = await repo.expire_followups(cutoff_date="2026-10-10", closed_date="2026-10-25")
    assert [r["id"] for r in stale] == [1]
    assert len(await repo.list_followups()) == 1  # the fresh one survives
    expired = await repo.list_followups(status="expired")
    assert expired and expired[0]["close_source"] == "auto-expire"


# -------------------------------------------------- digest personalization
async def test_digest_prompt_carries_owner_profile_and_followups(repo):
    cfg = MuzeConfig()
    await repo.add_memory("identity", "owner", "用户是研究生，兼职本科生辅导员，本科事务非首要")
    await repo.add_followup("group/4242", "交材料", "周五前", "dream:d1", "2026-10-20")

    from muze.digest import local_date
    import time as _t

    await _seed_day(repo, int(_t.time()) - 60)
    llm = SyncLLM()
    await run_digest(repo, llm, cfg, ChatLog(), date=local_date())

    p = llm.digest_prompts[0]
    assert "研究生" in p and "兼职本科生辅导员" in p  # owner memories injected
    assert "#1" in p and "交材料" in p  # open follow-ups with ids
    assert "画像优先" in p  # ordering defers to the profile


async def test_owner_context_disabled_or_empty(repo):
    cfg = MuzeConfig()
    cfg.digest.owner_context = False
    assert "未启用" in await owner_context(repo, cfg)
    cfg.digest.owner_context = True
    assert "暂无" in await owner_context(repo, cfg)
    await repo.add_memory("identity", "owner", "记A")
    await repo.add_memory("fact", "10086", "别人的记忆不算")  # other subject
    txt = await owner_context(repo, cfg)
    assert "记A" in txt and "别人的记忆" not in txt


# ------------------------------------------------------------- sync pass
async def test_sync_adds_closes_keeps(repo):
    f1 = await repo.add_followup("group/4242", "旧待办", "", "dream:d1", "2026-10-20")
    llm = SyncLLM(
        sync_json=(
            '[{"action":"add","subject":"group/4242","title":"催张三回执","detail":"周三前"},'
            f'{{"action":"close","id":{f1},"status":"done","note":"报告称已交"}},'
            f'{{"action":"keep","id":{f1}}}]'
        )
    )
    changed = await sync_followups(repo, llm, MuzeConfig(), date="2026-10-24", report="...", chatlog=ChatLog())
    assert changed == 2  # add + close (keep hit a closed item)
    open_now = await repo.list_followups()
    assert [r["title"] for r in open_now] == ["催张三回执"]
    assert (await repo.list_followups(status="done"))[0]["id"] == f1


async def test_sync_dedupes_identical_adds(repo):
    llm = SyncLLM(sync_json='[{"action":"add","subject":"","title":"同一件事"}]')
    await sync_followups(repo, llm, MuzeConfig(), date="2026-10-24", report="", chatlog=ChatLog())
    n = await sync_followups(repo, llm, MuzeConfig(), date="2026-10-25", report="", chatlog=ChatLog())
    assert n == 0
    assert len(await repo.list_followups()) == 1


async def test_sync_garbage_is_best_effort(repo):
    class BadLLM(SyncLLM):
        async def chat(self, messages, tools=None):
            return {"role": "assistant", "content": "模型幻觉，无 JSON"}

    await repo.add_followup("", "事项", "", "dream:d1", "2026-10-20")
    assert await sync_followups(repo, BadLLM(), MuzeConfig(), date="2026-10-24", report="", chatlog=ChatLog()) == 0
    assert len(await repo.list_followups()) == 1  # untouched


async def test_run_digest_survives_sync_crash(repo):
    class CrashSyncLLM(SyncLLM):
        async def chat(self, messages, tools=None):
            if "待办同步" in messages[-1]["content"]:
                raise RuntimeError("sync blew up")
            return await super().chat(messages)

    from muze.digest import local_date
    import time as _t

    await _seed_day(repo, int(_t.time()) - 60)
    cfg = MuzeConfig()
    cfg.digest.owner_context = False
    report = await run_digest(repo, CrashSyncLLM(), cfg, ChatLog(), date=local_date())
    assert "概览" in report  # the dream itself still succeeds


# --------------------------------------------------------- user commands
@pytest.fixture
async def svc(tmp_path):
    conn = await open_db(str(tmp_path / "s.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)

    class NoAdapter:
        pass

    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    yield service, repo
    await conn.close()


async def test_todo_and_done_commands(svc):
    service, repo = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    f1 = await repo.add_followup("group/4242", "交材料", "周五前", "dream:d1", "2026-10-20")
    await repo.add_followup("", "回张三消息", "", "dream:d1", "2026-10-20")

    out = await service.run_command("todo", ctx)
    assert "交材料" in out.text and "#2" in out.text

    out = await service.run_command("done 交材料", ctx)  # keyword, unique
    assert "已完成" in out.text and "交材料" in out.text
    assert len(await repo.list_followups()) == 1

    out = await service.run_command(f"done {f1}", ctx)  # already closed
    assert out.is_error

    await repo.add_followup("", "材料A", "", "dream:d1", "2026-10-20")
    await repo.add_followup("", "材料B", "", "dream:d1", "2026-10-20")
    out = await service.run_command("done 材料", ctx)  # ambiguous
    assert out.is_error and "材料A" in out.text and "材料B" in out.text

    out = await service.run_command("todo drop 2", ctx)
    assert "不再跟踪" in out.text
    assert await repo.list_followups(status="dropped")


# --------------------------------------------------------- agent tooling
async def test_followup_close_tool_and_gating(svc):
    service, repo = svc
    fid = await repo.add_followup("group/4242", "交材料", "", "dream:d1", "2026-10-20")
    assert "followup_close" in WRITE_TOOLS

    outside = ControlContext(source="qq", chat_key="qq:group/999", scene="group", peer_id=999)
    runner = ToolRunner(service, repo, service.adapter, None, outside)
    out = __import__("json").loads(await runner.run("followup_close", {"id": fid}))
    assert out.get("needs_approval") is True  # gated outside the control ctx
    assert len(await repo.list_followups()) == 1  # nothing closed yet

    inside = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    runner = ToolRunner(service, repo, service.adapter, None, inside)
    out = __import__("json").loads(await runner.run("followup_close", {"id": fid, "note": "用户说交了"}))
    assert out.get("closed") is True and out["status"] == "done"
    assert await repo.list_followups() == []

    out = __import__("json").loads(
        await runner.run("followup_close", {"query": "不存在"})
    )
    assert "error" in out


async def test_followup_list_tool_filters(svc):
    service, repo = svc
    await repo.add_followup("group/4242", "交材料", "", "dream:d1", "2026-10-20")
    await repo.add_followup("", "回张三", "", "dream:d1", "2026-10-20")
    inside = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    runner = ToolRunner(service, repo, service.adapter, None, inside)
    out = __import__("json").loads(await runner.run("followup_list", {"query": "张三"}))
    assert out["count"] == 1 and out["followups"][0]["title"] == "回张三"
