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

    # digits are DENSE numbers: after the close the survivor renumbers #1
    out = await service.run_command("todo", ctx)
    assert "#1" in out.text and "回张三消息" in out.text and "#2" not in out.text
    out = await service.run_command("done 1", ctx)
    assert not out.is_error and "回张三消息" in out.text
    out = await service.run_command("done 5", ctx)  # out of range -> error
    assert out.is_error

    await repo.add_followup("", "材料A", "", "dream:d1", "2026-10-20")
    await repo.add_followup("", "材料B", "", "dream:d1", "2026-10-20")
    out = await service.run_command("done 材料", ctx)  # ambiguous
    assert out.is_error and "材料A" in out.text and "材料B" in out.text

    out = await service.run_command("todo drop 2", ctx)
    assert "不再跟踪" in out.text
    assert await repo.list_followups(status="dropped")


async def test_todo_add_registers_user_task(svc):
    service, repo = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    out = await service.run_command(
        "todo add 10月30日15:30 学明楼307 企业家进高校前沿大课堂 活动写稿", ctx
    )
    assert not out.is_error and "已登记" in out.text
    rows = await repo.list_followups()
    assert len(rows) == 1 and "学明楼307" in rows[0]["title"] and rows[0]["source"] == "user"
    out = await service.run_command("todo add 10月30日15:30 学明楼307 企业家进高校前沿大课堂 活动写稿", ctx)
    assert out.is_error  # identical open item -> dedupe
    out = await service.run_command("todo add 短", ctx)
    assert out.is_error  # too short to be actionable


async def test_done_keyword_candidates_use_dense_nos(svc):
    service, repo = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    await repo.add_followup("", "材料A", "", "dream:d1", "2026-10-20")  # db 1 -> #1
    f2 = await repo.add_followup("", "中间被关", "", "dream:d1", "2026-10-20")
    await repo.close_followup(f2, status="done", closed_date="2026-10-21")
    await repo.add_followup("", "材料B", "", "dream:d1", "2026-10-22")  # db 3 -> #2
    out = await service.run_command("done 材料", ctx)
    assert out.is_error
    assert "#1" in out.text and "#2" in out.text and "#3" not in out.text  # dense, no holes


# ------------------------------------------------- /dream <date> backfills
async def test_dream_missing_date_is_made_on_the_spot(svc):
    from muze.digest import day_range

    class _AgentStub:
        def __init__(self, llm):
            self.llm = llm

    service, repo = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    ts = day_range("2026-10-24")[0] + 3600
    await _seed_day(repo, ts)
    service.agent = _AgentStub(SyncLLM())

    out = await service.run_command("dream 2026-10-24", ctx)
    assert not out.is_error and "刚补做" in out.text
    assert await repo.get_digest("2026-10-24") is not None

    out = await service.run_command("dream 2026-10-24", ctx)  # now cached
    assert "做梦报告" in out.text and "刚补做" not in out.text


async def test_dream_missing_date_without_llm_is_clear_error(svc):
    service, _ = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    out = await service.run_command("dream 2026-10-01", ctx)  # no agent configured
    assert out.is_error and "无法补做" in out.text


# ---------------------------------------------------- mid-hole catch-up
async def test_missing_day_holes_fills_gaps_below_latest(repo):
    from muze.digest import missing_day_holes

    assert await missing_day_holes(repo, "2026-10-23") == ["2026-10-23"]  # empty store
    await repo.upsert_digest("2026-10-20", "r", 24.0, {})
    await repo.upsert_digest("2026-10-22", "r", 24.0, {})
    holes = await missing_day_holes(repo, "2026-10-23")
    assert holes == ["2026-10-21", "2026-10-23"]  # 10-21 sits BELOW the latest


async def test_missing_day_holes_capped(repo):
    from muze.digest import missing_day_holes

    await repo.upsert_digest("2026-09-01", "r", 24.0, {})
    await repo.upsert_digest("2026-09-15", "r", 24.0, {})
    holes = await missing_day_holes(repo, "2026-09-20", limit=7)
    assert len(holes) == 7  # bounded per night, not a two-week burst


# --------------------------------------- /todo merged-forward node layout
class _Recorder:
    """Minimal adapter capturing merged forwards (cf. test_reply_send)."""

    def __init__(self):
        self.sent = []
        self.forwarded = []

        class _Caps:
            send_forward = True

        self.caps = _Caps()

    async def send_private_message(self, user_id, segments):
        from muze.provider.milky.models import SendMessageResult

        self.sent.append(("friend", user_id, segments))
        return SendMessageResult(message_seq=1, time=1)

    async def send_group_message(self, group_id, segments):
        from muze.provider.milky.models import SendMessageResult

        self.sent.append(("group", group_id, segments))
        return SendMessageResult(message_seq=1, time=1)

    async def send_forward_message(self, scene, peer_id, nodes, **kw):
        from muze.provider.milky.models import SendMessageResult

        self.forwarded.append((scene, peer_id, nodes, kw))
        return SendMessageResult(message_seq=9, time=1)

    async def ensure_group_name(self, peer):
        return ""

    async def ensure_friend_name(self, peer):
        return ""


@pytest.fixture
async def env_todo(tmp_path):
    conn = await open_db(str(tmp_path / "td.db"))
    repo = Repository(conn)
    adapter = _Recorder()
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    service = ControlService(cfg, adapter, repo, ChatLog(), Runtime())
    yield service, adapter, cfg
    await conn.close()


async def test_todo_builds_ordered_item_per_node_forward(svc):
    service, repo = svc
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    # add out of order: ids 1..3, but insert 3 first
    f3 = await repo.add_followup("", "第三件", "", "dream:d1", "2026-10-20")
    f1 = await repo.add_followup("group/4242", "第一件", "细节甲", "dream:d1", "2026-10-20")
    f2 = await repo.add_followup("", "第二件", "", "dream:d1", "2026-10-21")
    out = await service.run_command("todo", ctx)
    assert not out.is_error
    assert out.forward_nodes, "todo must carry one-node-per-item layout"
    items = [n for n in out.forward_nodes if n.startswith("#")]
    got_ids = [int(n.split(" ", 1)[0][1:]) for n in items]
    assert got_ids == sorted([f1, f2, f3])  # ascending id = stable opening order
    assert "**#" in out.text and "# 未结待办" in out.text  # markdown for the image


# --------------------------------------------- dense numbering (2026-10-28)
async def test_repo_orders_by_open_order_even_after_touch(repo):
    a = await repo.add_followup("", "A事", "", "dream:d1", "2026-10-01")
    b = await repo.add_followup("", "B事", "", "dream:d1", "2026-10-02")
    await repo.touch_followup(b, "2026-10-30")  # bumps updated_at
    assert [r["id"] for r in await repo.list_followups()] == [a, b]  # still open order


async def test_format_followups_dense_after_close(repo):
    from muze.digest import _format_followups, numbered_followups

    await repo.add_followup("", "事项甲", "", "dream:d1", "2026-10-20")
    f2 = await repo.add_followup("", "事项乙", "", "dream:d1", "2026-10-20")
    await repo.add_followup("", "事项丙", "", "dream:d1", "2026-10-20")
    await repo.close_followup(f2, status="done", closed_date="2026-10-21")
    rows = await repo.list_followups()
    assert [n for n, _ in numbered_followups(rows)] == [1, 2]  # no holes
    txt = _format_followups(rows)
    assert "#1" in txt and "#2" in txt and "#3" not in txt
    assert "事项甲" in txt and "事项丙" in txt and "事项乙" not in txt


async def test_sync_maps_dense_no_to_db_id(repo):
    f1 = await repo.add_followup("", "事项甲", "", "dream:d1", "2026-10-20")
    await repo.add_followup("", "事项乙", "", "dream:d1", "2026-10-20")
    await repo.add_followup("", "事项丙", "", "dream:d1", "2026-10-20")
    await repo.close_followup(f1, status="dropped", closed_date="2026-10-21")
    # open list: 事项乙(#1/db2), 事项丙(#2/db3) -- LLM closes #1 -> db 2
    llm = SyncLLM(sync_json='[{"action":"close","id":1,"status":"done","note":"完成"}]')
    await sync_followups(repo, llm, MuzeConfig(), date="2026-10-24", report="", chatlog=ChatLog())
    assert [r["title"] for r in await repo.list_followups()] == ["事项丙"]


# ------------------------------------------------ nightly completion sweep
class CheckLLM(SyncLLM):
    """Route the completion-check prompt to scripted JSON."""

    def __init__(self, check_json: str = "[]"):
        super().__init__()
        self.check_json = check_json
        self.check_prompts: list[str] = []

    async def chat(self, messages, tools=None):
        p = messages[-1]["content"]
        if "完成度检查" in p:
            self.check_prompts.append(p)
            return {"role": "assistant", "content": self.check_json}
        return await super().chat(messages)


async def test_check_followups_closes_on_raw_evidence(repo):
    from muze.digest import check_followups, day_range

    await repo.add_followup("group/4242", "学明楼活动写稿", "下周五前", "user", "2026-10-27")
    ts = day_range("2026-10-27")[0] + 3600
    await _seed_day(repo, ts)  # the home conversation talked that day
    llm = CheckLLM(check_json='[{"no":1,"action":"close","status":"done","note":"已交稿"}]')
    n = await check_followups(repo, llm, MuzeConfig(), date="2026-10-27", chatlog=ChatLog())
    assert n == 1
    assert await repo.list_followups() == []
    # the prompt carried the task AND its home-conversation day evidence
    p = llm.check_prompts[0]
    assert "学明楼活动写稿" in p and "周五前交材料" in p


async def test_check_followups_touch_no_evidence(repo):
    from muze.digest import check_followups

    await repo.add_followup("", "遥远的事", "", "dream:d1", "2026-10-01")
    llm = CheckLLM(check_json='[{"no":1,"action":"touch","note":"有进展"}]')
    assert await check_followups(repo, llm, MuzeConfig(), date="2026-10-27", chatlog=ChatLog()) == 1
    assert (await repo.list_followups())[0]["last_date"] == "2026-10-27"
    # an empty verdict changes nothing
    assert await check_followups(repo, CheckLLM(), MuzeConfig(), date="2026-10-27", chatlog=ChatLog()) == 0
    assert len(await repo.list_followups()) == 1


async def test_dream_night_sweeps_before_digest(repo):
    from muze.digest import dream_night

    class SweepOnly(CheckLLM):
        async def chat(self, messages, tools=None):
            p = messages[-1]["content"]
            if "完成度检查" in p:
                self.check_prompts.append(p)
                return {"role": "assistant", "content": "[]"}
            return {"role": "assistant", "content": "## 概览\n无\n## 需要处理\n无"}

    llm = SweepOnly()
    await repo.add_followup("", "待查事项", "", "user", "2026-10-27")
    out = await dream_night(repo, llm, MuzeConfig(), ChatLog())
    assert llm.check_prompts, "dream_night must run the completion sweep first"
    assert "做梦" in out


# ----------------------------------------------------------- agent tools
async def test_followup_add_tool_and_gating(svc):
    import json

    service, repo = svc
    await repo.add_followup("", "已有事项", "", "dream:d1", "2026-10-20")
    assert "followup_add" in WRITE_TOOLS

    outside = ControlContext(source="qq", chat_key="qq:group/999", scene="group", peer_id=999)
    runner = ToolRunner(service, repo, service.adapter, None, outside)
    out = json.loads(await runner.run(
        "followup_add", {"title": "10月30日15:30 学明楼307 活动写稿"}
    ))
    assert out.get("needs_approval") is True  # gated outside the control ctx
    assert len(await repo.list_followups()) == 1  # nothing added yet

    inside = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    runner = ToolRunner(service, repo, service.adapter, None, inside)
    out = json.loads(await runner.run(
        "followup_add",
        {"title": "10月30日15:30 学明楼307 活动写稿", "subject": "group/4242", "detail": "前沿大课堂"},
    ))
    assert out.get("added") is True
    rows = await repo.list_followups()
    assert [r["title"] for r in rows] == ["已有事项", "10月30日15:30 学明楼307 活动写稿"]
    assert rows[1]["source"] == "user" and rows[1]["subject"] == "group/4242"


async def test_followup_tools_use_dense_ids(svc):
    import json

    service, repo = svc
    inside = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    await repo.add_followup("", "事项甲", "", "dream:d1", "2026-10-20")  # db 1
    f2 = await repo.add_followup("", "中间被关", "", "dream:d1", "2026-10-20")
    await repo.close_followup(f2, status="done", closed_date="2026-10-21")
    await repo.add_followup("", "事项丙", "", "dream:d1", "2026-10-22")  # db 3

    runner = ToolRunner(service, repo, service.adapter, None, inside)
    out = json.loads(await runner.run("followup_list", {}))
    ids = [f["id"] for f in out["followups"]]
    assert ids == [1, 2]  # dense (db ids are 1 and 3)

    # closing dense #2 kills 事项丙 (db 3), not the closed db 2
    out = json.loads(await runner.run("followup_close", {"id": 2, "note": "用户说搞定"}))
    assert out.get("closed") is True
    assert [r["title"] for r in await repo.list_followups()] == ["事项甲"]


async def test_todo_delivered_as_chain_keeps_order(env_todo):
    """End-to-end: /todo through deliver() -> ONE merged forward whose nodes
    stay one-item-each in id order (user requirement 2026-10-26)."""
    service, adapter, cfg = env_todo
    from muze.core.service import ControlContext

    titles = ["甲事", "乙事", "丙事", "丁事", "戊事", "己事", "庚事", "辛事"]
    for t in titles:
        await service.repo.add_followup("", t, "细节" * 90, "dream:d1", "2026-10-20")
    cfg.reply.image_markdown = False
    out = await service.run_command(
        "todo", ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)
    )
    ok = await service.deliver(
        ControlContext(source="qq", chat_key="qq:group/5", scene="group", peer_id=5),
        out,
    )
    assert ok is True
    assert len(adapter.forwarded) == 1
    nodes = adapter.forwarded[0][2]
    texts = [n[0]["data"]["text"] for n in nodes]
    assert len(texts) == len(titles) + 2  # header + one per item + tail, in order
    assert texts[0].startswith("未结待办")
    for i, t in enumerate(titles, start=1):
        assert t in texts[i]
    assert "/done" in texts[-1]

# ----------------------------------------------- agent prompt carries TODOs
async def test_agent_prompt_lists_open_followups(svc):
    from muze.ai.agent import Agent

    service, repo = svc
    await repo.add_followup("group/4242", "交材料", "周五前", "dream:d1", "2026-10-20")
    agent = Agent(service, repo, None, ChatLog())
    ctx = ControlContext(source="qq", chat_key="qq:group/99", scene="group", peer_id=99)
    prompt = await agent._system_prompt(ctx)
    assert "交材料" in prompt and "#1" in prompt
    assert "最新下落" in prompt and "followup_close" in prompt  # pull-progress rule


async def test_memories_ordered_by_topic_similarity(svc):
    """Resident memory injection picks by vector similarity to the query
    (2026-10-26 request); the conversation heuristic only breaks ties."""
    from muze.ai.agent import Agent

    service, repo = svc
    await repo.add_memory("identity", "owner", "研究生主线事实")  # heuristic rank 1
    await repo.add_memory("fact", "999", "无关会话的事实")  # heuristic rank 3

    seen: dict = {}

    class _Rag:
        enabled = True

        async def search(self, query, k=None, *, kind=None, **kw):
            # record instead of asserting here: a raising mock would be
            # swallowed by the code-under-test's best-effort except
            seen.update(query=query, kind=kind, k=k)
            return [{"kind": "memory", "ref": "memory:2", "text": "[fact] 999: 无关会话的事实"}]

    service.runtime.rag = _Rag()
    agent = Agent(service, repo, None, ChatLog())
    ctx = ControlContext(source="qq", chat_key="qq:group/99", scene="group", peer_id=99)
    prompt = await agent._system_prompt(ctx, query="运动会照片发了吗")
    i_hit = prompt.index("无关会话的事实")
    i_owner = prompt.index("研究生主线事实")
    assert i_hit < i_owner  # topic-similar memory leads despite heuristic rank
    assert seen.get("kind") == "memory" and "运动会照片" in str(seen.get("query", ""))

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
