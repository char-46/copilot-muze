"""Logging setup and chat-content rendering.

Requirement: verbose logs including the actual chat content, written to stdout
and to an ever-growing file (no rotation). Every incoming/backfilled/sent
message produces one greppable line via the `muze.chat` logger.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path

from muze.config import LogConfig, StorageConfig
from muze.provider.milky.models import IncomingMessage, Segment
from muze.version import epoch_iso

CHAT_LOGGER = "muze.chat"


def setup_logging(log_cfg: LogConfig, storage_cfg: StorageConfig) -> Path:
    log_dir = Path(log_cfg.file or "").parent if log_cfg.file else storage_cfg.logs_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(log_cfg.file) if log_cfg.file else log_dir / "muze.log"

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_cfg.level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    filehandler = logging.FileHandler(log_path, encoding="utf-8")  # append forever
    filehandler.setFormatter(fmt)
    root.addHandler(filehandler)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    return log_path


def _one_line(s: str) -> str:
    return (s or "").replace("\r", "\\r").replace("\n", "\\n")


def size_fmt(n: int | None) -> str:
    if not n:
        return "?B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def conv_id(scene: str, peer_id: int) -> str:
    return f"{scene}/{peer_id}"


def render_segment(seg: Segment) -> str:
    t = getattr(seg, "type", "?")
    if t == "text":
        return getattr(seg, "text", "")
    if t == "mention":
        name = getattr(seg, "name", "")
        uid = getattr(seg, "user_id", 0)
        return f"@{name}({uid})" if name else f"@{uid}"
    if t == "mention_all":
        return "@全体成员"
    if t == "face":
        return f"[表情:{getattr(seg, 'face_id', '')}]"
    if t == "reply":
        return f"[回复#{getattr(seg, 'message_seq', 0)}]"
    if t == "image":
        s = seg  # ImageSeg
        cap = s.summary or f"{s.width}x{s.height}"
        return f"[图片:{cap}]"
    if t == "record":
        return f"[语音:{getattr(seg, 'duration', 0)}s]"
    if t == "video":
        s = seg
        return f"[视频:{s.duration}s {s.width}x{s.height}]"
    if t == "file":
        s = seg
        return f"[文件:{s.file_name} {size_fmt(s.file_size)}]"
    if t == "forward":
        s = seg
        title = s.title or s.summary or ";".join(s.preview or [])
        return f"[转发:{_one_line(title)}]"
    if t == "market_face":
        return f"[表情包:{getattr(seg, 'summary', '')}]"
    if t == "light_app":
        return f"[小程序:{getattr(seg, 'app_name', '')}]"
    if t == "xml":
        return f"[XML:{_one_line(getattr(seg, 'xml_payload', ''))[:60]}]"
    if t == "markdown":
        return f"[MD:{_one_line(getattr(seg, 'content', ''))[:80]}]"
    return f"[未知段:{t}]"


def render_message(msg: IncomingMessage, sender_display: str = "") -> str:
    who = f"{sender_display}({msg.sender_id})" if sender_display else str(msg.sender_id)
    parts = [render_segment(s) for s in msg.segments]
    body = " ".join(p for p in parts if p) or "(空消息)"
    ts = epoch_iso(msg.time) or "?"
    return (
        f"{conv_id(msg.message_scene, msg.peer_id)} seq={msg.message_seq} "
        f"{who} {ts} | {_one_line(body)}"
    )


def plain_text(segments: list[Segment]) -> str:
    """Searchable text projection stored in messages.text_content."""
    out: list[str] = []
    for seg in segments:
        t = getattr(seg, "type", "?")
        if t == "text":
            out.append(getattr(seg, "text", ""))
        elif t == "markdown":
            out.append(getattr(seg, "content", ""))
        elif t == "mention":
            name = getattr(seg, "name", "")
            out.append(f"@{name}" if name else f"@{getattr(seg, 'user_id', '')}")
        elif t == "mention_all":
            out.append("@全体成员")
        elif t == "reply":
            inner = plain_text(getattr(seg, "segments", []) or [])
            out.append(f"[回复#{getattr(seg, 'message_seq', 0)} {inner[:50]}]".rstrip())
        elif t == "image":
            s = seg
            out.append(f"[图片:{s.summary or f'{s.width}x{s.height}'}]")
        elif t == "record":
            out.append(f"[语音:{getattr(seg, 'duration', 0)}s]")
        elif t == "video":
            out.append(f"[视频:{getattr(seg, 'duration', 0)}s]")
        elif t == "file":
            out.append(f"[文件:{getattr(seg, 'file_name', '')}]")
        elif t == "forward":
            s = seg
            out.append(f"[转发:{s.title or s.summary}]")
        elif t == "market_face":
            out.append(f"[表情包:{getattr(seg, 'summary', '')}]")
        elif t == "light_app":
            out.append(f"[小程序:{getattr(seg, 'app_name', '')}]")
        elif t == "xml":
            out.append(f"[XML:{_one_line(getattr(seg, 'xml_payload', ''))[:50]}]")
    return " ".join(x for x in out if x).strip()


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def unescape_xml(s: str) -> str:
    return html.unescape(s)


class ChatLog:
    """Structured content logging on the `muze.chat` logger."""

    def __init__(self) -> None:
        self.log = logging.getLogger(CHAT_LOGGER)

    def recv(self, msg: IncomingMessage, sender_display: str = "") -> None:
        self.log.info("CHAT recv   | %s", render_message(msg, sender_display))

    def backfill(self, msg: IncomingMessage, sender_display: str = "") -> None:
        self.log.info("CHAT back   | %s", render_message(msg, sender_display))

    def sent(self, scene: str, peer_id: int, seq: int, preview: str) -> None:
        self.log.info(
            "CHAT sent  | -> %s seq=%s | %s", conv_id(scene, peer_id), seq, _one_line(preview)
        )

    def ctrl(self, source: str, who: str, text: str) -> None:
        self.log.info("CTRL %-4s   | %s | %s", source, who, _one_line(text))

    def ctrl_reply(self, source: str, who: str, text: str) -> None:
        self.log.info("CTRL %-4s   | -> %s | %s", source, who, _one_line(text)[:300])

    def agent(self, source: str, who: str, direction: str, text: str) -> None:
        self.log.info("AGENT %-4s  | %s | %s: %s", source, who, direction, _one_line(text)[:400])

    def confirm(self, action_id: int, desc: str) -> None:
        self.log.info("CONFIRM    | #%s | %s", action_id, _one_line(desc))

    def media(self, status: str, desc: str) -> None:
        self.log.info("MEDIA %-4s | %s", status, desc)
