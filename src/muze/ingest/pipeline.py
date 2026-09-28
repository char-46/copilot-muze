"""Realtime ingest pipeline: event message -> DB row -> media download -> log."""

from __future__ import annotations

import asyncio
import logging

from muze.chatlog import ChatLog, plain_text
from muze.config import MuzeConfig, describe_watch
from muze.provider.base import ProtocolAdapter
from muze.provider.milky.models import IncomingMessage, MessageRecallData
from muze.store.media import MediaStore
from muze.store.repo import Repository

log = logging.getLogger("muze.ingest")


class IngestPipeline:
    def __init__(
        self,
        adapter: ProtocolAdapter,
        repo: Repository,
        media: MediaStore,
        cfg: MuzeConfig,
        chatlog: ChatLog,
        self_id: int,
    ) -> None:
        self.adapter = adapter
        self.repo = repo
        self.media = media
        self.cfg = cfg
        self.chatlog = chatlog
        self.self_id = self_id
        self._sem = asyncio.Semaphore(max(1, cfg.ingest.media_download_concurrency))
        self._tasks: set[asyncio.Task] = set()
        self.processed_realtime = 0
        self.processed_backfill = 0

    # ------------------------------------------------------------- helpers
    async def sender_display(self, msg: IncomingMessage) -> str:
        if msg.sender_id == self.self_id:
            return "我"
        try:
            if msg.message_scene == "friend":
                name = self.adapter.cached_friend_name(msg.sender_id)
                if name:
                    return name
                return await self.adapter.get_friend_name(msg.sender_id)
            if msg.message_scene == "group":
                name = await self.adapter.get_group_member_name(msg.peer_id, msg.sender_id)
                if name:
                    return name
                friend = self.adapter.cached_friend_name(msg.sender_id)
                return friend or ""
        except Exception:
            return ""
        return ""

    def _queue_media(self, media_id: int | None, prefer_url: str | None) -> None:
        if media_id is None:
            return

        async def run() -> None:
            async with self._sem:
                try:
                    row = await self.repo.media_by_id(media_id)
                    if row is None or row["status"] in ("done", "skipped"):
                        return
                    await self.media.download_row(row, prefer_url=prefer_url)
                except Exception as e:
                    log.warning("media task #%s crashed: %s", media_id, e)

        t = asyncio.create_task(run())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # --------------------------------------------------------------- ingest
    async def handle_message(self, msg: IncomingMessage, source: str = "event") -> bool:
        """Store one message (dedup), log it, schedule media. True if new."""
        if not describe_watch(self.cfg, msg.message_scene, msg.peer_id):
            return False
        name = await self.sender_display(msg)
        text = plain_text(msg.segments)
        inserted = await self.repo.upsert_message(msg, text, source, sender_name=name)
        if not inserted:
            return False

        if source == "backfill":
            self.processed_backfill += 1
            self.chatlog.backfill(msg, name)
        else:
            self.processed_realtime += 1
            self.chatlog.recv(msg, name)

        prefer: str | None = None
        for i, seg in enumerate(msg.segments):
            kind = self.media.segment_kind(seg)
            if kind is None:
                continue
            if source == "event":
                prefer = getattr(seg, "temp_url", "") or None
            else:
                prefer = None  # roaming temp urls are stale; resolve fresh
            mid = await self.media.register(
                msg.message_scene, msg.peer_id, msg.message_seq, i, seg
            )
            self._queue_media(mid, prefer)
        return True

    async def handle_recall(self, data: MessageRecallData) -> None:
        ok = await self.repo.set_recalled(data.message_scene, data.peer_id, data.message_seq)
        if ok:
            self.chatlog.log.info(
                "CHAT recall| %s/%s seq=%s by %s %s",
                data.message_scene,
                data.peer_id,
                data.message_seq,
                data.operator_id,
                data.display_suffix,
            )

    async def drain_media(self, timeout: float = 10.0) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout)
