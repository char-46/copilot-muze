"""Agent turn sessions: every LLM invocation gets a short id the user can
query while it runs (`/sess <id>`).

A session records the full trace of a turn — prompt, each model round (with
received character count), every tool call and a compact result preview —
plus live counters (last activity, output chars) that the in-turn heartbeat
uses to tell "still producing" from "stuck".

Sessions are in-memory only; the store keeps the most recent `cap` entries.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

RENDER_LIMIT = 2400  # chars, /sess output is delivered as one QQ message


@dataclass
class Session:
    id: str
    chat_key: str
    prompt: str
    created: float = field(default_factory=time.monotonic)
    status: str = "running"  # running | done | error
    events: list[tuple[float, str, str]] = field(default_factory=list)
    last_activity: float = 0.0
    round_chars: int = 0  # chars received in the current model round
    total_chars: int = 0
    result_preview: str = ""

    def __post_init__(self) -> None:
        self.last_activity = self.created
        self.event("prompt", self.prompt)

    # ------------------------------------------------------------- updates
    def event(self, kind: str, detail: str = "") -> None:
        self.events.append((time.monotonic() - self.created, kind, detail[:400]))
        self.last_activity = time.monotonic()

    def bump(self, chars: int) -> None:
        """A streamed chunk arrived — the model is alive and producing."""
        self.round_chars += chars
        self.total_chars += chars
        self.last_activity = time.monotonic()

    def new_round(self) -> None:
        self.round_chars = 0

    def finish(self, ok: bool, preview: str = "") -> None:
        self.status = "error" if not ok else "done"
        self.result_preview = (preview or "")[:200]
        self.event(self.status, self.result_preview)

    def elapsed(self) -> float:
        return time.monotonic() - self.created

    def idle(self) -> float:
        return time.monotonic() - self.last_activity

    # -------------------------------------------------------------- render
    def render(self) -> str:
        icon = {"running": "⏳", "done": "✔", "error": "✖"}.get(self.status, "?")
        head = (
            f"{icon} #{self.id} [{self.status}] {self.chat_key} "
            f"用时 {self.elapsed():.0f}s"
        )
        if self.status == "running":
            head += f"（本轮已收 {self.round_chars} 字，空闲 {self.idle():.0f}s）"
        lines = [head, f"问: {self.prompt[:80]}"]
        for t, kind, detail in self.events:
            if kind == "prompt":
                continue
            if kind == "llm":
                lines.append(f"{t:6.1f}s 模型轮: {detail or '(等待输出)'}")
            elif kind == "tool":
                lines.append(f"{t:6.1f}s 工具: {detail}")
            elif kind == "tool_result":
                lines.append(f"{t:6.1f}s 结果: {detail}")
            else:
                lines.append(f"{t:6.1f}s {kind}: {detail}")
        body = "\n".join(lines)
        if len(body) > RENDER_LIMIT:
            body = body[:RENDER_LIMIT] + "\n…(已截断)"
        return body


class SessionStore:
    def __init__(self, cap: int = 100) -> None:
        self.cap = cap
        self._sessions: dict[str, Session] = {}

    def create(self, chat_key: str, prompt: str) -> Session:
        for _ in range(8):
            sid = secrets.token_hex(3)  # 6 hex chars
            if sid not in self._sessions:
                break
        s = Session(id=sid, chat_key=chat_key, prompt=prompt)
        self._sessions[sid] = s
        self._prune()
        return s

    def get(self, sid: str) -> Session | None:
        sid = (sid or "").lstrip("#").strip()
        return self._sessions.get(sid)

    def recent(self, n: int = 8) -> list[Session]:
        return list(self._sessions.values())[-n:]

    def _prune(self) -> None:
        if len(self._sessions) <= self.cap:
            return
        done = [s for s in self._sessions.values() if s.status != "running"]
        done.sort(key=lambda s: s.created)  # oldest finished first
        for s in done:
            if len(self._sessions) <= self.cap:
                break
            self._sessions.pop(s.id, None)
