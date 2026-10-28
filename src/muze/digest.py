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
画像优先：下方「用户画像」标明的身份、主线与优先级**高于**通用排序——与用户主线无关、被画像标为次要/非首要的类别（如与用户学业主线无关的事务性工作）默认降后、归并带过，除非其中出现临近时限/风险/承诺。

# 用户画像（长期记忆中关于用户本人的事实；排序与详略必须遵循，没写的不要臆造）
{owner}

# 系统跟踪的待办（结构化状态，不是散文；引用表中事项时带上 #id）
{followups}

# 格式
## 概览
（当天整体在聊什么、最重要的事）
## 需要处理
（按紧急程度排列：等回复的、有时限的、承诺/风险类；每条注明 会话/seq；系统待办表中今天有下落的事项写明下落；没有则写"无"）
## 会话要点
（当天每个活跃会话一小节 `### <会话>`，同样按重要性排序：关键信息、结论/约定/分歧；每节只保留值得保留的信息，不凑条数。标记"全天均匀采样"的会话为抽样子集，首尾消息必在，未收录部分不代表没有内容）
## 值得记住的
（人名-关系、偏好、账号、截止日等沉淀信息；没有则写"无"）

# 原则
- 只依据给出的档案，不要编造；引用关键消息时带 会话/seq。
- 待办状态以「系统跟踪的待办」表为准：表中事项今天档案有明确下落（完成/推进/失效）的写进"需要处理"并给 会话/seq；没有新证据就如实写"无新进展"，**不要因为挂得久就假装解决或避而不提**——用户可手动关闭（/done），长期无动静的系统自动过期。往日报告中的其他线索如有新进展照常跟进。
- 会话内以 `> 往日相关:` 开头的行是语义检索到的历史背景（记忆/档案/旧总结），不是今天的消息；用于关联旧线索，不当作当日事实。
- 语气克制、信息密度高：完整性优先于长度，总长控制在 1000 字内。
- 如果当天几乎没有消息，直接说明"当日无显著活动"并列出仅有的会话；有待办时顺带说明其状态。

# 往日背景（此前数日的总结，仅供跟进；不是今天的档案）
{prior}

# 档案
{transcript}"""

MEMORY_PROMPT = """你是个人 QQ copilot 的"记忆提炼"模块。基于做梦报告与现有长期记忆，提炼需要**长期记住**的事实。

# 提炼标准（从严）
- 只提跨对话长期有用的：身份职业、稳定关系（谁是谁、怎么认识）、稳定偏好/习惯、长期约定、重要账号/截止日。
- 单次闲聊、临时话题、易变状态（今天心情）不要。
- subject 用 'owner'（被控号主人本人）、'<QQ号>'（某联系人）或 'group/<群号>'。
- 新事实与现有记忆矛盾时，输出 {{"action":"retire","id":旧记忆id,"content":"矛盾原因"}} 再加新条。
- **过时功能记忆也要 retire**：现有记忆描述的系统用法/命令/流程若与【近期变更】矛盾（教了已被替换的旧写法），输出 {{"action":"retire","id":该记忆id,"content":"与 <版本号> 变更冲突：旧写法已废弃"}}；确有必要可按新行为补一条。

# 现有记忆
{existing}

# 近期变更（系统的现行行为，冲突时以此为准）
{changes}

# 做梦报告（{date}）
{report}

# 输出（严格 JSON 数组，不要其他文字；最多 {maxn} 条）
[{{"action":"add","subject":"...","kind":"identity|preference|relation|fact","content":"简明完整的中文事实（时间/对象/范围等要素齐全，不刻意压缩成一句话）"}}, ...]"""

ROLLUP_PROMPT = """你是个人 QQ copilot 的"层级做梦"模块。以下是 {scope}（{key}）的下一级做梦报告，请压缩提炼成一份{scope}报告，供长期上下文压缩使用。

# 输入（{n} 份子报告）
{inputs}

# 用户画像（详略与排序遵循；没写的不要臆造）
{owner}

# 要求
## 结构
- 概览：这段时间的主线、关键结论与当前状态
- 主题脉络：按话题归并（不是按天罗列），每个话题：涉及谁、来龙去脉、当前状态
- 待办跟进：上期提出的事项哪些已完成/仍悬置
- 值得记住的：新沉淀的人物关系/偏好/关键节点

# 原则
- 压缩率高但信息不丢：细节留在日/周报里，这里只要脉络与结论。
- 主题脉络的排序与详略遵循用户画像：画像标为次要/非主线的类别压缩归并，除非其中有临近时限或风险。
- 只依据输入报告，不要编造。控制在 {budget} 字内。"""


FOLLOWUP_SYNC_PROMPT = """你是个人 QQ copilot 的"待办同步"模块。根据今日做梦报告与现有待办表，输出需要执行的状态变更。

# 规则（从严）
- add：【当日原始布置】里每一条**明确的任务布置都要登记**——用户说"这个活动需要XX/记得发我/完成后发我审核/给我备份后向下对接"就是可行动事项，不要替用户判断"可能不用跟"；同一件事布置给了不同人的，按人分别登记（各自 assignee）。标题 ≤40 字，写清 谁+什么事+时限（如有）；布置给别人的 assignee 填受托人（会话号即受托人所在会话），有明确截止日的填 due_date（YYYY-MM-DD）。
- 只有"单纯提及、无任何任务语义"（寒暄、转述、纯信息）才不加；拿不准的按 add 处理，用户可随时 drop。
- close：仅当报告中有**明确证据**该事项已解决或失效（写明依据）；不要因为"挂了几天"就关闭——过期由系统自动处理，用户也会手动关闭。status 用 done（已完成）或 dropped（已失效/不用跟）。
- keep：今天有新进展但仍未了结的事项（系统会刷新其动静日期）。

# 现有待办（仅 open；#N 为序号，引用时原样使用）
{open}

# 今日报告（{date}；LLM 摘要，安静会话可能被省略，以【当日原始布置】为准）
{report}

# 当日原始布置（用户自己发出、含任务语义的消息原文；**add 的主要依据**，报告里没写但这里有的一律照登）
{directives}

# 输出（严格 JSON 数组，不要其他文字；最多 {maxn} 条）
[{{"action":"add","subject":"group/123 或 QQ号 或 空串","title":"…","detail":"…","assignee":"布置给谁（用户自己办则空）","due_date":"YYYY-MM-DD 或空"}},
 {{"action":"close","id":1,"status":"done","note":"依据"}},
 {{"action":"keep","id":2}}]"""


def local_date(ts: float | None = None) -> str:
    if ts is None:
        ts = datetime.now().astimezone().timestamp()
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
    rag=None,
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
            report = await run_digest(repo, llm, cfg, chatlog, date=date, adapter=adapter, rag=rag)
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


async def missing_day_holes(repo: Repository, upto: str, limit: int = 7) -> list[str]:
    """Every missing day between the earliest stored day report and ``upto``
    (bounded by ``limit`` per night). Unlike :func:`missing_dates` this also
    catches holes BELOW the latest stored day: a day whose dream failed while
    a later day succeeded must not stay missing forever (live incident
    2026-10-26: 10-24 failed on an LLM 402, 10-25 succeeded, and the catch-up
    never looked back)."""
    earliest = await repo.earliest_day_digest_date()
    if not earliest:
        return [upto]
    existing = {r["date"] for r in await repo.list_digests(limit=500, period="day")}
    out: list[str] = []
    d = datetime.strptime(earliest, "%Y-%m-%d")
    end = datetime.strptime(upto, "%Y-%m-%d")
    while d <= end and len(out) < limit:
        key = d.strftime("%Y-%m-%d")
        if key not in existing:
            out.append(key)
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# report generation
# --------------------------------------------------------------------------
def _spread(lines: list[str], k: int) -> list[str]:
    """k chronological picks that always include the first and the last
    line, with the rest evenly spaced across the day."""
    n = len(lines)
    if k >= n:
        return lines
    if k <= 1:
        return [lines[-1]]
    if k == 2:
        return [lines[0], lines[-1]]
    idxs = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    return [lines[i] for i in idxs]


def _pick_fitting(lines: list[str], target_chars: int) -> list[str]:
    """Largest even-spread subset of lines whose joined size fits the
    target (binary search on the pick count; always >= first+last)."""
    if not lines or sum(len(x) + 1 for x in lines) <= target_chars:
        return lines
    lo, hi, best = 2, len(lines), _spread(lines, 2)
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = _spread(lines, mid)
        if sum(len(x) + 1 for x in cand) <= target_chars:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


async def owner_context(repo: Repository, cfg: MuzeConfig) -> str:
    """Owner identity/priority facts from long-term memory.

    The report is FOR the owner, so its ordering must follow who they are
    (user feedback 2026-10-24: a grad-student + part-time counselor kept
    getting undergrad affairs leading the digest — the prompt never told
    the model who reads it, even though the fact lived in `memories`)."""
    if not cfg.digest.owner_context:
        return "(未启用)"
    try:
        mems = await repo.list_memories(subject="owner", limit=30)
    except Exception:
        mems = []
    lines = []
    for m in mems:
        c = (m.get("content") or "").strip().replace("\n", " ")
        if c:
            lines.append(f"- [{m.get('kind') or 'fact'}] {c}")
    txt = "\n".join(lines)[: cfg.digest.owner_max_chars]
    return txt or "(暂无关于用户本人的记忆；按通用排序，不要臆造画像)"


def numbered_followups(items: list[dict]) -> list[tuple[int, dict]]:
    """Dense 1..N display numbering over open followups, stable open order.

    The DB id stays internal: after closes the raw ids go sparse (#3, #7,
    #12...) which reads badly and confuses the LLM callers. Every surface
    (/todo, digest, agent injection, tools) shows THIS number instead, and
    every resolver maps it back positionally (user feedback 2026-10-28:
    按序号排序，done 之后避免稀疏)."""
    return list(enumerate(sorted(items, key=lambda r: r["id"]), 1))


def followup_no_map(items: list[dict]) -> dict[int, int]:
    """display number -> DB id, for mapping LLM/user answers back."""
    return {n: r["id"] for n, r in numbered_followups(items)}


def parse_due_date(s: str, today: str | None = None) -> str:
    """'2026-10-30' / '10-30' / '10.30' / '10月30日15:30' -> 'YYYY-MM-DD'.

    Missing-year forms borrow the current year. Returns '' when nothing
    parses -- callers keep the deadline inside the title text then."""
    s = (s or "").strip()
    if not s:
        return ""
    today = today or local_date()
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        return f"{today[:4]}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def followup_due_tag(r: dict, today: str | None = None) -> str:
    """'' | '截止 10-30' | '截止 10-30 ⚠已逾期' -- due dates are plain
    YYYY-MM-DD strings so lexicographic compare == chronological."""
    due = str(r.get("due_date") or "").strip()
    if not due:
        return ""
    if (today or local_date()) >= due:
        return f"截止 {due} ⚠已逾期"
    return f"截止 {due}"


def followup_state_tag(r: dict) -> str:
    """'' | '已交付10-30 待验收' -- delegated item handed in, waiting for
    the USER's acceptance (交付≠完成, schema v17)."""
    d = str(r.get("delivered_date") or "").strip()
    return f"已交付{d} 待验收" if d else ""


def _format_followups(items: list[dict]) -> str:
    if not items:
        return "(无)"
    out = []
    for n, r in numbered_followups(items):
        subj = r.get("subject") or "全局"
        line = f"- #{n} [{subj}] {r['title']}"
        if r.get("assignee"):
            line += f" →负责人:{r['assignee']}"
        state = followup_state_tag(r)
        if state:
            line += f"（{state}）"
        due = followup_due_tag(r)
        if due:
            line += f"（{due}）"
        if r.get("detail"):
            line += f"（{str(r['detail'])[:60]}）"
        last = r.get("last_date") or r.get("opened_date") or "?"
        out.append(f"{line} — 开于 {r.get('opened_date') or '?'}，最后动静 {last}")
    return "\n".join(out)


def _apply_transcript_budget(conv_lines: list[list[str]], budget: int) -> None:
    """Shrink the per-conversation line lists in place so their total
    joined size fits the global transcript budget. Proportional per-conv
    targets with a floor, so a busy day still mentions EVERY conversation
    (a floor section keeps >= first+last lines) instead of dropping the
    quiet ones."""
    sizes = [sum(len(x) + 1 for x in ls) for ls in conv_lines]
    total = sum(sizes)
    if total <= budget:
        return
    floor = 600
    for ls, sz in zip(conv_lines, sizes):
        target = max(floor, int(budget * sz / total))
        picked = _pick_fitting(ls, target)
        ls[:] = picked


async def run_digest(
    repo: Repository,
    llm,
    cfg: MuzeConfig,
    chatlog: ChatLog,
    *,
    date: str,
    adapter=None,
    rag=None,
    self_id: int = 0,
) -> str:
    """Generate (and store) the report for one calendar day.

    self_id: the controlled account's own QQ (needed to recognise the
    owner's own delegations for the follow-up bypass)."""
    dc = cfg.digest
    start_ts, end_ts = day_range(date)
    # full coverage by default: top_conversations=0 -> EVERY active
    # conversation of the day gets a section (not just the busiest few)
    convs = await repo.conversations_active_between(
        start_ts, end_ts, limit=dc.top_conversations or -1
    )

    stats: dict = {"date": date, "conversations": len(convs), "sections": []}
    if not convs:
        report = f"{date} 无活跃会话，当日无显著活动。"
        await repo.upsert_digest(date, report, 24.0, stats)
        chatlog.log.info("DREAM      | %s empty (no active conversations)", date)
        return report

    # fetch every conversation's day (capped hard for pathological days);
    # messages_per_conversation>0 acts as an explicit per-conv cap
    conv_lines: list[list[str]] = []
    for c in convs:
        rows = await repo.messages_between(
            c["scene"], c["peer_id"], start_ts, end_ts,
            limit=dc.messages_per_conversation or 2000,
        )
        conv_lines.append(
            [
                f"[{epoch_iso(r['time'])}] [{r['sender_id']}] "
                f"{r['text_content'] or '(无文本)'}"
                for r in rows
            ]
        )
    _apply_transcript_budget(conv_lines, max(4000, dc.transcript_chars))

    # prior-day reports for continuity (follow-ups, ongoing threads);
    # walks back past digest-less days, capped so a long gap stays bounded
    prior_blocks: list[str] = []
    prior_dates: set[str] = set()
    if dc.prior_days > 0:
        d0 = datetime.strptime(date, "%Y-%m-%d")
        got = back = 0
        while got < dc.prior_days and back < dc.prior_days + 7:
            back += 1
            prev = (d0 - timedelta(days=back)).strftime("%Y-%m-%d")
            prow = await repo.get_digest(prev)
            if prow:
                txt = prow["report"].strip()
                if len(txt) > dc.prior_chars:
                    txt = txt[: dc.prior_chars] + "…(截断)"
                prior_blocks.append(f"[{prev}]\n{txt}")
                prior_dates.add(prev)
                got += 1
    # the latest weekly rollup extends continuity beyond the day window
    # (threads older than prior_days live compressed in there)
    if dc.prior_week:
        wkrow = await repo.latest_digest(period="week")
        if wkrow:
            wk = await repo.get_digest(wkrow["date"]) or {}
            txt = (wk.get("report") or "").strip()
            if txt:
                if len(txt) > dc.prior_chars:
                    txt = txt[: dc.prior_chars] + "…(截断)"
                prior_blocks.append(f"[周报 {wkrow['date']}]\n{txt}")
    prior_txt = "\n\n".join(prior_blocks) or "(无)"

    # semantic recall: per-conversation retrieval over past summaries
    # (memories / profiles / older digests). Best-effort — a disabled or
    # unhealthy rag just means no notes; hits already covered by the prior
    # window are skipped to avoid duplication.
    rag_notes: dict[int, list[str]] = {}
    if rag is not None and dc.rag_context and getattr(rag, "enabled", False):
        for i, c in enumerate(convs[: max(1, dc.rag_conversations)]):
            name = c.get("name") or f"{c['scene']}/{c['peer_id']}"
            texts = [ln.split("] ", 2)[-1] for ln in conv_lines[i][:6]]
            query = " ".join([name] + texts)[:400]
            try:
                hits = await rag.query_context(query, k=dc.rag_hits)
            except Exception:
                hits = []
            for h in hits or []:
                ref = str(h.get("ref") or "")
                if ref.startswith("digest:"):
                    d = ref.split(":", 1)[1][:10]
                    if d in prior_dates:
                        continue  # already in 往日背景 verbatim
                text = str(h.get("text") or "").strip().replace("\n", " ")
                if text:
                    rag_notes.setdefault(i, []).append(
                        f"> 往日相关: {text[:dc.rag_chars]}"
                    )

    blocks: list[str] = []
    for idx, (c, lines) in enumerate(zip(convs, conv_lines)):
        scene, peer = c["scene"], c["peer_id"]
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
        sampled = len(lines) < c["cnt"]
        head = f"### {name} ({scene}/{peer}, 当日{c['cnt']}条, 摘{len(lines)}条"
        if sampled:
            head += ", 全天均匀采样"
        body = "\n".join(lines)
        notes = rag_notes.get(idx)
        if notes:
            body += "\n" + "\n".join(notes)
        blocks.append(head + ")\n" + body)
        stats["sections"].append(
            {
                "scene": scene, "peer": peer, "name": name,
                "count": c["cnt"], "kept": len(lines), "sampled": sampled,
            }
        )

    owner_txt = await owner_context(repo, cfg)
    open_items: list[dict] = []
    if dc.followup_extract:
        try:
            open_items = await repo.list_followups(status="open", limit=40)
        except Exception:
            open_items = []
    prompt = DIGEST_PROMPT.format(
        date=date, prior=prior_txt, owner=owner_txt,
        followups=_format_followups(open_items),
        transcript="\n\n".join(blocks),
    )
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
    if dc.followup_extract and llm.configured:
        # structured follow-up state: sync from the fresh report, then age
        # out long-untouched items — both best-effort, never fail the dream
        try:
            # the report is an LLM summary that drops quiet conversations;
            # delegations live in exactly those, so feed the RAW owner
            # messages alongside it (see collect_directives)
            directives = await collect_directives(repo, cfg, date=date, self_id=self_id)
            if directives:
                chatlog.log.info(
                    "DREAM      | %s directives fed to followup sync (%d chars)",
                    date, len(directives),
                )
            await sync_followups(
                repo, llm, cfg, date=date, report=report, chatlog=chatlog,
                directives=directives,
            )
        except Exception:
            log.exception("followup sync for %s failed", date)
        try:
            cutoff = (
                datetime.strptime(date, "%Y-%m-%d") - timedelta(days=dc.followup_expire_days)
            ).strftime("%Y-%m-%d")
            for r in await repo.expire_followups(cutoff_date=cutoff, closed_date=date):
                chatlog.log.info(
                    "FOLLOW     | - #%s expired (untouched since %s): %s",
                    r["id"], r.get("last_date") or r.get("opened_date"), str(r["title"])[:80],
                )
        except Exception:
            log.exception("followup expiry for %s failed", date)
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
    rag=None,
    self_id: int = 0,
) -> str:
    """The nightly builtin:
    1. catch up every missing day up to yesterday (per-day windows);
    2. roll up completed weeks/months into compressed reports (hierarchical
       memory — inputs are child reports, not raw messages);
    3. distill durable memories from the freshest report.
    """
    target = yesterday_date()
    # completion sweep FIRST: the 00:05 full roam has pulled the day's
    # messages by now, and every open followup gets its own evidence check
    # against the RAW messages (user request 2026-10-28) -- this runs
    # before any digest pass so the report and the sync pass see an
    # up-to-date table (a completion found here won't be re-listed as open)
    swept = 0
    try:
        swept = await check_followups(repo, llm, cfg, date=target, chatlog=chatlog)
    except Exception:
        log.exception("followup completion sweep for %s failed", target)

    # catch up every missing day since the EARLIEST stored day report, not
    # just since the latest: a day whose dream failed while a LATER day
    # succeeded used to become a permanent hole (latest advanced past it,
    # missing_dates never returned it again)
    todo = await missing_day_holes(repo, target, limit=7)

    reports: list[tuple[str, str]] = []
    for d in todo:
        try:
            reports.append((d, await run_digest(
                repo, llm, cfg, chatlog, date=d, adapter=adapter, rag=rag, self_id=self_id,
            )))
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
        return f"做梦无缺口（{target} 已存在），周/月报告无待补。"

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
        + (f"，完成度检查 {swept} 条变动" if swept else "")
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
        scope=scope, key=key, n=len(subs), inputs=inputs, budget=budget,
        owner=await owner_context(repo, cfg),
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


DIRECTIVE_HINTS = (
    "需要", "记得", "务必", "尽快", "安排", "对接", "交给", "负责",
    "完成", "截止", "之前", "之前发", "发我", "给我", "审核", "备份",
    "帮我", "请", "deadline", "due",
)


async def collect_directives(
    repo: Repository, cfg: MuzeConfig, *, date: str, self_id: int = 0, limit: int = 60
) -> str:
    """Raw messages where the OWNER hands out a task, taken straight from
    the archive -- independent of the day report.

    Why this exists (live finding 2026-10-28): a delegation is usually ONE
    or TWO short lines in an otherwise quiet chat. The digest LLM drops
    quiet conversations wholesale ("归并带过"/"忽略" in its own words), so
    on 2026-10-23 three real delegations (拍照/写稿/排版 for the 10-30
    event, in three 1-2 message chats) never reached the report and the
    sync pass could not file them -- re-running the dream did not help
    because the report is the ONLY input it had. This bypass is cheap and
    targets exactly the sentences the report is most likely to omit."""
    start_ts, end_ts = day_range(date)
    try:
        rows = await repo.owner_messages_between(start_ts, end_ts, self_id=self_id, limit=limit)
    except Exception:
        return ""
    if not rows:
        return ""
    out = []
    for m in rows:
        text = (m.get("text_content") or "").strip()
        if len(text) < 8:
            continue
        if not any(h in text for h in DIRECTIVE_HINTS):
            continue
        out.append(
            f"[{m.get('scene')}/{m.get('peer_id')}][{epoch_iso(m['time'])}] {text[:220]}"
        )
        if len(out) >= 25:
            break
    return "\n".join(out)


async def sync_followups(
    repo: Repository, llm, cfg: MuzeConfig, *, date: str, report: str, chatlog: ChatLog,
    directives: str = "",
) -> int:
    """Keep the structured follow-up table in step with the fresh report.

    Pending items used to live only as prose in past reports, and the only
    way one ever closed was the digest LLM spotting evidence in a sampled
    transcript — an item finished outside the archive stayed open forever
    (user feedback 2026-10-24). Now the table is the state: this pass adds
    new actionable items, closes ones the report resolves with evidence,
    and refreshes the "last activity" date of ones that progressed."""
    if not llm.configured:
        return 0
    open_items = await repo.list_followups(status="open", limit=40)
    prompt = FOLLOWUP_SYNC_PROMPT.format(
        open=_format_followups(open_items), date=date, report=report[:4000],
        directives=directives or "(无)",
        maxn=cfg.digest.max_memories_per_night,
    )
    msg = await llm.chat([{"role": "user", "content": prompt}])
    raw = (msg.get("content") or "").strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        log.warning("followup sync returned non-JSON, skipped: %.200s", raw)
        return 0
    try:
        items = json.loads(m.group(0))
    except ValueError:
        log.warning("followup JSON parse failed, skipped")
        return 0

    changed = 0
    # the LLM references items by the dense display number shown in {open}
    # (#N) -- map back to DB ids positionally before touching the table
    id_map = followup_no_map(open_items)
    for item in items[: cfg.digest.max_memories_per_night]:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        mid = item.get("id")
        if isinstance(mid, bool) or not isinstance(mid, int):
            mid = None
        real_id = id_map.get(mid) if mid is not None else None
        if action == "add":
            fid = await repo.add_followup(
                str(item.get("subject", "") or "").strip(),
                str(item.get("title", "") or "").strip(),
                str(item.get("detail", "") or ""),
                source=f"dream:{date}", opened_date=date,
                assignee=str(item.get("assignee", "") or ""),
                due_date=str(item.get("due_date", "") or "")[:10],
            )
            if fid is not None:
                changed += 1
                chatlog.log.info(
                    "FOLLOW     | + #%s [%s] %s",
                    fid, item.get("subject", ""), str(item.get("title"))[:80],
                )
        elif action == "close" and real_id is not None:
            st = item.get("status")
            st = st if st in ("done", "dropped") else "done"
            if await repo.close_followup(real_id, status=st, source=f"dream:{date}", closed_date=date):
                changed += 1
                chatlog.log.info(
                    "FOLLOW     | - #%s closed (%s): %s", real_id, st, str(item.get("note", ""))[:80]
                )
        elif action == "keep" and real_id is not None:
            if await repo.touch_followup(real_id, date):
                changed += 1
    return changed


FOLLOWUP_CHECK_PROMPT = """你是个人 QQ copilot 的"待办完成度检查"模块。凌晨漫游已拉全当日消息，下面是每条未结待办与它的【当日证据】（会话原文摘录）。逐条判断它当天是否有完成/失效/交付/新进展。

# 规则（从严，交付≠完成）
- close done：**用户自己办的**，用户明确说完成；**有负责人的，必须用户验收确认**（"可以了/过了/没问题/收到没问题"这类话）才算完成——负责人自己说"交了/发了"**不算**，那只能 deliver。
- deliver：有负责人，负责人明确说已交付/交了/发出来了（用户尚未表态验收）→ 记录交付，等用户验收。
- reject：用户明确打回（"不行/要改/重做"）→ 清除交付标记，继续跟。
- close dropped：证据明确表明事项取消/不再需要跟。
- touch：其他新进展但仍未了结（含承诺在做）。
- 证据不足或无证据：**不要输出该条**。宁可漏判，不可臆造——依据必须来自消息原文。
- #N 是序号，输出时原样引用。

# 未结待办与当日证据（{date}）
{blocks}

# 输出（严格 JSON 数组，不要其他文字；可空）
[{{"no":1,"action":"deliver","note":"负责人原话"}},
 {{"no":2,"action":"close","status":"done","note":"用户验收原话"}},
 {{"no":3,"action":"reject","note":"用户打回原话"}},
 {{"no":4,"action":"touch","note":"进展一句话"}}]"""


def _subject_peer(subject: str) -> tuple[str, int] | None:
    """Parse a followup subject ("group/123", "friend/45" or a bare QQ
    number -> friend) into (scene, peer) for day-message lookup."""
    s = (subject or "").strip()
    if not s:
        return None
    if "/" in s:
        scene, _, peer = s.partition("/")
        if peer.strip().isdigit():
            return scene.strip(), int(peer)
        return None
    if s.isdigit():
        return "friend", int(s)
    return None


def _title_keywords(title: str, cap: int = 3) -> list[str]:
    """Distinctive tokens of a followup title for a LIKE search -- longest
    tokens first (place/event names carry the signal, and a short verb like
    写稿/交表 matches too broadly across unrelated chats)."""
    toks = [t for t in re.split(r"[^\w\u4e00-\u9fff]+", title or "") if len(t) >= 2]
    toks.sort(key=len, reverse=True)
    out: list[str] = []
    for t in toks:
        if t not in out:
            out.append(t)
        if len(out) >= cap:
            break
    return out


async def check_followups(
    repo: Repository, llm, cfg: MuzeConfig, *, date: str, chatlog: ChatLog
) -> int:
    """Per-task completion sweep over the day's RAW messages.

    Runs in dream_night right after the nightly roam pull and BEFORE any
    digest pass (user request 2026-10-28: 每天凌晨拉消息后做梦前跟踪所有
    任务检查完成度). The sync pass inside run_digest only sees the sampled
    report; this pass gives EVERY open followup its own evidence block --
    its home conversation's day messages plus keyword hits anywhere -- so a
    completion mentioned in a quiet chat the report skipped still closes."""
    if not llm.configured:
        return 0
    open_items = await repo.list_followups(status="open", limit=40)
    if not open_items:
        return 0
    start_ts, end_ts = day_range(date)

    blocks: list[str] = []
    for no, r in numbered_followups(open_items):
        seen: set[tuple[str, int, int]] = set()
        lines: list[str] = []

        def _add(rows: list[dict]) -> None:
            for m in rows:
                if not (start_ts <= m["time"] <= end_ts):
                    continue  # searches span all history; keep the day only
                key = (m["scene"], m["peer_id"], m["message_seq"])
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"[{m['scene']}/{m['peer_id']}][{epoch_iso(m['time'])}] "
                    f"[{m['sender_id']}] {m['text_content'] or '(无文本)'}"
                )

        convo = _subject_peer(r.get("subject") or "")
        if convo:
            try:
                _add(await repo.messages_between(convo[0], convo[1], start_ts, end_ts, limit=80))
            except Exception:
                pass
        for kw in _title_keywords(r.get("title") or ""):
            try:
                _add(await repo.search_messages(kw, limit=20))
            except Exception:
                pass
            if len(lines) >= 40:
                break
        evidence = "\n".join(lines[:40]) or "(当日无相关消息)"
        subj = r.get("subject") or "全局"
        head = f"## #{no} [{subj}] {r['title']}"
        if r.get("assignee"):
            head += f" →负责人:{r['assignee']}"
        state = followup_state_tag(r)
        if state:
            head += f"（{state}：只等验收，交付已记录）"
        due = followup_due_tag(r, today=date)
        if due:
            head += f"（{due}）"
        blocks.append(
            head
            + (f"（{str(r.get('detail') or '')[:60]}）" if r.get("detail") else "")
            + f"\n开于 {r.get('opened_date')}，最后动静 {r.get('last_date') or r.get('opened_date')}"
            + f"\n【当日证据】\n{evidence[:1500]}"
        )

    prompt = FOLLOWUP_CHECK_PROMPT.format(
        date=date, blocks="\n\n".join(blocks)[:12000]
    )
    msg = await llm.chat([{"role": "user", "content": prompt}])
    raw = (msg.get("content") or "").strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        log.warning("followup check returned non-JSON, skipped: %.200s", raw)
        return 0
    try:
        items = json.loads(m.group(0))
    except ValueError:
        log.warning("followup check JSON parse failed, skipped")
        return 0

    changed = 0
    id_map = followup_no_map(open_items)
    for item in items[:40]:
        if not isinstance(item, dict):
            continue
        no = item.get("no")
        if isinstance(no, bool) or not isinstance(no, int):
            continue
        real_id = id_map.get(no)
        if real_id is None:
            continue
        action = item.get("action")
        if action == "close":
            st = item.get("status")
            st = st if st in ("done", "dropped") else "done"
            if await repo.close_followup(real_id, status=st, source=f"sweep:{date}", closed_date=date):
                changed += 1
                chatlog.log.info(
                    "FOLLOW     | - #%s swept closed (%s): %s",
                    real_id, st, str(item.get("note", ""))[:80],
                )
        elif action == "deliver":
            # 交付≠完成: the assignee handed it in; it now WAITS for the
            # user's acceptance (schema v17) instead of closing
            if await repo.set_followup_delivered(real_id, date):
                changed += 1
                chatlog.log.info(
                    "FOLLOW     | = #%s delivered %s, awaiting acceptance: %s",
                    real_id, date, str(item.get("note", ""))[:80],
                )
        elif action == "reject":
            if await repo.set_followup_delivered(real_id, None):
                await repo.touch_followup(real_id, date)
                changed += 1
                chatlog.log.info(
                    "FOLLOW     | ! #%s rejected, back to open: %s",
                    real_id, str(item.get("note", ""))[:80],
                )
        elif action == "touch":
            if await repo.touch_followup(real_id, date):
                changed += 1
                chatlog.log.info(
                    "FOLLOW     | ~ #%s swept touch: %s", real_id, str(item.get("note", ""))[:80]
                )
    return changed


async def extract_memories(
    repo: Repository, llm, cfg: MuzeConfig, *, date: str, report: str, chatlog: ChatLog
) -> int:
    """Second reflection pass: distill durable facts into `memories`."""
    existing = await repo.list_memories(limit=60)
    existing_txt = "\n".join(
        f"- #{m['id']} [{m['subject']}/{m['kind']}] {m['content']}" for m in existing
    ) or "(暂无)"
    from muze.changelog import recent_notes

    prompt = MEMORY_PROMPT.format(
        existing=existing_txt, report=report[:4000], date=date,
        changes=recent_notes(5),
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
