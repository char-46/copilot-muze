"""Daily digest — "dreaming" (à la OpenClaw) + long-term memory growth.

Every night at a fixed time (digest.at, default 01:00) the agent "dreams":
1. catch-up: every missing day since the last digest gets its own report,
   built strictly from that day's message window (00:00-24:00 local);
2. memory pass: a second LLM reflection distills durable facts about the
   user and their contacts (identity / preferences / relations) into the
   `memories` table, which is injected into the agent's system prompt —
   this is what makes the copilot better the longer it runs.

Stored reports double as compressed history: /dream & the recall_dream tool
read them instead of re-scanning raw messages.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from muze.chatlog import ChatLog, epoch_iso
from muze.config import MuzeConfig
from muze.store.repo import Repository

log = logging.getLogger("muze.digest")

DIGEST_PROMPT = """你是一台个人 QQ copilot 的"做梦/复盘"模块。请基于 {date} 这一天（本地 00:00-24:00）的消息档案，生成当日总结报告。要求：

# 格式
## 今日概览
（2-3 句：这一天整体在聊什么、最重要的事）
## 各会话要点
（每个会话一小节 `### <会话>`：谁说了什么关键信息、结论/约定/分歧，每节不超过 5 条）
## 待办与需回复
（明确列出哪条消息在等被控号回应、有哪些约定的时间点）
## 值得记住的
（人名-关系、偏好、账号、截止日等沉淀信息）

# 原则
- 只依据给出的档案，不要编造；引用关键消息时带 会话/seq。
- 语气克制、信息密度高，总长控制在 600 字内。
- 如果当天几乎没有消息，直接说明"当日无显著活动"并列出仅有的会话。

# 档案
{transcript}"""

MEMORY_PROMPT = """你是个人 QQ copilot 的"记忆提炼"模块。基于做梦报告与现有长期记忆，提炼需要**长期记住**的事实。

# 提炼标准（从严）
- 只提跨对话长期有用的：身份职业、稳定关系（谁是谁、怎么认识）、稳定偏好/习惯、长期约定、重要账号/截止日。
- 单次闲聊、临时话题、易变状态（今天心情）不要。
- subject 用 'owner'（被控号主人本人）、'<QQ号>'（某联系人）或 'group/<群号>'。
- 新事实与现有记忆矛盾时，输出 {{"action":"retire","id":旧记忆id,"content":"矛盾原因"}} 再加新条。

# 现有记忆
{existing}

# 做梦报告（{date}）
{report}

# 输出（严格 JSON 数组，不要其他文字；最多 {maxn} 条）
[{{"action":"add","subject":"...","kind":"identity|preference|relation|fact","content":"一句话中文事实"}}, ...]"""


def local_date(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d")


def day_range(date: str) -> tuple[int, int]:
    d = datetime.strptime(date, "%Y-%m-%d").astimezone()
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def yesterday_date() -> str:
    return local_date((datetime.now().astimezone() - timedelta(days=1)).timestamp())


def next_run_for_hhmm(hhmm: str) -> int:
    hh, _, mm = hhmm.partition(":")
    now = datetime.now().astimezone()
    target = now.replace(hour=int(hh) % 24, minute=int(mm) % 60, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return int(target.timestamp())


def missing_dates(repo_latest: str | None, upto: str) -> list[str]:
    """Days in (latest, upto] that lack a digest. No latest -> just [upto]."""
    out: list[str] = []
    if repo_latest is None:
        return [upto]
    d = datetime.strptime(repo_latest, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(upto, "%Y-%m-%d")
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# report generation
# --------------------------------------------------------------------------
async def run_digest(
    repo: Repository,
    llm,
    cfg: MuzeConfig,
    chatlog: ChatLog,
    *,
    date: str,
    adapter=None,
) -> str:
    """Generate (and store) the report for one calendar day."""
    dc = cfg.digest
    start_ts, end_ts = day_range(date)
    convs = await repo.conversations_active_between(
        start_ts, end_ts, limit=max(1, dc.top_conversations)
    )

    stats: dict = {"date": date, "conversations": len(convs), "sections": []}
    if not convs:
        report = f"{date} 无活跃会话，当日无显著活动。"
        await repo.upsert_digest(date, report, 24.0, stats)
        chatlog.log.info("DREAM      | %s empty (no active conversations)", date)
        return report

    blocks: list[str] = []
    for c in convs:
        scene, peer = c["scene"], c["peer_id"]
        rows = await repo.messages_between(
            scene, peer, start_ts, end_ts, limit=dc.messages_per_conversation
        )
        label = c.get("name") or f"{scene}/{peer}"
        name = label
        if adapter is not None:
            try:
                if scene == "group":
                    name = await adapter.ensure_group_name(peer) or label
                else:
                    name = await adapter.ensure_friend_name(peer) or label
            except Exception:
                pass
        lines = [
            f"[{epoch_iso(r['time'])}] [{r['sender_id']}] {r['text_content'] or '(无文本)'}"
            for r in rows
        ]
        blocks.append(
            f"### {name} ({scene}/{peer}, 当日{c['cnt']}条, 摘{len(rows)}条)\n"
            + "\n".join(lines)
        )
        stats["sections"].append(
            {"scene": scene, "peer": peer, "name": name, "count": c["cnt"]}
        )

    prompt = DIGEST_PROMPT.format(date=date, transcript="\n\n".join(blocks))
    if not llm.configured:
        raise RuntimeError("LLM 未配置，无法做梦（检查 llm.chat.*）")
    msg = await llm.chat([{"role": "user", "content": prompt}])
    report = (msg.get("content") or "").strip() or "(做梦结果为空)"
    if len(report) > 8000:
        report = report[:8000] + "\n...(截断)"

    await repo.upsert_digest(date, report, 24.0, stats)
    chatlog.log.info(
        "DREAM      | %s saved (%s conversations, %s chars)", date, len(convs), len(report)
    )
    return report


# --------------------------------------------------------------------------
# nightly flow: catch-up + memory pass
# --------------------------------------------------------------------------
async def dream_night(
    repo: Repository,
    llm,
    cfg: MuzeConfig,
    chatlog: ChatLog,
    *,
    adapter=None,
) -> str:
    """The nightly builtin: catch up every missing day up to yesterday,
    then run the memory pass over the freshest report. Returns a summary
    text suitable for delivery."""
    latest = await repo.latest_digest()
    target = yesterday_date()
    todo = missing_dates(latest["date"] if latest else None, target)
    if not todo:
        return f"昨天的梦已存在（{target}），无需补做。"

    reports: list[tuple[str, str]] = []
    for d in todo:
        try:
            reports.append((d, await run_digest(repo, llm, cfg, chatlog, date=d, adapter=adapter)))
        except Exception as e:
            log.exception("dream for %s failed", d)
            reports.append((d, f"(做梦失败: {e})"))

    new_memories = 0
    if cfg.digest.extract_memories and llm.configured:
        try:
            new_memories = await extract_memories(
                repo, llm, cfg, date=reports[-1][0], report=reports[-1][1], chatlog=chatlog
            )
        except Exception:
            log.exception("memory pass failed")

    head = f"做梦完成：补做 {len(reports)} 天（{todo[0]} ~ {todo[-1]}），新记忆 {new_memories} 条。\n\n"
    if len(reports) == 1:
        return head + reports[0][1][:2800]
    # multiple days: newest in full, others one-liners
    others = "\n".join(f"- {d}: {r[:80].replace(chr(10), ' ')}…" for d, r in reports[:-1])
    return head + others + f"\n\n== 最新（{reports[-1][0]}）==\n" + reports[-1][1][:2200]


async def extract_memories(
    repo: Repository, llm, cfg: MuzeConfig, *, date: str, report: str, chatlog: ChatLog
) -> int:
    """Second reflection pass: distill durable facts into `memories`."""
    existing = await repo.list_memories(limit=60)
    existing_txt = "\n".join(
        f"- #{m['id']} [{m['subject']}/{m['kind']}] {m['content']}" for m in existing
    ) or "(暂无)"
    prompt = MEMORY_PROMPT.format(
        existing=existing_txt, report=report[:4000], date=date,
        maxn=cfg.digest.max_memories_per_night,
    )
    msg = await llm.chat([{"role": "user", "content": prompt}])
    raw = (msg.get("content") or "").strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        log.warning("memory pass returned non-JSON, skipped: %.200s", raw)
        return 0
    try:
        items = json.loads(m.group(0))
    except ValueError:
        log.warning("memory JSON parse failed, skipped")
        return 0

    added = 0
    for item in items[: cfg.digest.max_memories_per_night]:
        if not isinstance(item, dict) or item.get("action") != "add":
            continue
        content = str(item.get("content", "")).strip()
        subject = str(item.get("subject", "owner")).strip() or "owner"
        kind = str(item.get("kind", "fact")).strip()
        if not content:
            continue
        await repo.add_memory(kind, subject, content, source=f"dream:{date}")
        chatlog.log.info("MEM        | + [%s/%s] %s", subject, kind, content)
        added += 1
    return added
