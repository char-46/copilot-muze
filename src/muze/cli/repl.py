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
from pathlib import Path

from muze.ai.agent import Agent
from muze.ai.llm import LLMClient
from muze.chatlog import ChatLog, setup_logging
from muze.config import load_accounts
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
        help="account name when the config defines multiple accounts",
    )
    return p.parse_args(argv)


def run_cli(argv: list[str]) -> int:
    args = parse_cli_args(argv)
    try:
        specs = load_accounts(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    spec = specs[0]
    if args.instance:
        matches = [s for s in specs if s.name == args.instance]
        if not matches:
            print(
                f"account {args.instance!r} not found; available: "
                + ", ".join(s.name for s in specs),
                file=sys.stderr,
            )
            return 2
        spec = matches[0]
    cfg = spec.config

    setup_logging(cfg.log, cfg.storage)  # keep the shared chat log growing

    async def main_async() -> None:
        conn = None
        adapter = MilkyAdapter(spec.endpoint)
        try:
            login = await adapter.start()
        except ProtocolError as e:
            print(f"协议端不可用: {e}\n请先启动 LLBot/Lagrange.Milky 并检查配置。", file=sys.stderr)
            await adapter.close()
            return
        try:
            acct = cfg.storage.account_dir(login.user_id)
            conn = await open_db(str(acct / "muze.db"))
            repo = Repository(conn)
            conflict = await repo.bind_owner(login.user_id)
            if conflict is not None:
                print(
                    f"数据目录 {acct} 已绑定账号 {conflict}（当前登录 {login.user_id}），拒绝启动。",
                    file=sys.stderr,
                )
                return
            runtime = Runtime()
            runtime.self_id = login.user_id
            runtime.instance_name = spec.name
            runtime.endpoint = spec.endpoint
            chatlog = ChatLog()
            blobs = BlobStore(cfg.storage.blob_root)
            media = MediaStore(
                blobs, adapter, repo,
                transcode_silk=cfg.ingest.media_transcode_silk,
                qq_cache_dir=cfg.ingest.qq_cache_dir,
            )
            service = ControlService(cfg, adapter, repo, chatlog, runtime)
            llm = LLMClient(cfg.llm)
            service.agent = Agent(service, repo, llm, chatlog, cfg.llm.agent_max_steps)

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
            print(f"muze {get_version()} | account={spec.name} | self={login.user_id} | db={acct / 'muze.db'}", flush=True)
            history = cfg.storage.data_dir / "console_history"
            await repl_async(service, ctx, history)
        finally:
            await adapter.close()
            if conn is not None:
                await conn.close()

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
