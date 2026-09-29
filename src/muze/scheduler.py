"""Scheduled tasks: fire prompts at the agent and deliver results to the
controller's control conversation.

Examples (natural language, the agent converts these via the schedule_task
tool): "两小时后给我发未读摘要" / "每 2 小时总结一次群 123" / "明早 9 点提醒我 X".

Persistence: scheduled_tasks table; the tick loop survives restarts.
"""

from __future__ import annotations

import asyncio
import logging

from muze.chatlog import ChatLog, epoch_iso
from muze.core.service import ControlContext, ControlService
from muze.store.repo import Repository

log = logging.getLogger("muze.scheduler")

TICK_SECONDS = 20


class Scheduler:
    def __init__(self, repo: Repository, service: ControlService, chatlog: ChatLog) -> None:
        self.repo = repo
        self.service = service
        self.chatlog = chatlog
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- control
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
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
        rows = await self.repo.due_tasks()
        for row in rows:
            try:
                await self.execute(row)
            except Exception:
                log.exception("task #%s crashed", row["id"])
        return len(rows)

    # ------------------------------------------------------------- create
    def resolve_delivery(self, ctx: ControlContext) -> tuple[str, int]:
        """Pick the control conversation a task's result should go to."""
        if ctx.qq_reachable:
            return ctx.scene, ctx.peer_id
        cfg = self.service.cfg.control
        if cfg.control_group:
            return "group", cfg.control_group
        if cfg.admin_users:
            return "friend", cfg.admin_users[0]
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
            raise ValueError("无可用的控制会话（请配置 control.admin_users 或 control_group）")

        task_id = await self.repo.create_task(
            kind, prompt, scene, peer, next_at, interval_sec, created_by, note
        )
        self.chatlog.log.info(
            "TASK create| #%s %s next=%s -> %s/%s | %s",
            task_id, kind, epoch_iso(next_at), scene, peer, prompt[:120],
        )
        return await self.repo.get_task(task_id)

    # ------------------------------------------------------------- execute
    async def execute(self, row: dict) -> None:
        task_id = row["id"]
        log.info("task #%s firing: %s", task_id, row["prompt"][:120])
        ctx = ControlContext(
            source="system",
            chat_key=f"task:{task_id}",
            scene=row["control_scene"],
            peer_id=row["control_peer_id"],
            user_label=f"scheduler#{task_id}",
            auto_confirm=False,
        )
        try:
            outcome = await self.service.execute(row["prompt"], ctx)
            result_text = outcome.text or "(无输出)"
            await self.service.deliver(ctx, outcome)
        except Exception as e:
            log.exception("task #%s execution failed", task_id)
            await self.repo.finish_task(task_id, "error", f"执行失败: {e}")
            return

        if row["kind"] == "interval":
            import time

            nxt = int(time.time() + int(row["interval_sec"] or 0))
            await self.repo.finish_task(task_id, "active", result_text, next_run_at=nxt)
            log.info("task #%s rescheduled to %s", task_id, epoch_iso(nxt))
        else:
            await self.repo.finish_task(task_id, "done", result_text)

    # -------------------------------------------------------------- status
    def status(self) -> dict:
        return {"running": bool(self._task and not self._task.done())}
