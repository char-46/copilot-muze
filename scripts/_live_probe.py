# -*- coding: utf-8 -*-
"""LIVE probe (temporary): run the agent on the REAL 10-30 event chat and
see whether it files the delegated task as a follow-up.

Scene: friend/1684728923 -- on 2026-10-23 15:17 the owner (1473195957)
wrote: "10月30日（下周五）下午3:30 学明楼307 "企业家进高校"前沿大课堂 /
这个活动需要写稿，给我备份后向下对接".

We ask in that conversation as the owner and print:
  - the tool calls the agent made (followup_add? search?)
  - the resulting open follow-up rows (added by this run are marked NEW)
"""
import asyncio
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\copilot_muze\.venv\Lib\site-packages")

DB = r"D:\copilot_muze\data\1473195957\muze.db"
SCENE, PEER = "friend", 1684728923
SELF_ID = 1473195957


def snapshot():
    con = sqlite3.connect(DB)
    try:
        return {
            r[0]: r[1]
            for r in con.execute(
                "SELECT id, title FROM followups WHERE status='open'"
            )
        }
    finally:
        con.close()


async def main() -> int:
    from muze.ai.tools import ToolRunner
    from muze.chatlog import ChatLog
    from muze.config import load_accounts
    from muze.core.service import ControlContext, ControlService, Runtime
    from muze.ai.agent import Agent
    from muze.store.db import open_db
    from muze.store.repo import Repository

    specs = load_accounts(r"D:\copilot_muze\config.yaml")
    spec = specs[0]
    cfg = spec.config
    conn = await open_db(str(cfg.storage.account_dir(SELF_ID) / "muze.db"))
    repo = Repository(conn)

    class NoAdapter:
        pass

    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    from muze.ai.llm import LLMClient

    llm = LLMClient(cfg.llm, data_dir=cfg.storage.data_dir)
    agent = Agent(service, repo, llm, ChatLog())

    # the owner speaking in the real conversation (auto_confirm: this is the
    # control-side equivalent so write tools apply without a human prompt)
    ctx = ControlContext(
        source="qq", chat_key=f"qq:{SCENE}/{PEER}", scene=SCENE, peer_id=PEER,
        auto_confirm=True, sender_id=SELF_ID, is_control=False,
    )

    before = snapshot()
    print("== follow-ups BEFORE ==")
    for k, v in before.items():
        print("   #%s %s" % (k, v[:70]))

    question = "10月30日学明楼307那个企业家进高校的活动，写稿的事安排下去了吗？顺手帮我记一下要跟进"
    print("\n== asking in %s/%s: %s ==\n" % (SCENE, PEER, question))
    out = await agent.ask(question, ctx)
    print("== answer ==")
    print((out.text or "")[:1500])

    after = snapshot()
    print("\n== follow-ups AFTER ==")
    new = [k for k in after if k not in before]
    for k in sorted(after):
        mark = "NEW " if k in new else "    "
        print("   %s#%s %s" % (mark, k, after[k][:80]))
    print("\nNEW IDs: %s" % (new or "(none -- task was NOT filed)"))
    await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
