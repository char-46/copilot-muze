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
from muze.config import load_instances
from muze.console import repl_async
from muze.core.service import ControlContext, ControlService, Runtime
from muze.provider.base import ProtocolError
from muze.provider.milky.adapter import MilkyAdapter
from muze.store.db import open_db
from muze.store.repo import Repository
from muze.version import get_version


def parse_cli_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="muze cli", description="copilot-muze interactive console")
    p.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    p.add_argument(
        "-i", "--instance", default=None,
        help="instance name when the config defines multiple accounts",
    )
    return p.parse_args(argv)


def run_cli(argv: list[str]) -> int:
    args = parse_cli_args(argv)
    try:
        load_instances(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    specs = load_instances(args.config)
    spec = specs[0]
    if args.instance:
        matches = [s for s in specs if s.name == args.instance]
        if not matches:
            print(
                f"instance {args.instance!r} not found; available: "
                + ", ".join(s.name for s in specs),
                file=sys.stderr,
            )
            return 2
        spec = matches[0]
    cfg = spec.config

    setup_logging(cfg.log, cfg.storage)  # keep the shared chat log growing

    async def main_async() -> None:
        conn = await open_db(str(cfg.storage.db_path))
        repo = Repository(conn)
        adapter = MilkyAdapter(cfg.milky)
        try:
            login = await adapter.start()
        except ProtocolError as e:
            print(f"协议端不可用: {e}\n请先启动 LLBot/Lagrange.Milky 并检查配置。", file=sys.stderr)
            await adapter.close()
            return
        conflict = await repo.bind_owner(login.user_id)
        if conflict is not None:
            print(
                f"数据目录已绑定账号 {conflict}（当前登录 {login.user_id}），拒绝启动。"
                f"请为该账号使用独立的 storage.data_dir。",
                file=sys.stderr,
            )
            await adapter.close()
            await conn.close()
            return
        runtime = Runtime()
        runtime.self_id = login.user_id
        chatlog = ChatLog()
        service = ControlService(cfg, adapter, repo, chatlog, runtime)
        llm = LLMClient(cfg.llm)
        service.agent = Agent(service, repo, llm, chatlog, cfg.llm.agent_max_steps)

        from pathlib import Path

        from muze.skills import SkillStore

        repo_root = Path(__file__).resolve().parents[3]
        skills = SkillStore([repo_root / "skills", cfg.storage.data_dir / "skills"])
        skills.reload()
        runtime.skills = skills
        ctx = ControlContext(
            source="cli",
            chat_key="cli:console",
            scene="",
            peer_id=0,
            user_label="console",
            auto_confirm=True,
        )
        print(f"muze {get_version()} | self={login.user_id} | db={cfg.storage.db_path}", flush=True)
        history = cfg.storage.data_dir / "console_history"
        try:
            await repl_async(service, ctx, history)
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
