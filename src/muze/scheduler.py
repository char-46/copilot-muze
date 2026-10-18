"""Scheduled tasks: fire prompts at the agent and deliver results to the
controller's control conversation.

Examples (natural language, the agent converts these via the schedule_task
tool): "两小时后给我发未读摘要" / "每 2 小时总结一次群 123" / "明早 9 点提醒我 X".

Persistence & process decoupling: task lifetime lives in the scheduled_tasks
table behind a claim/lease protocol, NOT in serve-process memory:
- firing = atomic DB claim (status 'running' + lease_until), so a second
  live scheduler sharing the DB cannot double-fire a task;
- a restart/shutdown cancels in-flight runs and RESETS their rows to
  due-now — the successor process re-runs them from scratch;
- a hard death (power loss, kill) leaves the row 'running'; the next
  process recovers expired leases at startup (or the tick claims them
  once they expire), again re-running from scratch;
- runs execute concurrently (the dawn briefing no longer queues behind a
  multi-minute nightly digest).
"""

from __future__ import annotations

import asyncio
import logging

from muze.chatlog import ChatLog, epoch_iso
from muze.core.service import ControlContext, ControlService, is_llm_invocation
from muze.store.repo import Repository

log = logging.getLogger("muze.scheduler")

TICK_SECONDS = 20
LEASE_SECONDS = 15 * 60     # claim lifetime; > longest expected task run
RETRY_SECONDS = 5 * 60      # interval task transient failure -> retry delay


class Scheduler:
    def __init__(self, repo: Repository, service: ControlService, chatlog: ChatLog) -> None:
        self.repo = repo
        self.service = service
        self.chatlog = chatlog
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._inflight: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------- control
    async def start(self) -> None:
        # adopt tasks orphaned by a previous process (power loss / hard
        # kill / pre-watchdog zombie): re-run them from scratch
        recovered = await self.repo.recover_stale_running()
        for row in recovered:
            log.warning(
                "task #%s was mid-run when its process died (lease expired); re-firing from scratch",
                row["id"],
            )
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        # cancel in-flight runs and hand their rows straight back to the
        # pool as due-NOW: the successor process (daemon relaunch after
        # /restart, or the user's start.ps1) picks them up and re-runs
        # them from the beginning — restart must not lose a task
        import time

        for task_id, runner in list(self._inflight.items()):
            if runner.done():
                continue  # finished cleanly; execute() already rescheduled it
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):
                pass
            if not runner.cancelled():
                # completed in the cancellation window: its own finish path
                # has run — resetting would duplicate the task
                continue
            await self.repo.reset_task(task_id, int(time.time()))
            log.info(
                "task #%s interrupted by shutdown; reset to due-now (will re-run from scratch)",
                task_id,
            )
        self._inflight.clear()

    async def drain(self) -> None:
        """Wait for all in-flight runs to finish (tests / graceful paths)."""
        runners = list(self._inflight.values())
        for r in runners:
            try:
                await r
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.sleep(TICK_SECONDS)
            except asyncio.CancelledError:
                return

    async def tick(self) -> int:
        rows = await self.repo.claim_due_tasks(LEASE_SECONDS)
        for row in rows:
            if row["id"] in self._inflight:
                continue
            runner = asyncio.create_task(self._run(row))
            self._inflight[row["id"]] = runner
            runner.add_done_callback(lambda _t, _id=row["id"]: self._inflight.pop(_id, None))
        return len(rows)

    async def _run(self, row: dict) -> None:
        try:
            await self.execute(row)
        except asyncio.CancelledError:
            raise  # stop() resets the row after awaiting us
        except Exception:
            log.exception("task #%s runner crashed; retrying from scratch", row["id"])
            import time

            await self.repo.reset_task(row["id"], int(time.time() + RETRY_SECONDS))

    # ------------------------------------------------------------- create
    def resolve_delivery(self, ctx: ControlContext) -> tuple[str, int]:
        """Pick the control conversation a task's result should go to."""
        if ctx.qq_reachable:
            return ctx.scene, ctx.peer_id
        cfg = self.service.cfg
        if cfg.control.control_group:
            return "group", cfg.control.control_group
        if cfg.control.controller:
            return "friend", cfg.control.controller[0]
        return "", 0

    async def create(
        self,
        *,
        prompt: str,
        at_ts: int | None = None,
        delay_sec: int | None = None,
        interval_sec: int | None = None,
        ctx: ControlContext | None = None,
        created_by: str = "agent",
        note: str = "",
    ) -> dict:
        import time

        if interval_sec:
            kind, next_at = "interval", int(time.time() + interval_sec)
        elif at_ts is not None:
            kind, next_at = "once", int(at_ts)
        elif delay_sec is not None:
            kind, next_at = "once", int(time.time() + delay_sec)
        else:
            raise ValueError("need one of at_ts / delay_sec / interval_sec")
        if next_at < time.time() - 5:
            raise ValueError("scheduled time is in the past")

        ctx = ctx or ControlContext(source="system", chat_key="system", scene="", peer_id=0)
        scene, peer = self.resolve_delivery(ctx)
        if not scene:
            raise ValueError("无可用的控制会话（请配置 control.controller 或 control.control_group）")

        task_id = await self.repo.create_task(
            kind, prompt, scene, peer, next_at, interval_sec, created_by, note
        )
        self.chatlog.log.info(
            "TASK create| #%s %s next=%s -> %s/%s | %s",
            task_id, kind, epoch_iso(next_at), scene, peer, prompt[:120],
        )
        return await self.repo.get_task(task_id)

    # ------------------------------------------------------------- builtin
    DIGEST_NOTE = "builtin:daily-digest"
    MORNING_NOTE = "builtin:morning-digest"

    async def ensure_daily_digest(self) -> int | None:
        """Idempotently create the nightly digest ("dream") task."""
        if not self.service.cfg.digest.enabled:
            return None
        rows = await self.repo.list_tasks()
        if any(r.get("note") == self.DIGEST_NOTE and r["status"] == "active" for r in rows):
            return None
        from muze.digest import next_run_for_hhmm

        cfg = self.service.cfg
        next_at = next_run_for_hhmm(cfg.digest.at)
        ctx = ControlContext(source="system", chat_key="system", scene="", peer_id=0)
        scene, peer = self.resolve_delivery(ctx)
        if not scene:
            log.warning("digest task not created: no control conversation configured")
            return None
        task_id = await self.repo.create_task(
            "interval",
            "/dream night",
            scene,
            peer,
            next_at,
            86400,
            "builtin",
            self.DIGEST_NOTE,
        )
        log.info("daily digest task #%s created, next run per cfg.digest.at", task_id)
        return task_id

    async def ensure_morning_digest(self) -> int | None:
        """Idempotently create the dawn briefing task. Delivery fans out to
        the morning audience (digest.morning_targets, or all controllers +
        the control group) instead of the task's single control row."""
        cfg = self.service.cfg
        if not (cfg.digest.enabled and cfg.digest.morning_enabled):
            return None
        rows = await self.repo.list_tasks()
        if any(r.get("note") == self.MORNING_NOTE and r["status"] == "active" for r in rows):
            return None
        from muze.digest import next_run_for_hhmm, resolve_morning_targets

        if not resolve_morning_targets(cfg):
            log.warning("morning briefing task not created: no delivery target")
            return None
        next_at = next_run_for_hhmm(cfg.digest.morning_at)
        ctx = ControlContext(source="system", chat_key="system", scene="", peer_id=0)
        scene, peer = self.resolve_delivery(ctx)
        task_id = await self.repo.create_task(
            "interval",
            "/dream morning",
            scene or "friend",
            peer,
            next_at,
            86400,
            "builtin",
            self.MORNING_NOTE,
        )
        log.info(
            "morning briefing task #%s created (at %s, fan-out delivery)",
            task_id, cfg.digest.morning_at,
        )
        return task_id

    # ------------------------------------------------------------- execute
    async def execute(self, row: dict) -> None:
        task_id = row["id"]
        prompt = row["prompt"]
        # Natural-language tasks (the documented norm for agent-made tasks)
        # must reach the AGENT, not the command parser: without this a
        # prompt like "重试语音转写：用 transcribe_voice ..." fell through
        # execute() as "neither command nor ::" and the help text got
        # delivered verbatim into the control chat.
        pfx = getattr(self.service.cfg.llm, "prefix", "::")
        is_cmd = prompt.startswith("/") or (prompt.startswith(".") and len(prompt) > 1)
        if not is_cmd and not is_llm_invocation(prompt, pfx):
            prompt = f"{pfx} {prompt}"
        log.info("task #%s firing: %s", task_id, prompt[:120])
        ctx = ControlContext(
            source="system",
            chat_key=f"task:{task_id}",
            scene=row["control_scene"],
            peer_id=row["control_peer_id"],
            user_label=f"scheduler#{task_id}",
            auto_confirm=False,
        )
        try:
            outcome = await self.service.execute(prompt, ctx)
            result_text = outcome.text or "(无输出)"
            if row.get("note") == self.MORNING_NOTE:
                # dawn briefing: fan out to the whole morning audience
                # (admins + admin groups) instead of the single control row
                sent, failed = await self.service.morning_broadcast(result_text)
                log.info(
                    "task #%s morning briefing delivered: %s ok, %s failed",
                    task_id, sent, failed,
                )
                result_text = f"晨报已推送 {sent} 个目标" + (f"（{failed} 个失败）" if failed else "")
            else:
                skip_deliver = (
                    row.get("note") == self.DIGEST_NOTE
                    and not self.service.cfg.digest.deliver
                )
                if not skip_deliver:
                    await self.service.deliver(ctx, outcome)
        except Exception as e:
            log.exception("task #%s execution failed", task_id)
            if row["kind"] == "interval":
                # transient failure (LLM hiccup, milky drop): an interval
                # task must NOT die — a single error used to flip the
                # nightly digest to status='error' forever. Retry soon.
                import time

                retry_at = int(time.time() + RETRY_SECONDS)
                await self.repo.finish_task(
                    task_id, "active", f"执行失败将重试: {e}", next_run_at=retry_at
                )
                log.info("task #%s interval failed; retry at %s", task_id, epoch_iso(retry_at))
            else:
                await self.repo.finish_task(task_id, "error", f"执行失败: {e}")
            return

        if row["kind"] == "interval":
            import time

            note = row.get("note") or ""
            if note in (self.DIGEST_NOTE, self.MORNING_NOTE):
                # re-anchor to the configured wall-clock slot instead of
                # fire_time + 24h: a missed slot (box powered off overnight,
                # live incident 2026-10-17/18) fires as a catch-up on boot
                # and would otherwise DRIFT to "boot time + 24h" forever;
                # long digest runtimes also no longer slide the slot
                from muze.digest import next_run_for_hhmm

                hhmm = (
                    self.service.cfg.digest.morning_at
                    if note == self.MORNING_NOTE
                    else self.service.cfg.digest.at
                )
                nxt = next_run_for_hhmm(hhmm)
            else:
                nxt = int(time.time() + int(row["interval_sec"] or 0))
            await self.repo.finish_task(task_id, "active", result_text, next_run_at=nxt)
            log.info("task #%s rescheduled to %s", task_id, epoch_iso(nxt))
        else:
            await self.repo.finish_task(task_id, "done", result_text)

    # -------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "running": bool(self._task and not self._task.done()),
            "inflight": sorted(self._inflight),
        }
