"""Application assembly: wires provider, store, ingest, control, web together."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from muze.ai.agent import Agent
from muze.ai.llm import LLMClient
from muze.chatlog import ChatLog, setup_logging
from muze.config import MuzeConfig, load_config
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
    def __init__(self, cfg: MuzeConfig) -> None:
        self.cfg = cfg
        self.chatlog = ChatLog()
        self.runtime = Runtime()
        self._stop = asyncio.Event()
        self.conn = None
        self.repo = None
        self.adapter = None
        self.media = None
        self.pipeline = None
        self.backfiller = None
        self.service = None
        self.web_runner = None

    async def setup(self) -> None:
        cfg = self.cfg
        cfg.storage.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.storage.media_dir.mkdir(parents=True, exist_ok=True)

        self.conn = await open_db(str(cfg.storage.db_path))
        self.repo = Repository(self.conn)

        self.adapter = MilkyAdapter(cfg.milky)
        login = await self.adapter.start()
        self.runtime.self_id = login.user_id
        log.info("logged in as %s (%s)", login.user_name or "?", login.user_id)

        self.media = MediaStore(cfg.storage.media_dir, self.adapter, self.repo)
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

        if cfg.web.enabled:
            from muze.web.server import start_web

            self.web_runner, _token = await start_web(
                cfg.web, self.service, self.repo, self.runtime, self.chatlog
            )

    # ------------------------------------------------------------- events
    async def run(self) -> None:
        await self.backfiller.start_background()
        asyncio.create_task(self._maintenance_loop())
        try:
            async for ev in self.adapter.events():
                await self.dispatch(ev)
        finally:
            await self.shutdown()

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

        if not svc.qq_should_trigger(msg.message_scene, msg.peer_id, msg.sender_id, mention_self, text):
            return
        if not text:
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
                fixed = await self.media.retry_failed(limit=20)
                if fixed:
                    log.info("media retry recovered %s file(s)", fixed)
            except Exception:
                log.exception("maintenance cycle failed")

    async def shutdown(self) -> None:
        log.info("shutting down...")
        self._stop.set()
        if self.backfiller is not None:
            await self.backfiller.stop()
        if self.web_runner is not None:
            await self.web_runner.cleanup()
        if self.pipeline is not None:
            await self.pipeline.drain_media(timeout=5)
        if self.adapter is not None:
            await self.adapter.close()
        if self.conn is not None:
            await self.conn.close()


def run_main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    log_path = setup_logging(cfg.log, cfg.storage)
    log.info("muze %s starting (config=%s)", get_version(), args.config)
    log.info("log file: %s (never rotated)", log_path)

    app = App(cfg)
    try:
        asyncio.run(_bootstrap(app))
    except KeyboardInterrupt:
        log.info("interrupted by user")
        return 0
    except ProtocolError as e:
        log.error("protocol side unreachable or misconfigured: %s", e)
        log.error("check: LLBot/Lagrange.Milky is running, milky.base_url & access_token are correct")
        return 3
    return 0


async def _bootstrap(app: App) -> None:
    try:
        await app.setup()
    except BaseException:
        try:
            await app.shutdown()
        except Exception:
            log.exception("cleanup after failed setup crashed")
        raise
    await app.run()
