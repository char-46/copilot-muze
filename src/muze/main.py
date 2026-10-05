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
from muze.core.service import (
    ControlContext,
    ControlService,
    Runtime,
    CommandOutcome,
    is_llm_invocation,
    strip_llm_prefix,
)
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
    ReplySeg,
    TextSeg,
)
from muze.store.blobs import BlobStore
from muze.store.db import open_db
from muze.store.media import MediaStore
from muze.store.repo import Repository
from muze.version import get_version

log = logging.getLogger("muze.main")


def _ts(v: str | float | None) -> str:
    """Unix epoch (string or number) -> local 'YYYY-mm-dd HH:MM:SS', or '-'."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "-"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="muze serve", description="copilot-muze daemon")
    p.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    return p.parse_args(argv)


_EMBEDDER_CACHE: dict[tuple, object] = {}


def _shared_embedder(emb_cfg):
    """One model instance per unique embedding config: multi-account
    processes share the weights (RAM/VRAM) instead of loading one copy per
    account. The key covers everything that selects a backend/model."""
    from muze.ai.embeddings import Embedder

    key = (
        emb_cfg.provider, emb_cfg.backend, emb_cfg.model,
        emb_cfg.remote_model, emb_cfg.base_url, emb_cfg.api_key,
        emb_cfg.device,
    )
    inst = _EMBEDDER_CACHE.get(key)
    if inst is None:
        inst = Embedder(emb_cfg)
        _EMBEDDER_CACHE[key] = inst
    return inst


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
        self._sup_task = None
        self._last_seen_write = 0.0
        self._offline_since: float | None = None  # set on bot_offline, cleared on reconnect
        self._last_activity = time.time()  # in-memory clock for the RAG idle governor
        self.conn = None
        self.repo = None
        self.adapter = None
        self.media = None
        self.pipeline = None
        self.backfiller = None
        self.service = None
        self.account_dir: Path | None = None
        self.degraded = False  # booted from a remembered uin; protocol side down
        self._reconnect_task: asyncio.Task | None = None

    # ------------------------------------------------ remembered identity
    def _accounts_file(self) -> Path:
        return self.cfg.storage.data_dir / "accounts.json"

    def _remember_uin(self, uin: int) -> None:
        """Persist name -> uin so a later boot whose protocol side is down
        can still open THIS account's data dir and serve the console."""
        import json

        path = self._accounts_file()
        data: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if data.get(self.spec.name) == uin:
            return
        data[self.spec.name] = int(uin)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            log.exception("persist account uin failed")

    def _remembered_uin(self) -> int | None:
        import json

        try:
            data = json.loads(self._accounts_file().read_text(encoding="utf-8"))
            uin = data.get(self.spec.name)
            return int(uin) if uin else None
        except Exception:
            return None

    async def setup(self) -> None:
        cfg = self.cfg
        cfg.storage.data_dir.mkdir(parents=True, exist_ok=True)

        self.adapter = MilkyAdapter(self.spec.endpoint)
        login = None
        try:
            login = await self.adapter.start()
        except ProtocolError as e:
            uin = self._remembered_uin()
            if uin is None:
                raise  # first boot for this account: no local identity to fall back on
            self.degraded = True
            self.runtime.degraded = True
            log.warning(
                "[%s] 协议端暂不可达（%s）。以历史账号 %s 降级启动："
                "控制台/WebUI/本地数据可用，QQ 收发在协议端恢复后自动接上",
                self.spec.name, e, uin,
            )
        uin = login.user_id if login is not None else self._remembered_uin()
        self.runtime.self_id = uin
        if login is not None:
            self._remember_uin(uin)
            log.info("[%s] logged in as %s (%s)", self.spec.name, login.user_name or "?", uin)

        # per-account data dir is derived from the logged-in QQ number;
        # media bytes go to the shared app-level blob store (cross-account dedup)
        acct = cfg.storage.account_dir(uin)
        self.account_dir = acct
        # exactly one serve process per account directory: a second writer on
        # the same muze.db shows up as SQLITE_BUSY storms on the ingest path
        from muze import instlock

        _lock_path = acct / "serve.lock"
        self._instlock = instlock.acquire(_lock_path, timeout=20.0)
        if self._instlock is None:
            raise RuntimeError(
                f"账号数据目录 {acct} 已被另一个 muze 进程占用"
                f"{instlock.describe_holder(None, _lock_path)}；"
                "若确认没有其他实例在跑，删除 serve.lock 后重试。"
            )
        # per-process DB pool keyed by resolved path: when two account
        # instances log into the SAME QQ (e.g. one QQ behind two Milky
        # endpoints) they resolve to the same muze.db — two separate
        # connections writing one file fight each other with SQLITE_BUSY,
        # so they share ONE serialized aiosqlite connection instead.
        db_path = str(acct / "muze.db")
        entry = _DB_POOL.get(db_path)
        if entry is not None:
            entry["refs"] += 1
            self.conn = entry["conn"]
        else:
            self.conn = await open_db(db_path)
            _DB_POOL[db_path] = {"conn": self.conn, "refs": 1}
        self._db_path = db_path
        self.repo = Repository(self.conn)

        # multi-account isolation: one data dir belongs to exactly one account
        conflict = await self.repo.bind_owner(uin)
        if conflict is not None:
            raise RuntimeError(
                f"数据目录 {acct} 已绑定账号 {conflict}，"
                f"当前登录的是 {uin}。请勿手工挪用账号数据目录。"
            )
        if login is not None:
            await self._record_login()  # degraded boot records it on reconnect

        self.blobstore = BlobStore(cfg.storage.data_dir / "blobs")
        self.media = MediaStore(
            self.blobstore, self.adapter, self.repo,
            transcode_silk=cfg.ingest.media_transcode_silk,
            qq_cache_dir=cfg.ingest.qq_cache_dir,
        )
        self.pipeline = IngestPipeline(
            self.adapter, self.repo, self.media, cfg, self.chatlog, uin
        )
        self.backfiller = Backfiller(self.adapter, self.repo, cfg, self.pipeline, self.chatlog)
        self.runtime.backfiller = self.backfiller

        self.service = ControlService(cfg, self.adapter, self.repo, self.chatlog, self.runtime)
        self.llm = LLMClient(cfg.llm)
        self.agent = Agent(
            self.service, self.repo, self.llm, self.chatlog, cfg.llm.agent_max_steps
        )
        self.service.agent = self.agent

        # vector RAG (summaries + optional chat windows) + identity profiles
        from muze.ai.embeddings import Embedder
        from muze.rag import RagIndexer, VecStore
        from muze.rag_resource import ResourceGovernor

        self.vec = VecStore(self.repo)
        await self.vec.setup()
        emb = cfg.rag.embedding
        if not emb.base_url:
            emb.base_url = cfg.llm.chat.base_url
        if not emb.api_key:
            emb.api_key = cfg.llm.chat.api_key
        self.embedder = _shared_embedder(emb)
        self.governor = ResourceGovernor(cfg.rag)
        self.rag = RagIndexer(
            self.repo, self.vec, self.embedder, cfg,
            governor=self.governor, activity_fn=lambda: self._last_activity,
        )
        self.runtime.rag = self.rag
        self._last_activity = time.time()
        self._last_profile_refresh = 0.0
        self._enrich_task = None
        self._vram_task = None

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
        await self.scheduler.ensure_morning_digest()

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

    async def _record_login(self) -> None:
        """Persist this session's start; log the previous online window so the
        downtime (and thus what backfill may need to recover) is visible."""
        prev_login = await self.repo.get_meta("login_at")
        prev_seen = await self.repo.get_meta("last_seen_at")
        prev_shutdown = await self.repo.get_meta("last_shutdown_at")
        prev_offline = await self.repo.get_meta("last_offline_at")
        if prev_login or prev_seen:
            forced = bool(prev_offline and (not prev_shutdown or prev_offline > prev_shutdown))
            log.info(
                "[%s] 上次在线: %s ~ %s%s%s；本次启动会漫游补齐离线期间的消息",
                self.spec.name, _ts(prev_login), _ts(prev_seen or prev_shutdown or prev_offline),
                f"（正常关闭于 {_ts(prev_shutdown)}）" if prev_shutdown else "",
                "（上次为掉线/被强制下线）" if forced else "",
            )
        now = time.time()
        self._last_seen_write = now
        await self.repo.set_meta("login_at", str(now))
        await self.repo.set_meta("last_seen_at", str(now))

    async def _touch_seen(self) -> None:
        """Record 'last operation' time, throttled to one write per minute."""
        now = time.time()
        self._last_activity = now  # in-memory idle clock for the RAG governor
        if now - self._last_seen_write < 60:
            return
        self._last_seen_write = now
        try:
            await self.repo.set_meta("last_seen_at", str(now))
        except Exception:
            log.exception("last_seen update failed")

    # ---------------------------------------------------- online / offline
    async def _on_bot_offline(self, reason: str) -> None:
        """The account was kicked / the socket dropped: record the gap start."""
        if self.degraded:
            return  # already known offline since boot; reconnect loop owns it
        now = time.time()
        self._offline_since = now
        try:
            await self.repo.set_meta("last_offline_at", str(now))
            await self.repo.set_meta("last_offline_reason", str(reason))
        except Exception:
            log.exception("record offline failed")
        log.warning("[%s] 掉线/被下线: %s（重连后自动补齐漏掉的消息）", self.spec.name, reason)
        await self._notify(f"⚠ 账号掉线/被下线：{reason}\n重连后会自动补拉漏掉的消息。")

    async def _on_bot_online(self) -> "asyncio.Task | None":
        """Connection (re)established. If we were offline long enough, kick off
        a catch-up pull immediately instead of waiting for the next rescan."""
        now = time.time()
        started = self._offline_since
        self._offline_since = None
        try:
            await self.repo.set_meta("last_online_at", str(now))
        except Exception:
            log.exception("record online failed")
        if started is None:
            return None  # first connect this process; on_start backfill covers it
        gap = max(0.0, now - started)
        if gap < float(self.cfg.ingest.backfill.reconnect_gap_sec):
            log.info("[%s] reconnected after %.0fs (below gap threshold)", self.spec.name, gap)
            return None
        log.info(
            "[%s] reconnected after %.0fs: catching up missed messages",
            self.spec.name, gap,
        )
        return asyncio.create_task(self._catch_up(gap))

    async def _catch_up(self, gap: float) -> None:
        """Incremental roaming pull to recover messages missed while offline."""
        if self.backfiller is None or self.backfiller.running:
            return
        try:
            targets = await self.backfiller.target_conversations()
            n = await self.backfiller.run_once(targets)
        except Exception:
            log.exception("catch-up backfill failed")
            return
        if n is None:
            return
        from muze.timeparse import human_duration

        human = human_duration(int(gap)) if gap >= 60 else f"{int(gap)} 秒"
        log.info("[%s] catch-up done: +%s messages (offline %s)", self.spec.name, n, human)
        await self._notify(f"🔁 重连补齐完成：离线 {human}，新增 {n} 条消息。")

    async def _notify(self, text: str) -> None:
        if self.service is None:
            return
        try:
            await self.service.notify_controller(text)
        except Exception:
            log.exception("offline/online notify failed")

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
        if getattr(self, "agent", None) is not None:
            await self.agent.load_sessions()  # restore suspended/continued turns
        if not self.degraded:
            await self.backfiller.start_background()
        else:
            # protocol side down at boot: roaming pulls would only error out;
            # the reconnect loop starts them once the login succeeds
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        self._mt_task = asyncio.create_task(self._maintenance_loop())
        self._enrich_task = asyncio.create_task(self._startup_enrich())
        self._rag_task = asyncio.create_task(self._rag_loop())
        self._vram_task = asyncio.create_task(self._vram_steward_loop())
        if os.environ.get("MUZE_SUPERVISOR_PID"):
            self._sup_task = asyncio.create_task(self._supervisor_watch())
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

    RECONNECT_DELAY_START = 10.0
    RECONNECT_DELAY_MAX = 60.0

    async def _reconnect_loop(self) -> None:
        """Degraded boot: keep retrying the protocol side (console and webui
        stay alive meanwhile — that's the point). On success, finish the
        startup steps that need a live connection: caps probe + login record
        (inside adapter.start/_record_login) and the roaming backfill."""
        delay = self.RECONNECT_DELAY_START
        while not self._stop.is_set():
            try:
                login = await self.adapter.start()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info(
                    "[%s] 协议端仍不可达（%s）；%.0fs 后重试", self.spec.name, e, delay
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return  # stop set
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, self.RECONNECT_DELAY_MAX)
                continue
            self.runtime.self_id = login.user_id
            self.degraded = False
            self.runtime.degraded = False
            self._remember_uin(login.user_id)
            log.info(
                "[%s] 协议端恢复，已登录 %s (%s)", self.spec.name,
                login.user_name or "?", login.user_id,
            )
            await self._record_login()
            await self.backfiller.start_background()
            await self._notify("✅ 协议端已恢复连接，漫游补拉已启动。")
            return

    async def _supervisor_watch(self) -> None:
        """When launched by `muze daemon`, exit if the daemon dies so we never
        linger as an orphan holding the web port / DB."""
        try:
            pid = int(os.environ.get("MUZE_SUPERVISOR_PID", "0") or 0)
        except ValueError:
            return
        if pid <= 0:
            return
        while not self._stop.is_set():
            await asyncio.sleep(5)
            if not _parent_alive(pid):
                log.warning("supervisor pid %s is gone; exiting orphaned serve", pid)
                self._request_stop()
                return

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
            await self._touch_seen()
            if ev.event_type == "message_receive" and isinstance(ev.data, MessageEventData):
                await self.on_message(ev.data)
            elif ev.event_type == "message_recall" and isinstance(ev.data, MessageRecallData):
                await self.pipeline.handle_recall(ev.data)
            elif ev.event_type == "bot_offline":
                await self._on_bot_offline(str(getattr(ev.data, "reason", "?") or "?"))
            elif ev.event_type == "bot_online":
                await self._on_bot_online()
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
        reply_seq = next(
            (s.message_seq for s in msg.segments if isinstance(s, ReplySeg) and s.message_seq),
            0,
        )
        ctx = ControlContext(
            source="qq",
            chat_key=f"qq:{msg.message_scene}:{msg.peer_id}",
            scene=msg.message_scene,
            peer_id=msg.peer_id,
            user_label=await self.pipeline.sender_display(msg),
            auto_confirm=False,
            reply_seq=reply_seq,
        )
        # `::` turns run ASYNC: acknowledge with a session id right away and
        # let the background task deliver the answer, so one slow LLM reply
        # never blocks other conversations (per-chat serialization inside).
        if is_llm_invocation(text) and self.service.agent is not None:
            session = self.service.agent.submit(strip_llm_prefix(text), ctx)
            await svc.deliver(
                ctx,
                CommandOutcome(f"#{session.id} 已受理；/sess {session.id} 查看进度"),
            )
            return
        outcome = await svc.execute(text, ctx)
        await svc.deliver(ctx, outcome)

    # --------------------------------------------------------- maintenance
    async def _startup_enrich(self) -> None:
        """After the startup backfill has had a moment, build identity docs and
        index summaries for semantic retrieval."""
        from muze.profiles import refresh_profiles

        try:
            await asyncio.sleep(20)
            if self._stop.is_set():
                return
            # LLM turns that were in flight when the process died resume
            # first (user-visible continuation), then the slower enrichment
            if getattr(self, "agent", None) is not None:
                n = await self.agent.resume_interrupted()
                if n:
                    log.info("[%s] resumed %s interrupted LLM session(s)", self.spec.name, n)
            if getattr(self.cfg.profiles, "auto", True) and self.llm.configured:
                n = await refresh_profiles(self.repo, self.llm, self.cfg, self.chatlog)
                self._last_profile_refresh = time.time()
                if n:
                    log.info("[%s] profiles refreshed: %s", self.spec.name, n)
            if getattr(self, "rag", None) is not None and self.rag.enabled:
                n = await self.rag.run_once()
                if n:
                    log.info("[%s] RAG indexed %s docs", self.spec.name, n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("startup enrichment failed")

    async def _rag_loop(self) -> None:
        """Dedicated indexing cadence (rag.index_interval_min, default 5 min).
        Each pass is still governor-gated and interruptible, so an aggressive
        cadence only means the idle window is caught sooner."""
        while not self._stop.is_set():
            interval = max(30.0, float(self.cfg.rag.index_interval_min) * 60)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # stop set
            except asyncio.TimeoutError:
                pass
            if getattr(self, "rag", None) is None or not self.rag.enabled:
                continue
            try:
                n = await self.rag.run_once()
                if n:
                    log.info("[%s] RAG indexed %s docs", self.spec.name, n)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("rag indexing pass failed")

    async def _vram_steward_loop(self) -> None:
        """VRAM stewardship, every 60s, OUTSIDE any idle gate: while the user
        games (busy machine, indexing skipped) a GPU-resident embedding model
        must still be evicted to RAM the moment system VRAM pressure spikes —
        and promoted back once the GPU is comfortable again."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                pass
            rag = getattr(self, "rag", None)
            if rag is None or not rag.enabled or rag.embedder is None:
                continue
            try:
                await rag.embedder.evict_if_vram_pressure(rag.governor)
                await rag.embedder.promote_to_gpu_if_fits(rag.governor)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vram steward pass failed")

    async def _maintenance_loop(self) -> None:
        from muze.profiles import refresh_profiles

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
                now = time.time()
                if getattr(self.cfg.profiles, "auto", True) and self.llm.configured:
                    if now - self._last_profile_refresh >= self.cfg.profiles.interval_min * 60:
                        self._last_profile_refresh = now
                        await refresh_profiles(self.repo, self.llm, self.cfg, self.chatlog)
            except Exception:
                log.exception("maintenance cycle failed")
            await self._touch_seen()  # keep the last-operation time fresh

    async def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return  # idempotent: run()'s finally and the bootstrap both call it
        self._shutdown_done = True
        log.info("[%s] shutting down...", self.spec.name)
        self._stop.set()
        try:
            await self.repo.set_meta("last_shutdown_at", str(time.time()))
        except Exception:
            pass

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
        if getattr(self, "_reconnect_task", None) is not None:
            self._reconnect_task.cancel()
        if getattr(self, "_enrich_task", None) is not None:
            self._enrich_task.cancel()
        if getattr(self, "_rag_task", None) is not None:
            self._rag_task.cancel()
        if getattr(self, "_vram_task", None) is not None:
            self._vram_task.cancel()
        rf = getattr(self.runtime, "rag_force", None)
        if rf is not None and not rf.done():
            rf.cancel()
        rs = getattr(self.runtime, "rag_stop", None)
        if rs is not None:
            rs.set()
        scheduler = getattr(self, "scheduler", None)
        if scheduler is not None:
            await _safe("scheduler.stop", scheduler.stop(), 5)
        if self.backfiller is not None:
            await _safe("backfiller.stop", self.backfiller.stop(), 5)
        if self.pipeline is not None:
            await _safe("drain_media", self.pipeline.drain_media(timeout=5), 7)
        if getattr(self, "agent", None) is not None:
            await _safe("agent.aclose", self.agent.aclose(), 5)
        if self.adapter is not None:
            await _safe("adapter.close", self.adapter.close(), 5)
        if self.conn is not None:
            # pooled connection: close only when the last sharer is gone
            entry = _DB_POOL.get(getattr(self, "_db_path", ""))
            if entry is not None:
                entry["refs"] -= 1
                if entry["refs"] > 0:
                    self.conn = None  # someone else still uses it
                else:
                    _DB_POOL.pop(self._db_path, None)
            await _safe("conn.close", self.conn.close(), 5)


RESTART_EXIT = 100  # daemon contract: rc=100 -> relaunch serve
FORCE_EXIT_SECONDS = 10  # graceful shutdown deadline after Ctrl-C

# per-process shared DB connections (see App.startup): same QQ behind two
# endpoints must not open two competing writers to one muze.db
_DB_POOL: dict[str, dict] = {}


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


def _parent_alive(pid: int) -> bool:
    """Is process `pid` still running? (portable: parent-watchdog for serve)."""
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        k = ctypes.windll.kernel32
        h = k.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            # WAIT_OBJECT_0 (0) => the process object is signaled => it exited
            return k.WaitForSingleObject(h, 0) != 0
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _reexec() -> None:
    """Re-exec this process with the same serve args (loads new code)."""
    args = [sys.executable, "-m", "muze", *sys.argv[1:]]
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, args)
    except OSError:
        log.exception("re-exec failed; exiting with rc=%d", RESTART_EXIT)


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
        if os.environ.get("MUZE_SUPERVISED") == "1":
            log.info("restart requested (rc=%d); daemon will relaunch", RESTART_EXIT)
            return RESTART_EXIT
        log.info("restart requested; re-executing this process now")
        _reexec()
        return RESTART_EXIT  # only if exec failed
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
