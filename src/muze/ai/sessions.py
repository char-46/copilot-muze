"""Agent turn sessions: every LLM invocation gets a short id the user can
query (`/sess <id>`), list (`/sess list`) and continue (`/sess go [id]`,
`/sess <id> <msg>`).

A session records the full trace of a turn — prompt, each model round (with
received character count), every tool call and a compact result preview —
plus live counters (last activity, output chars) that the in-turn heartbeat
uses to tell "still producing" from "stuck".

Sessions are persisted (store table `ai_sessions`): a turn that stopped at the
step limit is `suspend` and can be resumed after a restart, and a finished
session can be continued with a new message. `messages` holds the tool-loop
messages (system prompt stripped) so `go` resumes exactly where it stopped.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field

RENDER_LIMIT = 2400  # chars, /sess output is delivered as one QQ message
RUNNING, DONE, ERROR, SUSPEND = "running", "done", "error", "suspend"
_RESUMABLE = (SUSPEND, ERROR)
_ICON = {RUNNING: "⏳", DONE: "✔", ERROR: "✖", SUSPEND: "⏸"}
_STATUS_CN = {RUNNING: "运行中", DONE: "完成", ERROR: "出错", SUSPEND: "已暂停"}


def heuristic_title(prompt: str, limit: int = 24) -> str:
    """Cheap title from the prompt (first line, trimmed) — shown immediately;
    an LLM pass may overwrite it later."""
    line = (prompt or "").strip().splitlines()[0] if (prompt or "").strip() else ""
    line = line.strip(" 。.!！?？")
    return line[:limit] if line else "(无标题)"


@dataclass
class Session:
    id: str
    chat_key: str
    prompt: str
    title: str = ""
    status: str = RUNNING
    step: int = 0
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    events: list[tuple[float, str, str]] = field(default_factory=list)
    last_activity: float = 0.0
    round_chars: int = 0  # chars received in the current model round
    total_chars: int = 0
    result_preview: str = ""
    messages: list[dict] = field(default_factory=list)  # loop msgs, system stripped

    def __post_init__(self) -> None:
        self.last_activity = self.created
        if not self.title:
            self.title = heuristic_title(self.prompt)
        if not self.events:
            self.event("prompt", self.prompt)

    # ------------------------------------------------------------- updates
    def event(self, kind: str, detail: str = "") -> None:
        now = time.time()
        self.events.append((now - self.created, kind, detail[:400]))
        self.last_activity = now
        self.updated = now

    def bump(self, chars: int) -> None:
        """A streamed chunk arrived — the model is alive and producing."""
        self.round_chars += chars
        self.total_chars += chars
        self.updated = self.last_activity = time.time()

    def new_round(self) -> None:
        self.round_chars = 0

    def finish(self, ok: bool, preview: str = "") -> None:
        self.status = DONE if ok else ERROR
        self.result_preview = (preview or "")[:200]
        self.event(self.status, self.result_preview)

    def suspend(self, messages: list[dict], preview: str = "") -> None:
        """Stopped at the step limit — keep the loop messages for /sess go."""
        self.status = SUSPEND
        self.messages = messages
        self.result_preview = (preview or "")[:200]
        self.event(SUSPEND, self.result_preview or f"第 {self.step} 轮暂停")

    @property
    def resumable(self) -> bool:
        return self.status in _RESUMABLE

    def elapsed(self) -> float:
        return max(0.0, time.time() - self.created)

    def idle(self) -> float:
        return max(0.0, time.time() - self.last_activity)

    # ------------------------------------------------------------- persist
    def to_row(self) -> dict:
        return {
            "id": self.id,
            "chat_key": self.chat_key,
            "prompt": self.prompt,
            "title": self.title,
            "status": self.status,
            "step": self.step,
            "created": self.created,
            "updated": self.updated,
            "result_preview": self.result_preview,
            "messages_json": json.dumps(self.messages, ensure_ascii=False),
            "events_json": json.dumps(self.events, ensure_ascii=False),
        }

    @classmethod
    def from_row(cls, row: dict) -> "Session":
        def _load(raw, default):
            try:
                return json.loads(raw) if raw else default
            except (ValueError, TypeError):
                return default

        return cls(
            id=row["id"],
            chat_key=row["chat_key"],
            prompt=row["prompt"],
            title=row.get("title", ""),
            status=row.get("status", DONE),
            step=int(row.get("step", 0) or 0),
            created=float(row.get("created", 0) or 0),
            updated=float(row.get("updated", 0) or 0),
            events=[tuple(e) for e in _load(row.get("events_json"), [])],
            last_activity=float(row.get("updated", 0) or 0),
            result_preview=row.get("result_preview", ""),
            messages=_load(row.get("messages_json"), []),
        )

    # -------------------------------------------------------------- render
    def render(self) -> str:
        icon = _ICON.get(self.status, "?")
        head = (
            f"{icon} #{self.id} [{_STATUS_CN.get(self.status, self.status)}] "
            f"{self.chat_key} 用时 {self.elapsed():.0f}s"
        )
        if self.status == RUNNING:
            head += f"（本轮已收 {self.round_chars} 字，空闲 {self.idle():.0f}s）"
        if self.status == SUSPEND:
            head += "（/sess go 可继续）"
        lines = [head, f"标题: {self.title}", f"问: {self.prompt[:80]}"]
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

    def add(self, session: Session) -> None:
        self._sessions[session.id] = session
        self._prune()

    def get(self, sid: str) -> Session | None:
        sid = (sid or "").lstrip("#").strip()
        return self._sessions.get(sid)

    def recent(self, n: int = 8) -> list[Session]:
        return list(self._sessions.values())[-n:]

    def latest(self, n: int = 10) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.updated, reverse=True)[:n]

    def resumable(self) -> list[Session]:
        return [s for s in self._sessions.values() if s.resumable]

    def _prune(self) -> None:
        if len(self._sessions) <= self.cap:
            return
        # never prune a running turn; drop the oldest finished first
        done = [s for s in self._sessions.values() if s.status != RUNNING]
        done.sort(key=lambda s: s.updated)
        for s in done:
            if len(self._sessions) <= self.cap:
                break
            self._sessions.pop(s.id, None)
