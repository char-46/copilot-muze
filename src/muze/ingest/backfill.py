"""Roaming backfill: pull history (including already-read and self-sent
messages) from the protocol side until the stored watermark is covered.

This is the mechanism that makes the copilot resilient to downtime: messages
that arrived while offline are recovered from server-side roaming on startup
and on every periodic rescan.

Watermarks (schema v8): each conversation keeps a high-water mark
(``last_seq`` = newest stored seq) plus a resumable deep-pull state
(``bf_cursor_seq`` + ``bf_target_seq``). A pull pages from newest down to
``target`` and persists progress after every page, so an interruption — crash,
page cap, restart — resumes exactly where it stopped instead of leaving a
silent hole below ``last_seq`` (which previously required ``/sync force``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import date as _date
from pathlib import Path

from muze.chatlog import ChatLog, conv_id
from muze.config import MuzeConfig, is_control_conversation
from muze.ingest.pipeline import IngestPipeline
from muze.progress import ProgressReporter
from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.store.repo import Repository

log = logging.getLogger("muze.backfill")

# ------------------------------------------------- anti-ban pacing (shared)
# History pulls are the main account-risk surface. Three shared guards:
#  - jitter: randomized page/conv delays (a metronome-exact rate is the
#    classic bot fingerprint)
#  - daily page budget: persisted in <data_dir>/pull_budget.json, shared by
#    every Backfiller over the store (and across restarts)
#  - risk backoff: consecutive failed pages — or an error that smells like
#    risk control (风控/冻结/frequency...) — pause ALL pulls process-wide,
#    doubling per additional trip up to a day
_RISK_WORDS = ("风控", "冻结", "frozen", "risk", "blocked", "too many", "频率", "频繁")
_FAIL_STREAK = 0
_RISK_UNTIL = 0.0
_RISK_NEXT_SEC = 0.0


def _jittered(base: float, jitter_pct: float) -> float:
    j = max(0.0, min(0.95, float(jitter_pct or 0)))
    return max(0.0, float(base) * (1.0 + random.uniform(-j, j)))


def _note_pull_success() -> None:
    global _FAIL_STREAK, _RISK_NEXT_SEC
    _FAIL_STREAK = 0
    _RISK_NEXT_SEC = 0.0


def _note_pull_failure(bf, err_text: str = "") -> float:
    """Register a failed page; returns the cooldown started (0 = none)."""
    global _FAIL_STREAK, _RISK_UNTIL, _RISK_NEXT_SEC
    _FAIL_STREAK += 1
    t = str(err_text or "").lower()
    risky = any(w in t for w in _RISK_WORDS)
    if risky or _FAIL_STREAK >= bf.fail_streak_limit:
        step = bf.risk_backoff_sec if _RISK_NEXT_SEC <= 0 else _RISK_NEXT_SEC * 2
        step = min(step, bf.risk_backoff_max_sec)
        _RISK_NEXT_SEC = step
        _RISK_UNTIL = time.monotonic() + step
        _FAIL_STREAK = 0
        return step
    return 0.0


def _risk_paused() -> tuple[bool, float]:
    left = _RISK_UNTIL - time.monotonic()
    return (left > 0.0, max(0.0, left))


async def _budget_take(data_dir: str, pages: int = 1) -> int:
    """Count `pages` against today's pull budget; returns today's total."""
    path = Path(data_dir) / "pull_budget.json"

    def _op() -> int:
        today = _date.today().isoformat()
        try:
            d = json.loads(path.read_text("utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        if d.get("date") != today:
            d = {"date": today, "pages": 0}
        d["pages"] = int(d.get("pages") or 0) + int(pages)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d), "utf-8")
        return d["pages"]

    return await asyncio.to_thread(_op)

# ------------------------------------------------------------------ dedup
# One QQ behind several endpoints (main + milky-2) shares ONE repo: several
# Backfillers may live in this process over the same data. Pulls are deduped:
#  - per-CONVERSATION claims: a second in-flight pull of the same conv skips
#    (rows would upsert-dedupe anyway; the claim saves the network pull and
#    keeps the saved bf cursor from being written by two pagers at once)
#  - per-DB run state: an automatic (background) full run skips when a
#    sibling backfiller over the SAME repo is already pulling or just ran
_PULL_CLAIMS: dict[tuple[str, int], float] = {}  # (scene, peer) -> expiry
_CLAIM_TTL = 600.0  # safety expiry: a crashed holder can't block forever
_RUN_STATE: dict[int, dict] = {}  # id(repo.conn) -> {"running", "last_run_at"}
_RUN_FRESH_SEC = 120.0  # a sibling's recent full run covers a new one


def _claim_pull(conv: tuple[str, int]) -> bool:
    """True when this caller may pull the conversation now."""
    now = time.monotonic()
    if _PULL_CLAIMS.get(conv, 0.0) > now:
        return False
    _PULL_CLAIMS[conv] = now + _CLAIM_TTL
    return True


def _release_pull(conv: tuple[str, int]) -> None:
    _PULL_CLAIMS.pop(conv, None)


def _run_state(conn: object) -> dict:
    return _RUN_STATE.setdefault(id(conn), {"running": False, "last_run_at": 0.0})


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
        self._retry_task: asyncio.Task | None = None
        self._reporter: ProgressReporter | None = None
        # auto-retry of interrupted pulls only once the background loop is
        # owned by the app (direct run_once callers manage their own retries)
        self._auto_retry = False
        # a pager that stops advancing gets a couple of human-paced retries
        # before we give up for now (resume state is kept either way)
        self.stall_backoff_sec = 2.0

    @property
    def initial_task(self) -> asyncio.Task | None:
        """The boot-time roaming pull (set by start_background). App awaits
        this before releasing the scheduler so due digests never read a DB
        that still lacks the messages missed while we were down."""
        return self._task

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
        for u in cfg.control.controller:
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
    async def _backfill_conversation(
        self,
        scene: str,
        peer_id: int,
        reporter=None,
        force: bool = False,
        since_ts: float | None = None,
    ) -> int:
        conv = (scene, peer_id)
        if not _claim_pull(conv):
            log.info(
                "backfill %s skipped: another pull of this conversation is "
                "in flight (shared-store dedup)",
                conv_id(scene, peer_id),
            )
            return 0
        try:
            return await self._backfill_conversation_inner(
                scene, peer_id, reporter=reporter, force=force, since_ts=since_ts
            )
        finally:
            _release_pull(conv)

    async def _backfill_conversation_inner(
        self,
        scene: str,
        peer_id: int,
        reporter=None,
        force: bool = False,
        since_ts: float | None = None,
    ) -> int:
        bf = self.cfg.ingest.backfill
        caps = self.adapter.caps
        if scene == "friend" and not caps.friend_history:
            return 0
        if scene == "group" and not caps.group_history:
            return 0

        watermark = await self.repo.get_watermark(scene, peer_id)
        # Multi-watermark pull, three modes:
        #   since_ts set -> /sync 2d: IGNORE the watermark, recover the window
        #   force        -> ignore the watermark, page to the roaming end
        #   otherwise    -> resume a saved pull, or incremental from last_seq
        since: float | None
        if since_ts is not None:
            target: int = 0
            cursor: int | None = None
            since = float(since_ts)
        elif force:
            target, cursor, since = 0, None, None
        else:
            resume = await self.repo.get_bf_state(scene, peer_id)
            if resume:
                target = int(resume.get("target") or 0)
                cursor = resume.get("cursor")
                since = resume.get("since")
                log.info(
                    "backfill %s resuming interrupted pull (cursor=%s target=%s since=%s)",
                    conv_id(scene, peer_id), cursor, target, since,
                )
            else:
                target, cursor, since = watermark, None, None
        stop_watermark = target
        time_bounded = since is not None
        total_new = 0
        pages = 0
        stalls = 0  # non-advancing cursor hits (retried, never "the end")
        done = False  # reached the target / window start / roaming/pager end

        # record intent up front so even a crash during the first page can be
        # resumed (otherwise the partial upserts raise last_seq and hide the hole)
        await self.repo.set_bf_state(
            scene, peer_id, cursor=cursor, target=stop_watermark, since=since
        )

        while pages < bf.max_pages_per_conv and not self._stop.is_set():
            paused, left = _risk_paused()
            if paused:
                log.warning(
                    "backfill %s skipped: risk-control backoff active (%.0fs left)",
                    conv_id(scene, peer_id), left,
                )
                break
            if bf.daily_page_budget > 0 and not force:
                used = await _budget_take(self.cfg.storage.data_dir, 1)
                if used > bf.daily_page_budget:
                    log.info(
                        "backfill %s stopped: daily page budget %s spent (%s used); "
                        "pulls resume tomorrow (manual /sync force bypasses)",
                        conv_id(scene, peer_id), bf.daily_page_budget, used,
                    )
                    break
            try:
                if caps.history_start_seq and cursor is not None:
                    res = await self.adapter.get_history_messages(
                        scene, peer_id, start_message_seq=cursor, limit=bf.page_limit
                    )
                else:
                    res = await self.adapter.get_history_messages(scene, peer_id, limit=bf.page_limit)
            except ProtocolError as e:
                step = _note_pull_failure(bf, str(e))
                if step:
                    log.warning(
                        "backfill %s failed (%s); ALL pulls pause %.0fs (risk backoff)",
                        conv_id(scene, peer_id), e, step,
                    )
                else:
                    log.warning("backfill %s failed: %s", conv_id(scene, peer_id), e)
                break
            _note_pull_success()

            pages += 1
            page_new = 0
            oldest = None
            oldest_time = None
            for m in res.messages:  # ascending by message_seq
                oldest = m.message_seq if oldest is None else min(oldest, m.message_seq)
                mt = m.time or 0
                if mt > 0 and (oldest_time is None or mt < oldest_time):
                    oldest_time = mt
                if time_bounded:
                    # window pull: keep everything in range (dedup handles the
                    # rest); an unknown timestamp (0) is kept rather than lost
                    keep = mt == 0 or mt >= since
                else:
                    keep = force or m.message_seq > stop_watermark
                if keep and await self.pipeline.handle_message(m, source="backfill"):
                    page_new += 1
            total_new += page_new
            if reporter is not None:
                reporter.bump(new=page_new, pages=1)
                await reporter.maybe_tick()

            # Stop conditions:
            #  - server says no more pages (roaming end)
            #  - time pull: page went past the window start
            #  - seq pull: paged down to the target
            #  - cursor stopped moving (safety against pager quirks)
            if res.next_message_seq is None:
                done = True
                break
            if time_bounded:
                if oldest_time is not None and oldest_time < since:
                    done = True
                    break
            elif oldest is not None and oldest <= stop_watermark:
                done = True
                break
            if cursor is not None and res.next_message_seq >= cursor:
                # NOT the end of roaming: the pager handed back a cursor that
                # does not advance (throttled/duplicated page). Treat it as an
                # interruption — retry a few times, then stop WITH the resume
                # state kept, so a later rescan/retry continues from here
                # instead of quietly assuming "we reached the top".
                stalls += 1
                if stalls >= 3:
                    log.warning(
                        "backfill %s pager stalled at cursor=%s after %s pages; "
                        "keeping resume state so a later retry continues",
                        conv_id(scene, peer_id), cursor, pages,
                    )
                    break
                log.info(
                    "backfill %s cursor did not advance (cursor=%s, page=%s); retrying",
                    conv_id(scene, peer_id), cursor, pages,
                )
                await asyncio.sleep(
                    _jittered(max(bf.page_delay, self.stall_backoff_sec), bf.jitter_pct)
                )
                continue

            cursor = res.next_message_seq
            # persist progress so an interruption never loses more than a page
            await self.repo.set_bf_state(
                scene, peer_id, cursor=cursor, target=stop_watermark, since=since
            )
            await asyncio.sleep(_jittered(bf.page_delay, bf.jitter_pct))

        if done:
            await self.repo.clear_bf_state(scene, peer_id)
        else:
            log.info(
                "backfill %s paused (page cap/interrupt) at %s pages; "
                "will resume (cursor=%s target=%s since=%s)",
                conv_id(scene, peer_id), pages, cursor, stop_watermark, since,
            )

        if total_new or force or time_bounded:
            log.info(
                "backfill %s +%s new (pages=%s, watermark was %s, target=%s, "
                "since=%s, force=%s)",
                conv_id(scene, peer_id), total_new, pages, watermark,
                stop_watermark, since, force,
            )
        return total_new

    async def run_once(
        self,
        targets: list[tuple[str, int]] | None = None,
        reporter: ProgressReporter | None = None,
        force: bool = False,
        since_ts: float | None = None,
        background: bool = False,
    ) -> int | None:
        st = _run_state(self.repo.conn)
        if background and not force and since_ts is None:
            # automatic runs (boot roam, periodic rescan) are shared-store:
            # skip when a sibling backfiller is pulling or just covered it
            now = time.monotonic()
            if st["running"]:
                log.info("backfill run skipped: sibling instance already pulling this store")
                return None
            if now - st["last_run_at"] < _RUN_FRESH_SEC:
                log.info(
                    "backfill run skipped: sibling ran %.0fs ago (coverage shared)",
                    now - st["last_run_at"],
                )
                return None
        if self.running:
            log.info("backfill already running; skipping")
            return None
        self.running = True
        st["running"] = True
        own_reporter = reporter is None
        if reporter is None:
            # Owned reporter: a background pull has no front to report to until
            # someone /syncs, at which point they subscribe (see subscribe()).
            tag = "（强制全量）" if force else ("（重拉近期）" if since_ts else "")
            reporter = ProgressReporter("全量补拉" + tag)
        self._reporter = reporter
        started = time.time()
        new_total = 0
        try:
            targets = targets if targets is not None else await self.target_conversations()
            log.info(
                "backfill start: %s conversations (force=%s since=%s)",
                len(targets), force, since_ts,
            )
            note = "（强制全量）" if force else ("（重拉近期）" if since_ts else "")
            await reporter.report_start(f"{len(targets)} 个会话{note}")

            concurrency = max(1, self.cfg.ingest.backfill.concurrency)
            queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
            for t in targets:
                queue.put_nowait(t)
            counts = {"new": 0}

            async def worker() -> None:
                while not self._stop.is_set():
                    try:
                        scene, peer = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        n = await self._backfill_conversation(
                            scene, peer, reporter=reporter, force=force, since_ts=since_ts
                        )
                        counts["new"] += n
                        reporter.bump(new=0, pages=0, convs=1)
                        await reporter.maybe_tick()
                    except Exception as e:
                        log.warning("backfill conv %s crashed: %s", conv_id(scene, peer), e)
                    await asyncio.sleep(
                        _jittered(
                            self.cfg.ingest.backfill.conv_delay,
                            self.cfg.ingest.backfill.jitter_pct,
                        )
                    )

            await asyncio.gather(*(worker() for _ in range(concurrency)))
            new_total = counts["new"]
            self.last_new_messages = new_total
            self.last_run_at = time.time()
            log.info(
                "backfill done: %s new messages in %.1fs (concurrency=%s)",
                new_total, self.last_run_at - started, concurrency,
            )
            if own_reporter:
                await reporter.finish(f"完成，新增 {new_total} 条")
            return new_total
        finally:
            self._reporter = None
            self.running = False
            st["running"] = False
            st["last_run_at"] = time.monotonic()
            await self._schedule_retry_if_interrupted()

    RETRY_SEC = 300.0  # unfinished pull retried ~5 min later (jittered ±50%)

    async def _schedule_retry_if_interrupted(self) -> None:
        """Interrupted pulls are not "done": if any conversation still has a
        resume state after this run, retry it soon instead of waiting for the
        periodic rescan (or never, if the process restarts)."""
        if self._stop.is_set():
            return
        if not getattr(self, "_auto_retry", False):
            return  # not started by the app (tests / manual runs)
        task = getattr(self, "_retry_task", None)
        if task is not None and not task.done():
            return  # a retry pass is already queued/running
        try:
            pending = await self.repo.pending_bf_conversations()
        except Exception:
            log.exception("backfill retry check failed")
            return
        if not pending:
            return
        log.info(
            "backfill: %s conversation(s) have an unfinished pull; retrying soon",
            len(pending),
        )
        self._retry_task = asyncio.create_task(self._retry_pending())

    async def _retry_pending(self) -> None:
        await asyncio.sleep(_jittered(self.RETRY_SEC, 0.5))
        if self._stop.is_set():
            return
        pending = await self.repo.pending_bf_conversations()
        if not pending:
            return
        log.info("backfill retry: resuming %s unfinished pull(s)", len(pending))
        # background=False: a manual-style targeted run must not be skipped
        # by the "a sibling ran recently" freshness guard
        await self.run_once(pending, background=False)

    def subscribe(self, emit, key: str | None = None) -> bool:
        """Attach a late delivery target to the currently-running pull.

        Used when /sync is issued while a backfill is already active: its
        periodic progress is forwarded to the new trigger. Subscribers are
        keyed by destination, so a repeated /sync from the same conversation
        replaces (not duplicates) its subscription. Returns True if there was
        a running pull to subscribe to.
        """
        r = self._reporter
        if not self.running or r is None:
            return False
        r.add_subscriber(emit, key=key)
        return True

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
            await asyncio.sleep(_jittered(bf.page_delay, bf.jitter_pct))
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
        conv = (scene, peer_id)
        if not _claim_pull(conv):
            log.info(
                "deep pull %s skipped: another pull of this conversation is "
                "in flight (shared-store dedup)",
                conv_id(scene, peer_id),
            )
            return 0
        try:
            return await self._deep_pull_inner(scene, peer_id, pages, reporter=reporter)
        finally:
            _release_pull(conv)

    async def _deep_pull_inner(self, scene: str, peer_id: int, pages: int, reporter=None) -> int:
        bf = self.cfg.ingest.backfill
        caps = self.adapter.caps
        cursor: int | None = None
        new = 0
        fetched = 0
        while fetched < pages and not self._stop.is_set():
            paused, left = _risk_paused()
            if paused:
                log.warning(
                    "deep pull %s stopped: risk-control backoff (%.0fs left)",
                    conv_id(scene, peer_id), left,
                )
                break
            try:
                if caps.history_start_seq and cursor is not None:
                    res = await self.adapter.get_history_messages(
                        scene, peer_id, start_message_seq=cursor, limit=bf.page_limit
                    )
                else:
                    res = await self.adapter.get_history_messages(scene, peer_id, limit=bf.page_limit)
            except ProtocolError as e:
                step = _note_pull_failure(bf, str(e))
                log.warning(
                    "deep pull %s failed: %s%s",
                    conv_id(scene, peer_id), e,
                    f" (risk backoff {step:.0}s)" if step else "",
                )
                break
            _note_pull_success()
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
            await asyncio.sleep(_jittered(bf.page_delay, bf.jitter_pct))
        log.info("deep pull %s: %s new in %s pages", conv_id(scene, peer_id), new, fetched)
        return new

    # -------------------------------------------------------- background
    async def start_background(self) -> None:
        bf = self.cfg.ingest.backfill
        self._auto_retry = True
        if bf.on_start:
            self._task = asyncio.create_task(self.run_once(background=True))
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
                    await self.run_once(targets, background=True)
            except Exception as e:
                log.warning("rescan failed: %s", e)

    async def _rescan_targets(self) -> list[tuple[str, int]]:
        """Rescan recently-active conversations plus control conversations,
        plus any conversation with an unfinished deep pull (hole to resume)."""
        bf = self.cfg.ingest.backfill
        rows = await self.repo.list_conversations(active_within_h=bf.rescan_active_window_h)
        targets = [(r["scene"], r["peer_id"]) for r in rows]
        targets.extend(await self.repo.pending_bf_conversations())
        cfg = self.cfg
        if cfg.control.control_group:
            targets.append(("group", cfg.control.control_group))
        for u in cfg.control.controller:
            targets.append(("friend", u))
        return list(dict.fromkeys(targets))

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._task, self._rescan_task, self._retry_task):
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
