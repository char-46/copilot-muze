"""Roaming backfill: pull history (including already-read and self-sent
messages) from the protocol side until the stored watermark is covered.

This is the mechanism that makes the copilot resilient to downtime: messages
that arrived while offline are recovered from server-side roaming on startup
and on every periodic rescan.
"""

from __future__ import annotations

import asyncio
import logging
import time

from muze.chatlog import ChatLog, conv_id
from muze.config import MuzeConfig, is_control_conversation
from muze.ingest.pipeline import IngestPipeline
from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.store.repo import Repository

log = logging.getLogger("muze.backfill")


class Backfiller:
    def __init__(
        self,
        adapter: ProtocolAdapter,
        repo: Repository,
        cfg: MuzeConfig,
        pipeline: IngestPipeline,
        chatlog: ChatLog,
    ) -> None:
        self.adapter = adapter
        self.repo = repo
        self.cfg = cfg
        self.pipeline = pipeline
        self.chatlog = chatlog
        self.running = False
        self.last_run_at: float = 0.0
        self.last_new_messages = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._rescan_task: asyncio.Task | None = None

    # ------------------------------------------------------------ targets
    async def target_conversations(self) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()

        def add(scene: str, peer: int) -> None:
            if peer and (scene, peer) not in seen:
                seen.add((scene, peer))
                out.append((scene, peer))

        cfg = self.cfg
        if cfg.control.control_group:
            add("group", cfg.control.control_group)
        for u in cfg.authority:
            add("friend", u)

        if cfg.ingest.scope == "all":
            try:
                for g in await self.adapter.get_group_list():
                    add("group", g.group_id)
            except ProtocolError as e:
                log.warning("group list unavailable for backfill: %s", e)
            try:
                for f in await self.adapter.get_friend_list():
                    add("friend", f.user_id)
            except ProtocolError as e:
                log.warning("friend list unavailable for backfill: %s", e)
        else:
            for g in cfg.ingest.watchlist.groups:
                add("group", g)
            for f in cfg.ingest.watchlist.friends:
                add("friend", f)
        return out

    # ------------------------------------------------------------- worker
    async def _backfill_conversation(self, scene: str, peer_id: int) -> int:
        bf = self.cfg.ingest.backfill
        caps = self.adapter.caps
        if scene == "friend" and not caps.friend_history:
            return 0
        if scene == "group" and not caps.group_history:
            return 0

    async def _backfill_conversation(self, scene: str, peer_id: int, reporter=None) -> int:
        bf = self.cfg.ingest.backfill
        caps = self.adapter.caps
        if scene == "friend" and not caps.friend_history:
            return 0
        if scene == "group" and not caps.group_history:
            return 0

        watermark = await self.repo.get_watermark(scene, peer_id)
        total_new = 0
        pages = 0
        cursor: int | None = None

        while pages < bf.max_pages_per_conv and not self._stop.is_set():
            try:
                if caps.history_start_seq and cursor is not None:
                    res = await self.adapter.get_history_messages(
                        scene, peer_id, start_message_seq=cursor, limit=bf.page_limit
                    )
                else:
                    res = await self.adapter.get_history_messages(scene, peer_id, limit=bf.page_limit)
            except ProtocolError as e:
                log.warning("backfill %s failed: %s", conv_id(scene, peer_id), e)
                break

            pages += 1
            page_new = 0
            oldest = None
            for m in res.messages:  # ascending by message_seq
                oldest = m.message_seq if oldest is None else min(oldest, m.message_seq)
                if m.message_seq > watermark:
                    if await self.pipeline.handle_message(m, source="backfill"):
                        page_new += 1
            total_new += page_new
            if reporter is not None:
                reporter.bump(new=page_new, pages=1)
                await reporter.maybe_tick()

            # Stop conditions:
            #  - server says no more pages
            #  - we covered the watermark (everything older is already stored)
            #  - cursor stopped moving (safety against implementation quirks)
            if res.next_message_seq is None:
                break
            if oldest is not None and oldest <= watermark:
                break
            if cursor is not None and res.next_message_seq >= cursor:
                break
            cursor = res.next_message_seq
            await asyncio.sleep(bf.page_delay)

        if total_new:
            log.info(
                "backfill %s +%s new (pages=%s, watermark was %s)",
                conv_id(scene, peer_id), total_new, pages, watermark,
            )
        return total_new

    async def run_once(
        self,
        targets: list[tuple[str, int]] | None = None,
        reporter=None,
    ) -> int:
        if self.running:
            log.info("backfill already running; skipping")
            return 0
        self.running = True
        started = time.time()
        new_total = 0
        try:
            targets = targets if targets is not None else await self.target_conversations()
            log.info("backfill start: %s conversations", len(targets))
            if reporter is not None:
                await reporter.report_start(f"{len(targets)} 个会话")
            for scene, peer in targets:
                if self._stop.is_set():
                    break
                try:
                    n = await self._backfill_conversation(scene, peer, reporter=reporter)
                    new_total += n
                    if reporter is not None:
                        reporter.bump(new=0, pages=0, convs=1)
                        await reporter.maybe_tick()
                except Exception as e:
                    log.warning("backfill conv %s crashed: %s", conv_id(scene, peer), e)
                await asyncio.sleep(self.cfg.ingest.backfill.conv_delay)
            self.last_new_messages = new_total
            self.last_run_at = time.time()
            log.info(
                "backfill done: %s new messages in %.1fs",
                new_total, self.last_run_at - started,
            )
            return new_total
        finally:
            self.running = False

    # ---------------------------------------------------------- coverage
    async def probe_coverage(self, scene: str, peer_id: int, pages: int = 10) -> dict:
        """Read-only probe of how deep server-side roaming goes.

        Walks `pages` pages newest->older without storing anything; reports
        the oldest seq seen, whether the roaming end was reached, and how
        many messages the server returned (seqs are per-conversation and
        roughly 1:1 with messages, so gaps can be estimated from seq diff).
        """
        bf = self.cfg.ingest.backfill
        caps = self.adapter.caps
        if scene == "friend" and not caps.friend_history:
            return {"error": "protocol side has no friend roaming"}
        if scene == "group" and not caps.group_history:
            return {"error": "protocol side has no group roaming"}

        cursor: int | None = None
        oldest: int | None = None
        newest: int | None = None
        seen = 0
        fetched = 0
        reached_end = False
        while fetched < pages and not self._stop.is_set():
            try:
                if caps.history_start_seq and cursor is not None:
                    res = await self.adapter.get_history_messages(
                        scene, peer_id, start_message_seq=cursor, limit=bf.page_limit
                    )
                else:
                    res = await self.adapter.get_history_messages(scene, peer_id, limit=bf.page_limit)
            except ProtocolError as e:
                return {"error": str(e)}
            fetched += 1
            for m in res.messages:
                oldest = m.message_seq if oldest is None else min(oldest, m.message_seq)
                newest = m.message_seq if newest is None else max(newest, m.message_seq)
                seen += 1
            if res.next_message_seq is None:
                reached_end = True
                break
            if cursor is not None and res.next_message_seq >= cursor:
                break  # pager cannot go further on this protocol side
            cursor = res.next_message_seq
            await asyncio.sleep(bf.page_delay)
        return {
            "pages": fetched,
            "seen": seen,
            "oldest_seq": oldest,
            "newest_seq": newest,
            "reached_end": reached_end,
            "paged": caps.history_start_seq,
        }

    async def deep_pull(self, scene: str, peer_id: int, pages: int, reporter=None) -> int:
        """Ignore the watermark and pull up to `pages` pages into the store."""
        bf = self.cfg.ingest.backfill
        caps = self.adapter.caps
        cursor: int | None = None
        new = 0
        fetched = 0
        while fetched < pages and not self._stop.is_set():
            try:
                if caps.history_start_seq and cursor is not None:
                    res = await self.adapter.get_history_messages(
                        scene, peer_id, start_message_seq=cursor, limit=bf.page_limit
                    )
                else:
                    res = await self.adapter.get_history_messages(scene, peer_id, limit=bf.page_limit)
            except ProtocolError as e:
                log.warning("deep pull %s failed: %s", conv_id(scene, peer_id), e)
                break
            fetched += 1
            page_new = 0
            for m in res.messages:
                if await self.pipeline.handle_message(m, source="backfill"):
                    page_new += 1
            new += page_new
            if reporter is not None:
                reporter.bump(new=page_new, pages=1)
                await reporter.maybe_tick()
            if res.next_message_seq is None:
                break
            if cursor is not None and res.next_message_seq >= cursor:
                break
            cursor = res.next_message_seq
            await asyncio.sleep(bf.page_delay)
        log.info("deep pull %s: %s new in %s pages", conv_id(scene, peer_id), new, fetched)
        return new

    # -------------------------------------------------------- background
    async def start_background(self) -> None:
        bf = self.cfg.ingest.backfill
        if bf.on_start:
            self._task = asyncio.create_task(self.run_once())
        if bf.rescan_interval_min > 0:
            self._rescan_task = asyncio.create_task(self._rescan_loop())

    async def _rescan_loop(self) -> None:
        bf = self.cfg.ingest.backfill
        while not self._stop.is_set():
            await asyncio.sleep(bf.rescan_interval_min * 60)
            if self._stop.is_set():
                break
            try:
                targets = await self._rescan_targets()
                if targets:
                    await self.run_once(targets)
            except Exception as e:
                log.warning("rescan failed: %s", e)

    async def _rescan_targets(self) -> list[tuple[str, int]]:
        """Rescan recently-active conversations plus control conversations."""
        bf = self.cfg.ingest.backfill
        rows = await self.repo.list_conversations(active_within_h=bf.rescan_active_window_h)
        targets = [(r["scene"], r["peer_id"]) for r in rows]
        cfg = self.cfg
        if cfg.control.control_group:
            targets.append(("group", cfg.control.control_group))
        for u in cfg.authority:
            targets.append(("friend", u))
        return list(dict.fromkeys(targets))

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._task, self._rescan_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    def status(self) -> dict:
        return {
            "running": self.running,
            "last_run_at": self.last_run_at,
            "last_new_messages": self.last_new_messages,
        }
