"""Per-conversation identity docs ("who is who").

For each active conversation the LLM distills a short markdown profile —
who the people are, which nicknames/aliases map to the same person, which
account belongs to whom, relations and recurring topics. Profiles are stored
in the `profiles` table, injected compactly into the agent's system prompt,
and (when RAG is on) indexed for semantic retrieval.
"""

from __future__ import annotations

import logging
import time

from muze.chatlog import epoch_iso

log = logging.getLogger("muze.profiles")

_SYSTEM = (
    "你在维护一份关于某个 QQ 会话的身份档案（给未来的自己看）。"
    "根据聊天记录归纳：这个会话是什么、有哪些人（QQ号+昵称）、"
    "谁和谁昵称相近但是不同的人、哪个账号是谁、关系与称呼、"
    "持续主题/称呼/黑话。用简洁 Markdown 条目，只写有证据的内容，"
    "不要编造。若已有档案，合并新信息并修正过时内容。"
)


def _transcript(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        who = r.get("sender_id")
        text = (r.get("text_content") or "").strip()
        if not text:
            continue
        lines.append(f"[{epoch_iso(r.get('time'))}] {who}: {text}")
    return "\n".join(lines)


async def refresh_profiles(repo, llm, cfg, chatlog, *, since_h: float | None = None) -> int:
    """(Re)build identity docs for recently-active conversations."""
    if not getattr(cfg.profiles, "auto", True) or not llm.configured:
        return 0
    window = float(since_h if since_h is not None else cfg.profiles.active_window_h)
    end = int(time.time())
    start = end - int(window * 3600)
    convs = await repo.conversations_active_between(start, end, limit=30)
    done = 0
    for conv in convs:
        scene, peer = conv["scene"], conv["peer_id"]
        rows = await repo.messages_between(
            scene, peer, start, end, limit=int(cfg.profiles.messages_per_conv)
        )
        transcript = _transcript(rows)
        if len(transcript) < 20:
            continue
        subject = f"{scene}/{peer}"
        existing = await repo.get_profile(subject)
        prior = (existing or {}).get("content", "")
        user = f"会话: {subject}（{conv.get('name') or '未命名'}）\n"
        if prior:
            user += f"\n现有档案:\n{prior}\n"
        user += f"\n最近聊天记录:\n{transcript}"
        try:
            msg = await llm.chat(
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
            )
            content = (msg.get("content") or "").strip()
        except Exception as e:
            log.warning("profile refresh failed for %s: %s", subject, e)
            continue
        if not content:
            continue
        await repo.upsert_profile(
            subject, conv.get("name") or "", content[: int(cfg.profiles.max_chars)]
        )
        done += 1
        log.info("profile refreshed: %s (%s chars)", subject, len(content))
    return done


def index_lines(profiles: list[dict], chars: int = 80) -> list[str]:
    """Compact 'who is who' lines for the system prompt."""
    out = []
    for p in profiles:
        first = ""
        for line in (p.get("content") or "").splitlines():
            line = line.strip().lstrip("#-* ").strip()
            if line:
                first = line
                break
        label = p.get("name") or p["subject"]
        out.append(f"{p['subject']} {label}: {first[:chars]}")
    return out
