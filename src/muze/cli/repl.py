"""Interactive CLI front: `muze cli`.

Runs a REPL against the same ControlService. The local console is the
operator's own hands, so sends execute immediately (auto-confirm).
Requires the protocol side (LLBot / Lagrange.Milky) to be online; the DB is
shared with the daemon (SQLite WAL handles multi-process access).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from muze.ai.agent import Agent
from muze.ai.llm import LLMClient
from muze.chatlog import ChatLog, setup_logging
from muze.config import load_config
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.base import ProtocolError
from muze.provider.milky.adapter import MilkyAdapter
from muze.store.db import open_db
from muze.store.repo import Repository
from muze.version import get_version


def parse_cli_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="muze cli", description="copilot-muze interactive console")
    p.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    return p.parse_args(argv)


async def repl_loop(service: ControlService, ctx: ControlContext) -> None:
    loop = asyncio.get_running_loop()
    print("输入 /help 查看命令；直接说话交给 AI；/exit 退出。", flush=True)
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("muze> "))
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = line.strip()
        if not line:
            continue
        if line in ("/exit", "/quit", "/q"):
            return
        outcome = await service.execute(line, ctx)
        if outcome.is_error:
            print(f"[x] {outcome.text}", flush=True)
        elif outcome.text:
            print(outcome.text, flush=True)
        for p in outcome.media_paths:
            print(f"  [media] {p}", flush=True)


def run_cli(argv: list[str]) -> int:
    args = parse_cli_args(argv)
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"config error: {e}", encoding="utf-8")
        return 2

    setup_logging(cfg.log, cfg.storage)  # keep the shared chat log growing

    async def main_async() -> None:
        conn = await open_db(str(cfg.storage.db_path))
        repo = Repository(conn)
        adapter = MilkyAdapter(cfg.milky)
        try:
            login = await adapter.start()
        except ProtocolError as e:
            print(f"协议端不可用: {e}\n请先启动 LLBot/Lagrange.Milky 并检查配置。", encoding="utf-8")
            await adapter.close()
            return
        runtime = Runtime()
        runtime.self_id = login.user_id
        chatlog = ChatLog()
        service = ControlService(cfg, adapter, repo, chatlog, runtime)
        llm = LLMClient(cfg.llm)
        service.agent = Agent(service, repo, llm, chatlog, cfg.llm.agent_max_steps)
        ctx = ControlContext(
            source="cli",
            chat_key="cli:console",
            scene="",
            peer_id=0,
            user_label="console",
            auto_confirm=True,
        )
        print(f"muze {get_version()} | self={login.user_id} | db={cfg.storage.db_path}", flush=True)
        try:
            await repl_loop(service, ctx)
        finally:
            await adapter.close()
            await conn.close()

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
