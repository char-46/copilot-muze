"""Interactive CLI front: `muze cli`.

Runs a REPL against the same ControlService. The local console is the
operator's own hands, so sends execute immediately (auto-confirm).
Requires the protocol side (LLBot / Lagrange.Milky) to be online; the DB is
shared with the daemon (SQLite WAL handles multi-process access).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
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
    p.add_argument(
        "--remote", default=None,
        help="upstream webui URL (client mode: drive the running instance "
             "over HTTP instead of embedding a second one), e.g. "
             "http://127.0.0.1:8300; also env MUZE_REMOTE_URL or the "
             "`remote:` section of config.yaml",
    )
    p.add_argument(
        "--remote-token", default=None,
        help="webui bearer token; also env MUZE_REMOTE_TOKEN or remote.token",
    )
    p.add_argument(
        "--account", default=None,
        help="account name on the upstream (multi-account instances)",
    )
    return p.parse_args(argv)


def _resolve_remote(args: argparse.Namespace):
    """args > env > config.yaml `remote:`; returns (url, token, account)."""
    from muze.config import load_remote

    rc = load_remote(args.config)
    url = args.remote or os.environ.get("MUZE_REMOTE_URL") or rc.url
    token = (args.remote_token or os.environ.get("MUZE_REMOTE_TOKEN") or rc.token)
    account = args.account or os.environ.get("MUZE_REMOTE_ACCOUNT") or rc.account
    return url, token, account


async def _run_remote(args: argparse.Namespace) -> int:
    from muze.cli.remote import RemoteService
    from muze.console import repl_async
    from muze.core.service import ControlContext

    url, token, account = _resolve_remote(args)
    if not url:
        print("remote mode needs a URL: --remote URL / MUZE_REMOTE_URL / remote.url",
              file=sys.stderr)
        return 2
    svc = RemoteService(url, token=token, account=account)
    try:
        info = await svc.ping()
    except RuntimeError as e:
        print(f"remote: {e}", file=sys.stderr)
        await svc.close()
        return 2
    ver = info.get("version", "?")
    acct = info.get("account", account or "default")
    print(
        f"muze cli (remote) -> {url} | account={acct} | upstream={ver}\n"
        f"  remote console: /shutdown closes this console only (the service keeps running)",
        flush=True,
    )
    ctx = ControlContext(
        source="cli", chat_key="cli:remote", scene="", peer_id=0,
        user_label="console-remote", auto_confirm=True,
    )
    history = Path(tempfile.gettempdir()) / "muze_remote_console_history"
    try:
        await repl_async(svc, ctx, history)
    finally:
        await svc.close()
    return 0


def run_cli(argv: list[str]) -> int:
    args = parse_cli_args(argv)
    if args.remote is not None or os.environ.get("MUZE_REMOTE_URL"):
        try:
            return asyncio.run(_run_remote(args))
        except KeyboardInterrupt:
            return 0
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
            read_roots = [Path(p) for p in getattr(cfg.storage, "blobs_roots", []) or []]
            legacy = cfg.storage.data_dir / "blobs"
            if legacy.is_dir() and str(legacy.resolve()) != str(cfg.storage.blob_root.resolve()):
                read_roots.append(legacy)
            blobs = BlobStore(cfg.storage.blob_root, read_roots=read_roots)
            media = MediaStore(
                blobs, adapter, repo,
                transcode_silk=cfg.ingest.media_transcode_silk,
                qq_cache_dir=cfg.ingest.qq_cache_dir,
            )
            service = ControlService(cfg, adapter, repo, chatlog, runtime)
            llm = LLMClient(cfg.llm, data_dir=cfg.storage.data_dir)
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
