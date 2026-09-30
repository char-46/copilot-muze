"""Application assembly: wires provider, store, ingest, control, web together.

One process may serve several QQ accounts (accounts.milky[]). Every account
gets its own data dir (<storage.data_dir>/<uin>/), database, media store and
schedule; the webui is application-level and serves all of them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from muze.ai.agent import Agent
from muze.ai.llm import LLMClient
from muze.chatlog import ChatLog, setup_logging
from muze.config import AccountSpec, load_accounts
from muze.core.service import ControlContext, ControlService, Runtime
from muze.ingest.backfill import Backfiller
from muze.ingest.pipeline import IngestPipeline
from muze.provider.base import ProtocolError
from muze.provider.milky.adapter import MilkyAdapter
from muze.provider.milky.models import (
    Event,
    FriendFileUploadData,
    GroupFileUploadData,
    IncomingMessage,
    MentionAllSeg,
    MentionSeg,
    MessageEventData,
    MessageRecallData,
    TextSeg,
)
from muze.store.blobs import BlobStore
from muze.store.db import open_db
from muze.store.media import MediaStore
from muze.store.repo import Repository
from muze.version import get_version

log = logging.getLogger("muze.main")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="muze serve", description="copilot-muze daemon")
    p.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    return p.parse_args(argv)


class App:
    """One account stack inside the process."""

    def __init__(self, spec: AccountSpec, config_path: str) -> None:
        self.spec = spec
        self.cfg = spec.config
        self.config_path = config_path
        self.chatlog = ChatLog()
        self.runtime = Runtime()
        self.runtime.instance_name = spec.name
        self.runtime.endpoint = spec.endpoint
        self._stop = asyncio.Event()
        self.restart_requested = False
        self._shutdown_done = False
        self._mt_task = None
        self.conn = None
        self.repo = None
        self.adapter = None
        self.media = None
        self.pipeline = None
        self.backfiller = None
        self.service = None
        self.account_dir: Path | None = None

    async def setup(self) -> None:
        cfg = self.cfg
        cfg.storage.data_dir.mkdir(parents=True, exist_ok=True)

        self.adapter = MilkyAdapter(self.spec.endpoint)
        login = await self.adapter.start()
        self.runtime.self_id = login.user_id
        log.info("[%s] logged in as %s (%s)", self.spec.name, login.user_name or "?", login.user_id)

        # per-account data dir is derived from the logged-in QQ number;
        # media bytes go to the shared app-level blob store (cross-account dedup)
        acct = cfg.storage.account_dir(login.user_id)
        self.account_dir = acct
        self.conn = await open_db(str(acct / "muze.db"))
        self.repo = Repository(self.conn)

        # multi-account isolation: one data dir belongs to exactly one account
        conflict = await self.repo.bind_owner(login.user_id)
        if conflict is not None:
            raise RuntimeError(
                f"数据目录 {acct} 已绑定账号 {conflict}，"
                f"当前登录的是 {login.user_id}。请勿手工挪用账号数据目录。"
            )

        self.blobstore = BlobStore(cfg.storage.data_dir / "blobs")
        self.media = MediaStore(self.blobstore, self.adapter, self.repo)
        self.pipeline = IngestPipeline(
            self.adapter, self.repo, self.media, cfg, self.chatlog, login.user_id
        )
        self.backfiller = Backfiller(self.adapter, self.repo, cfg, self.pipeline, self.chatlog)
        self.runtime.backfiller = self.backfiller

        self.service = ControlService(cfg, self.adapter, self.repo, self.chatlog, self.runtime)
        self.llm = LLMClient(cfg.llm)
        self.agent = Agent(
            self.service, self.repo, self.llm, self.chatlog, cfg.llm.agent_max_steps
        )
        self.service.agent = self.agent

        # skills / scheduler / reloader
        from muze.reloader import Reloader
        from muze.scheduler import Scheduler
        from muze.skills import SkillStore

        repo_root = Path(__file__).resolve().parents[2]
        self.skills = SkillStore([repo_root / "skills", cfg.storage.data_dir / "skills"])
        self.skills.reload()
        self.runtime.skills = self.skills

        self.scheduler = Scheduler(self.repo, self.service, self.chatlog)
        self.runtime.scheduler = self.scheduler
        await self.scheduler.start()
        await self.scheduler.ensure_daily_digest()

        self.reloader = Reloader(
            config_path=self.config_path,
            cfg=cfg,
            endpoint=self.spec.endpoint,
            skills=self.skills,
            llm=self.llm,
            agent=self.agent,
            runtime=self.runtime,
        )
        self.runtime.reloader = self.reloader
        self.runtime.request_restart = self._request_restart
        self.runtime.request_stop = self._request_stop
        self.runtime.llm = self.llm

    def _request_restart(self) -> None:
        self.restart_requested = True
        self._stop.set()

    def _request_stop(self) -> None:
        self.restart_requested = False
        self._stop.set()

    def _stop_quiet(self) -> None:
        """Stop this account WITHOUT changing its restart intent.

        Used when a sibling account exits: the others must come down, but
        an account that itself asked for a restart keeps that flag so
        run_main still returns the daemon's restart code.
        """
        self._stop.set()

    # ------------------------------------------------------------- events
    async def run(self, spawn_console: bool = True, console_registry=None) -> None:
        self._loop = asyncio.get_running_loop()
        await self.backfiller.start_background()
        self._mt_task = asyncio.create_task(self._maintenance_loop())
        if spawn_console:
            self._spawn_console(console_registry)
        events_task = asyncio.create_task(self._consume_events())
        stopper = asyncio.create_task(self._stop.wait())
        try:
            _done, pending = await asyncio.wait(
                {events_task, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            # reap cancellations so the event generator / websocket really closes
            await asyncio.gather(events_task, stopper, return_exceptions=True)
        finally:
            await self.shutdown()

    async def _consume_events(self) -> None:
        async for ev in self.adapter.events():
            await self.dispatch(ev)

    # ------------------------------------------------------------- console
    def _spawn_console(self, registry=None) -> None:
        """Interactive REPL on stdin when running in a foreground terminal."""
        import os

        forced = os.environ.get("MUZE_FORCE_CONSOLE") == "1"
        if not forced and not (sys.stdin.isatty() and sys.stdout.isatty()):
            log.info("stdin is not a tty; console disabled (use `muze cli`, the webui, or MUZE_FORCE_CONSOLE=1)")
            return
        self._console_registry = registry
        t = threading.Thread(
            target=self._console_thread,
            name="muze-console",
            daemon=True,
        )
        t.start()

    def _console_thread(self) -> None:
        from muze.console import repl_thread

        ctx = ControlContext(
            source="cli",
            chat_key="cli:console",
            scene="",
            peer_id=0,
            user_label="console",
            auto_confirm=True,
        )
        history = self.cfg.storage.data_dir / "console_history"
        registry = getattr(self, "_console_registry", None)
        try:
            repl_thread(self._loop, self.service, ctx, history, self._stop, registry=registry)
        except Exception:
            log.exception("console thread crashed")

    async def dispatch(self, ev: Event) -> None:
        try:
            if ev.event_type == "message_receive" and isinstance(ev.data, MessageEventData):
                await self.on_message(ev.data)
            elif ev.event_type == "message_recall" and isinstance(ev.data, MessageRecallData):
                await self.pipeline.handle_recall(ev.data)
            elif ev.event_type == "bot_offline":
                log.warning("bot offline: %s", getattr(ev.data, "reason", "?"))
            elif ev.event_type == "friend_file_upload" and isinstance(ev.data, FriendFileUploadData):
                self.chatlog.log.info(
                    "CHAT file | friend/%s %s %sB is_self=%s",
                    ev.data.user_id, ev.data.file_name, ev.data.file_size, ev.data.is_self,
                )
            elif ev.event_type == "group_file_upload" and isinstance(ev.data, GroupFileUploadData):
                self.chatlog.log.info(
                    "CHAT file | group/%s by %s %s %sB",
                    ev.data.group_id, ev.data.user_id, ev.data.file_name, ev.data.file_size,
                )
        except Exception:
            log.exception("event dispatch failed: %s", ev.event_type)

    async def on_message(self, msg: IncomingMessage) -> None:
        await self.pipeline.handle_message(msg, source="event")
        svc = self.service
        mention_self = any(
            (isinstance(s, MentionSeg) and s.user_id == self.runtime.self_id)
            or isinstance(s, MentionAllSeg)
            for s in msg.segments
        )
        texts = [s.text for s in msg.segments if isinstance(s, TextSeg)]
        text = " ".join(t for t in texts if t).strip()
        is_self = bool(getattr(msg, "is_self", False)) or msg.sender_id == self.runtime.self_id

        if not svc.qq_should_trigger(
            msg.message_scene, msg.peer_id, msg.sender_id, mention_self, text, is_self=is_self
        ):
            return
        ctx = ControlContext(
            source="qq",
            chat_key=f"qq:{msg.message_scene}:{msg.peer_id}",
            scene=msg.message_scene,
            peer_id=msg.peer_id,
            user_label=await self.pipeline.sender_display(msg),
            auto_confirm=False,
        )
        outcome = await svc.execute(text, ctx)
        await svc.deliver(ctx, outcome)

    # --------------------------------------------------------- maintenance
    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(600)
            try:
                n = await self.repo.expire_actions(self.cfg.control.confirm_timeout_min)
                if n:
                    log.info("expired %s pending confirmation(s)", n)
                gone = await self.repo.retire_gone_media()
                if gone:
                    log.info("retired %s permanently-gone media row(s) (404/410)", gone)
                fixed = await self.media.retry_failed(limit=20)
                if fixed:
                    log.info("media retry recovered %s file(s)", fixed)
            except Exception:
                log.exception("maintenance cycle failed")

    async def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return  # idempotent: run()'s finally and the bootstrap both call it
        self._shutdown_done = True
        log.info("[%s] shutting down...", self.spec.name)
        self._stop.set()

        async def _safe(what: str, coro, timeout: float) -> None:
            try:
                await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                log.warning(
                    "[%s] %s timed out after %ss; continuing", self.spec.name, what, timeout
                )
            except Exception:
                log.exception("[%s] %s failed during shutdown", self.spec.name, what)

        if self._mt_task is not None:
            self._mt_task.cancel()
        scheduler = getattr(self, "scheduler", None)
        if scheduler is not None:
            await _safe("scheduler.stop", scheduler.stop(), 5)
        if self.backfiller is not None:
            await _safe("backfiller.stop", self.backfiller.stop(), 5)
        if self.pipeline is not None:
            await _safe("drain_media", self.pipeline.drain_media(timeout=5), 7)
        if self.adapter is not None:
            await _safe("adapter.close", self.adapter.close(), 5)
        if self.conn is not None:
            await _safe("conn.close", self.conn.close(), 5)


RESTART_EXIT = 100  # daemon contract: rc=100 -> relaunch serve
FORCE_EXIT_SECONDS = 10  # graceful shutdown deadline after Ctrl-C


def _force_exit_after(seconds: float) -> None:
    time.sleep(seconds)
    sys.stderr.write(f"\nmuze: shutdown did not finish in {seconds}s; forcing exit\n")
    os._exit(130)


def _make_sigint_handler(apps: list["App"], loop: asyncio.AbstractEventLoop):
    """Ctrl-C 1st press: graceful stop (no KeyboardInterrupt cancelling the
    loop mid-cleanup, which is what made exits hang). 2nd press: hard exit.
    A watchdog thread guarantees the process dies even if cleanup wedges."""

    state = {"count": 0}

    def handler(signum, frame):  # runs in the main thread
        state["count"] += 1
        if state["count"] == 1:
            log.warning("Ctrl-C: shutting down gracefully (press again to force-quit)")
            loop.call_soon_threadsafe(lambda: [a._stop_quiet() for a in apps])
            threading.Thread(
                target=_force_exit_after, args=(FORCE_EXIT_SECONDS,), daemon=True
            ).start()
        else:
            log.warning("Ctrl-C again: forcing exit now")
            os._exit(130)

    return handler


def run_main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        specs: list[AccountSpec] = load_accounts(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    log_path = setup_logging(specs[0].config.log, specs[0].config.storage)
    log.info(
        "muze %s starting (config=%s, accounts=%s)",
        get_version(), args.config, [s.name for s in specs],
    )
    log.info("log file: %s (never rotated)", log_path)

    apps = [App(s, args.config) for s in specs]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    sigint_handler = _make_sigint_handler(apps, loop)
    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, sigint_handler)
        except (ValueError, OSError):
            pass  # not the main thread

    rc = 0
    try:
        loop.run_until_complete(_bootstrap(apps))
    except KeyboardInterrupt:
        log.info("interrupted by user; shutting down")
    except ProtocolError as e:
        log.error("protocol side unreachable or misconfigured: %s", e)
        log.error("check: LLBot/Lagrange.Milky is running, accounts.milky[].base_url & access_token are correct")
        rc = 3
    except RuntimeError as e:
        log.error("%s", e)
        rc = 4
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()

    if rc:
        return rc
    if any(a.restart_requested for a in apps):
        log.info("restart requested (rc=%d); daemon will relaunch", RESTART_EXIT)
        return RESTART_EXIT
    return 0


async def _bootstrap(apps: list[App]) -> None:
    """Set up every account, start the app-level webui, then run them all.

    Any account exiting (shutdown / restart / crash) stops the others; the
    exit code is aggregated in run_main.
    """
    web_runner = None
    registry: dict[str, ControlService] = {}
    try:
        for i, app in enumerate(apps):
            app.runtime.is_primary = i == 0  # only apps[0] answers bare
            #                                commands in shared groups
            await app.setup()
            registry[app.spec.name] = app.service

        if apps[0].cfg.web.enabled:
            from muze.web.server import start_web

            accounts = {
                app.spec.name: (app.service, app.repo, app.runtime)
                for app in apps
            }
            web_runner, _token = await start_web(
                apps[0].cfg.web, accounts, apps[0].chatlog
            )

        log.info("running %s account(s): %s", len(apps), ", ".join(registry))
        tasks = [
            asyncio.create_task(
                app.run(
                    spawn_console=(i == 0),
                    console_registry=registry if len(apps) > 1 else None,
                ),
                name=f"account:{app.spec.name}",
            )
            for i, app in enumerate(apps)
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for app in apps:
            app._stop_quiet()  # bring siblings down, keep their restart intent
        for t in pending:
            try:
                await t
            except Exception:
                pass
        for t in done:
            exc = t.exception()
            if exc:
                log.error("account task crashed: %s", exc)
    finally:
        if web_runner is not None:
            try:
                await asyncio.wait_for(web_runner.cleanup(), timeout=5)
            except Exception:
                log.exception("web cleanup failed")
        for a in apps:
            try:
                await a.shutdown()
            except Exception:
                log.exception("cleanup after shutdown crashed")
