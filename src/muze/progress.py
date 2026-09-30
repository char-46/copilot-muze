"""Progress reporting for long pulls (sync / deep pull).

Cadence (per user spec): immediate on start, then at minute 1, 2 and 3,
then every 5 minutes, and a final summary on completion. Progress lines are
delivered to the originating control conversation when QQ-reachable,
otherwise written to the log.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("muze.progress")


class ProgressReporter:
    FIRST_MILESTONES = (60, 120, 180)  # seconds
    PERIODIC = 300  # seconds, after the first three minutes

    def __init__(
        self,
        label: str,
        emit: Callable[[str], Awaitable[None]],
        now: Callable[[], float] | None = None,
    ) -> None:
        self.label = label
        self.emit = emit
        self.now = now or time.monotonic
        self.t0 = self.now()
        self.new_msgs = 0
        self.pages = 0
        self.convs_done = 0
        self._milestones = list(self.FIRST_MILESTONES)
        self._next_periodic = float(self.PERIODIC)
        self.started_msg_sent = False

    # ------------------------------------------------------------- update
    def bump(self, new: int = 0, pages: int = 0, convs: int = 0) -> None:
        self.new_msgs += new
        self.pages += pages
        self.convs_done += convs

    async def report_start(self, note: str = "") -> None:
        self.started_msg_sent = True
        text = f"▶ {self.label} 开始{('：' + note) if note else ''}"
        try:
            await self.emit(text)
        except Exception:
            log.exception("progress emit failed")

    async def maybe_tick(self) -> bool:
        """Emit a progress line if a cadence point has been reached."""
        elapsed = self._pop_due()
        if elapsed is None:
            return False
        text = (
            f"⏳ {self.label} 进行中（{int(elapsed // 60)} 分钟）："
            f"已翻 {self.pages} 页，新增 {self.new_msgs} 条"
            + (f"，完成会话 {self.convs_done} 个" if self.convs_done else "")
        )
        try:
            await self.emit(text)
        except Exception:
            log.exception("progress emit failed")
        return True

    async def finish(self, note: str) -> None:
        elapsed = self.now() - self.t0
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        text = (
            f"✔ {self.label} {note}：共 {mins}分{secs}秒，"
            f"{self.pages} 页，新增 {self.new_msgs} 条"
            + (f"，会话 {self.convs_done} 个" if self.convs_done else "")
        )
        try:
            await self.emit(text)
        except Exception:
            log.exception("progress emit failed")

    # -------------------------------------------------------------- timing
    def _pop_due(self) -> float | None:
        elapsed = self.now() - self.t0
        if self._milestones and elapsed >= self._milestones[0]:
            self._milestones.pop(0)
            return elapsed
        if not self._milestones and elapsed >= self._next_periodic:
            while self._next_periodic <= elapsed:
                self._next_periodic += self.PERIODIC
            return elapsed
        return None
