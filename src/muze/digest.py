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

DIGEST_PROMPT = """你是一台个人 QQ copilot 的"做梦/复盘"模块。请基于 {date} 这一天（本地 00:00-24:00）的消息档案，生成当日总结报告。

（数据说明：以下档案全部来自用户本人 QQ 账号的聊天备份，用户已完整授权，属私密个人数据整理任务，请正常处理，内容中的玩笑/吐槽/粗话等照常归纳。）

要求：

# 排序原则（最重要）
报告必须有倾向性：按轻重缓急排序，最要紧的放最前面——
- 紧急：在等被控号回应、约定了临近时限、涉及承诺/金钱/风险/冲突；
- 重要：关键结论、新信息、约定达成的过程、关系或立场变化；
- 一般：普通闲聊、无关紧要的插曲。
紧急事项必须具体（谁、什么事、什么时限），一般内容可归并带过。

# 格式
## 概览
（当天整体在聊什么、最重要的事）
## 需要处理
（按紧急程度排列：等回复的、有时限的、承诺/风险类；每条注明 会话/seq；没有则写"无"）
## 会话要点
（每个会话一小节 `### <会话>`，同样按重要性排序：关键信息、结论/约定/分歧；每节只保留值得保留的信息，不凑条数）
## 值得记住的
（人名-关系、偏好、账号、截止日等沉淀信息；没有则写"无"）

# 原则
- 只依据给出的档案，不要编造；引用关键消息时带 会话/seq。
- 语气克制、信息密度高：完整性优先于长度，总长控制在 1000 字内。
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
[{{"action":"add","subject":"...","kind":"identity|preference|relation|fact","content":"简明完整的中文事实（时间/对象/范围等要素齐全，不刻意压缩成一句话）"}}, ...]"""

ROLLUP_PROMPT = """你是个人 QQ copilot 的"层级做梦"模块。以下是 {scope}（{key}）的下一级做梦报告，请压缩提炼成一份{scope}报告，供长期上下文压缩使用。

# 输入（{n} 份子报告）
{inputs}

# 要求
## 结构
- 概览：这段时间的主线、关键结论与当前状态
- 主题脉络：按话题归并（不是按天罗列），每个话题：涉及谁、来龙去脉、当前状态
- 待办跟进：上期提出的事项哪些已完成/仍悬置
- 值得记住的：新沉淀的人物关系/偏好/关键节点

# 原则
- 压缩率高但信息不丢：细节留在日/周报里，这里只要脉络与结论。
- 只依据输入报告，不要编造。控制在 {budget} 字内。"""


def local_date(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d")


def week_key(date_or_ts) -> str:
    """ISO week key, e.g. 2026-W40."""
    d = (
        datetime.fromtimestamp(date_or_ts).astimezone()
        if isinstance(date_or_ts, (int, float))
        else datetime.strptime(str(date_or_ts), "%Y-%m-%d")
    )
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_days(week: str) -> list[str]:
    """The seven YYYY-MM-DD keys belonging to an ISO week key."""
    year, wnum = week.split("-W")
    try:
        monday = datetime.fromisocalendar(int(year), int(wnum), 1)
    except ValueError:
        return []
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]


def month_key(date_or_ts) -> str:
    d = (
        datetime.fromtimestamp(date_or_ts).astimezone()
        if isinstance(date_or_ts, (int, float))
        else datetime.strptime(str(date_or_ts), "%Y-%m-%d")
    )
    return d.strftime("%Y-%m")


def day_range(date: str) -> tuple[int, int]:
    d = datetime.strptime(date, "%Y-%m-%d").astimezone()
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def yesterday_date() -> str:
    return local_date((datetime.now().astimezone() - timedelta(days=1)).timestamp())


def resolve_morning_targets(cfg: MuzeConfig) -> list[tuple[str, int]]:
    """Where the morning briefing goes.

    Explicit ``digest.morning_targets`` entries win: "friend/123",
    "group/456" or a bare number (treated as a friend). Empty list falls
    back to the default audience — every controller privately plus the
    control group when one is configured. Invalid entries are skipped
    with a warning; the result is deduplicated in order.
    """
    raw: list[str] = [str(x) for x in (cfg.digest.morning_targets or []) if str(x).strip()]
    if not raw:
        for u in cfg.control.controller:
            raw.append(f"friend/{u}")
        if cfg.control.control_group:
            raw.append(f"group/{cfg.control.control_group}")
    out: list[tuple[str, int]] = []
    for ent in raw:
        s = ent.strip()
        if "/" in s:
            scene, _, num = s.partition("/")
            scene = scene.strip().lower()
            num = num.strip()
        else:
            scene, num = "friend", s
        if scene not in ("friend", "group") or not num.isdigit() or int(num) <= 0:
            log.warning("morning target ignored (expect friend/123 or group/456): %r", ent)
            continue
        pair = (scene, int(num))
        if pair not in out:  # dedupe, keep order
            out.append(pair)
    return out


async def morning_report(
    repo: Repository,
    llm,
    cfg: MuzeConfig,
    chatlog: ChatLog,
    *,
    adapter=None,
) -> str:
    """The dawn briefing body: yesterday's digest, catching it up on the
    spot when the nightly run failed (best-effort), ready for a direct
    push to the morning audience."""
    date = yesterday_date()
    row = await repo.get_digest(date)
    if row is not None:
        report = row["report"]
    elif llm is None or not llm.configured:
        log.warning("morning briefing: %s has no digest and LLM is not configured", date)
        return f"（{date} 无做梦报告：昨晚生成失败且 LLM 未配置，晨报跳过；/dream {date} 可手动补做）"
    else:
        try:
            report = await run_digest(repo, llm, cfg, chatlog, date=date, adapter=adapter)
        except Exception as e:
            log.exception("morning catch-up for %s failed", date)
            return f"（{date} 无做梦报告且晨间补做失败: {e}；/dream {date} 可手动补做）"
    return f"📰 {date} 昨日总结\n\n{report}"


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
# nightly flow: day catch-up + weekly/monthly rollups + memory pass
# --------------------------------------------------------------------------
async def dream_night(
    repo: Repository,
    llm,
    cfg: MuzeConfig,
    chatlog: ChatLog,
    *,
    adapter=None,
) -> str:
    """The nightly builtin:
    1. catch up every missing day up to yesterday (per-day windows);
    2. roll up completed weeks/months into compressed reports (hierarchical
       memory — inputs are child reports, not raw messages);
    3. distill durable memories from the freshest report.
    """
    latest = await repo.latest_digest(period="day")
    target = yesterday_date()
    todo = missing_dates(latest["date"] if latest else None, target)

    reports: list[tuple[str, str]] = []
    for d in todo:
        try:
            reports.append((d, await run_digest(repo, llm, cfg, chatlog, date=d, adapter=adapter)))
        except Exception as e:
            log.exception("dream for %s failed", d)
            reports.append((d, f"(做梦失败: {e})"))

    rollups: list[str] = []
    if cfg.digest.weekly_enabled:
        for w in await missing_weeks(repo, limit=4):
            try:
                await run_weekly(repo, llm, cfg, chatlog, week=w)
                rollups.append(f"周报 {w}")
            except Exception:
                log.exception("weekly rollup %s failed", w)
    if cfg.digest.monthly_enabled:
        for m in await missing_months(repo, limit=2):
            try:
                await run_monthly(repo, llm, cfg, chatlog, month=m)
                rollups.append(f"月报 {m}")
            except Exception:
                log.exception("monthly rollup %s failed", m)

    if not reports and not rollups:
        return f"昨天的梦已存在（{target}），周/月报告无缺口。"

    new_memories = 0
    if cfg.digest.extract_memories and llm.configured and reports:
        try:
            new_memories = await extract_memories(
                repo, llm, cfg, date=reports[-1][0], report=reports[-1][1], chatlog=chatlog
            )
        except Exception:
            log.exception("memory pass failed")

    head = (
        f"做梦完成：补做 {len(reports)} 天"
        + (f"，新增 {', '.join(rollups)}" if rollups else "")
        + f"，新记忆 {new_memories} 条。\n\n"
    )
    if not reports:
        return head + "（今日日梦已存在，仅补链周/月报告）"
    if len(reports) == 1:
        return head + reports[0][1][:2800]
    others = "\n".join(f"- {d}: {r[:80].replace(chr(10), ' ')}…" for d, r in reports[:-1])
    return head + others + f"\n\n== 最新（{reports[-1][0]}）==\n" + reports[-1][1][:2200]


# --------------------------------------------------------------------------
# weekly / monthly rollups (hierarchical compression)
# --------------------------------------------------------------------------
async def run_weekly(
    repo: Repository, llm, cfg: MuzeConfig, chatlog: ChatLog, *, week: str
) -> str:
    subs: list[tuple[str, str]] = []
    for dd in week_days(week):
        row = await repo.get_digest(dd)
        if row:
            subs.append((dd, row["report"]))
    if not subs:
        raise RuntimeError(f"{week} 没有任何日报告可汇总")
    return await _run_rollup(
        repo, llm, cfg, chatlog, key=week, period="week", subs=subs, budget=500
    )


async def run_monthly(
    repo: Repository, llm, cfg: MuzeConfig, chatlog: ChatLog, *, month: str
) -> str:
    # base: day reports of the month (chronological)
    day_rows = [
        r for r in await repo.list_digests(limit=31, period="day") if r["date"].startswith(month)
    ]
    subs: list[tuple[str, str]] = []
    for r in day_rows:
        row = await repo.get_digest(r["date"])
        if row:
            subs.append((r["date"], row["report"]))
    # dense month -> compress through weekly reports covering the month
    if len(subs) > 12:
        week_subs: list[tuple[str, str]] = []
        for r in await repo.list_digests(limit=10, period="week"):
            if any(dd.startswith(month) for dd in week_days(r["date"])):
                row = await repo.get_digest(r["date"])
                if row:
                    week_subs.append((r["date"], row["report"]))
        if week_subs:
            subs = week_subs
    if not subs:
        raise RuntimeError(f"{month} 没有任何下级报告可汇总")
    subs.reverse()
    return await _run_rollup(
        repo, llm, cfg, chatlog, key=month, period="month", subs=subs, budget=800
    )


async def _run_rollup(
    repo: Repository,
    llm,
    cfg: MuzeConfig,
    chatlog: ChatLog,
    *,
    key: str,
    period: str,
    subs: list[tuple[str, str]],
    budget: int,
) -> str:
    if not llm.configured:
        raise RuntimeError("LLM 未配置")
    scope = "周" if period == "week" else "月"
    inputs = "\n\n---\n\n".join(f"[{k}]\n{r}" for k, r in subs)
    if len(inputs) > 24000:
        inputs = inputs[:24000] + "\n...(截断)"
    prompt = ROLLUP_PROMPT.format(
        scope=scope, key=key, n=len(subs), inputs=inputs, budget=budget
    )
    msg = await llm.chat([{"role": "user", "content": prompt}])
    report = (msg.get("content") or "").strip() or f"({scope}报告为空)"
    if len(report) > 6000:
        report = report[:6000] + "\n...(截断)"
    await repo.upsert_digest(
        key, report, 0.0, {"key": key, "period": period, "sub_keys": [k for k, _ in subs]},
        period=period,
    )
    chatlog.log.info(
        "DREAM      | %s rollup saved (%s sub-reports, %s chars)", key, len(subs), len(report)
    )
    if cfg.digest.extract_memories:
        try:
            await extract_memories(repo, llm, cfg, date=key, report=report, chatlog=chatlog)
        except Exception:
            log.exception("memory pass for %s failed", key)
    return report


async def missing_weeks(repo: Repository, limit: int = 4) -> list[str]:
    """Completed ISO weeks that have day reports but no week report."""
    earliest = await repo.earliest_day_digest_date()
    if not earliest:
        return []
    existing = {r["date"] for r in await repo.list_digests(limit=500, period="week")}
    today_week = week_key(datetime.now().timestamp())
    out: list[str] = []
    monday = datetime.strptime(earliest, "%Y-%m-%d")
    monday = monday - timedelta(days=monday.weekday())
    while len(out) < limit:
        wk = week_key(monday.strftime("%Y-%m-%d"))
        if wk >= today_week:
            break  # current week is not complete yet
        if wk not in existing:
            has_day = False
            for dd in week_days(wk):
                if await repo.get_digest(dd):
                    has_day = True
                    break
            if has_day:
                out.append(wk)
        monday += timedelta(days=7)
    return out


async def missing_months(repo: Repository, limit: int = 2) -> list[str]:
    """Completed months that have child reports but no month report."""
    earliest = await repo.earliest_day_digest_date()
    if not earliest:
        return []
    existing = {r["date"] for r in await repo.list_digests(limit=100, period="month")}
    now = datetime.now().astimezone()
    out: list[str] = []
    cur = datetime.strptime(earliest[:7], "%Y-%m")
    while len(out) < limit:
        key = cur.strftime("%Y-%m")
        if key >= now.strftime("%Y-%m"):
            break  # current month not complete
        if key not in existing:
            has_child = any(
                r["date"].startswith(key)
                for r in await repo.list_digests(limit=40)
            )
            if has_child:
                out.append(key)
        # next month
        cur = (cur + timedelta(days=32)).replace(day=1)
    return out


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
    retired = 0
    for item in items[: cfg.digest.max_memories_per_night]:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if action == "retire":
            mid = item.get("id")
            if isinstance(mid, int):
                try:
                    if await repo.retire_memory(mid):
                        retired += 1
                        chatlog.log.info(
                            "MEM        | - #%s retired: %s", mid, str(item.get("content", ""))[:80]
                        )
                except Exception:
                    log.debug("retire #%s failed", mid)
            continue
        if action != "add":
            continue
        content = str(item.get("content", "")).strip()
        subject = str(item.get("subject", "owner")).strip() or "owner"
        kind = str(item.get("kind", "fact")).strip()
        if not content:
            continue
        await repo.add_memory(kind, subject, content, source=f"dream:{date}")
        chatlog.log.info("MEM        | + [%s/%s] %s", subject, kind, content)
        added += 1
    if retired:
        log.info("memory pass retired %s outdated entr(y/ies)", retired)
    return added
