"""Conversational agent over the message archive (tool-calling loop)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, suppress

from muze.ai.llm import LLMClient
from muze.ai.sessions import ERROR, RUNNING, SUSPEND, Session, SessionStore
from muze.ai.tools import SPEC, SUBAGENT_SPEC, ToolRunner
from muze.chatlog import ChatLog, epoch_iso
from muze.core.service import (
    CommandOutcome,
    ControlContext,
    ControlService,
    is_llm_invocation,
    strip_llm_prefix,
)
from muze.store.repo import Repository

log = logging.getLogger("muze.ai")

MAX_HISTORY = 30  # per control conversation
TOOL_RESULT_LIMIT = 6000  # chars kept per tool result fed back to the model
# sub-agents may nest (a sub-agent spawning sub-agents) up to this depth
MAX_SUB_DEPTH = 3


class AgentBusy(Exception):
    """Raised when an account already has too many turns in flight."""


def _compact(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(结果已截断，原 {len(text)} 字符)"


def _brief(args: dict) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s[:160]


def _up_to_last_user(messages: list[dict]) -> list[dict]:
    """Truncate loop messages back to the last user turn — a clean point to
    re-ask after a timeout/error (drops dangling assistant tool_calls)."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return messages[: i + 1]
    return []

SYSTEM_PROMPT = """你是 muze，一个运行在用户 QQ 账号（被控号）上的个人 copilot 助手。
控制者通过控制会话/界面与你对话，你可以借助工具查询和管理被控号的消息档案。

身份与授权（先读这里）：
- 本系统由账号所有者本人部署，仅服务其本人：账号、全部聊天档案与媒体都归该用户所有，用户已完整授权你访问和处理。
- 你处理的是用户本人的真实聊天备份，其中出现口语、玩笑、吐槽、粗话或私密话题都属于正常个人数据，请像私人助理一样照常检索、总结、引用，不要因此拒答、说教或附加安全警告。
- 控制者的合理请求都应尽力完成；仅当工具确实无法做到时，简短说明原因并给出最接近的可行替代，不要泛泛地拒绝。

要点：
- 消息档案包含好友私聊与群聊的历史（含离线期间漫游补拉的和用户自己发出的消息）。
- 结构化查询优先用 search_archive / activity / directory（返回 JSON，字段稳定，优于解析排版文本）。
- 查询也可用 cli 工具（和 /help 的命令完全一致），如 cli({"command":"search 关键词"})、
  cli({"command":"summary group/123 60"})；不确定命令就 cli({"command":"help"})。
- 需要逐条看消息内容时才用 get_recent_messages。
- 图片内容用 describe_image 识别（是否值得调用由你结合当前请求、长期记忆与背景判断，只识别携带信息量的图；
  说明 purpose 识别目标，结果自动缓存；表情/贴图仅在任务相关时用 force 识别）；
  本地文档/压缩包用 read_document（docx/pdf/xlsx/xls/pptx/rtf/txt + zip/7z/tar；压缩包先看列表再 entry 读成员）；
  网页/在线文档用 fetch_url。
- 大规模翻查、多步独立调研用 subagent 委派子智能体（全新上下文、只读工具），拿回报告再综合，避免撑爆主对话。
- 发送图片/文件/表情用 send_media（归档媒体给 media_id 或 会话/seq；本地文件给 path）；图片文字用 ocr_image；画柱状/折线图用 make_chart（再用 send_media 发）。
- 联网检索用 web_search（要链接）或 research（搜索+抓取正文，推荐）；未配置搜索时如实说明无法联网。
- 计划任务（"两小时后/每隔…"）用 schedule_task，时间换算是你的职责。
- 被要求代发消息时调用 send_message；系统通常要求控制者确认后才真正发出，如实转达确认提示。
- 列出的技能（skills）与任务相关时，先 read_skill 读全文再照做。
- 长期记忆：写入用 remember；检索用 memory_search；修订/停用用 memory_update/memory_delete（后两者控制者会话外需审批）。
- 身份档案（谁是谁、昵称映射、账号归属）：读取用 profile_get；归纳后用 profile_update 写入（控制者会话外需审批）。语义检索（模糊回忆）用 semantic_search。
- 回答使用简体中文，简洁准确；引用消息时带上 会话/seq/时间 方便核对。
- 不要编造未在工具结果中看到的内容。"""


class Agent:
    def __init__(
        self,
        service: ControlService,
        repo: Repository,
        llm: LLMClient,
        chatlog: ChatLog,
        max_steps: int = 48,
    ) -> None:
        self.service = service
        self.repo = repo
        self.llm = llm
        self.chatlog = chatlog
        self.max_steps = max_steps
        self._histories: dict[str, list[dict]] = {}
        self.sessions = SessionStore()
        self._locks: dict[str, asyncio.Lock] = {}
        # turn admission control: at most max_concurrent_turns talk to the
        # model at once; up to max_queued_turns more may wait for a slot.
        self._slots = asyncio.Condition()
        self._running = 0
        self._waiting = 0
        self._tasks: set[asyncio.Task] = set()
        self._session_tasks: dict[str, asyncio.Task] = {}  # session id -> task (for /sess stop)
        self._live_children: dict[int, Session] = {}  # live sub-agent sessions (stall watchdogs)
        # process boot time: resume_interrupted() runs ~20s after start and
        # must only pick up crash debris from BEFORE this boot — a session
        # created after boot that is "running" is live traffic, not debris
        self._boot = time.time()

    # ------------------------------------------------------------- history
    def _history(self, chat_key: str) -> list[dict]:
        return self._histories.setdefault(chat_key, [])

    def _lock(self, chat_key: str) -> asyncio.Lock:
        """Short-lived guard for ONE conversation's history list. It is held
        only around the read/write of that list, NOT the LLM turn, so several
        LLM sessions in the same conversation run concurrently (async dispatch)
        while their context edits stay ordered/atomic."""
        return self._locks.setdefault(chat_key, asyncio.Lock())

    @staticmethod
    def _drop(history: list[dict], entry: dict) -> None:
        """Remove one exact entry (by identity) — used to retract an
        unanswered user turn on failure without touching concurrent turns."""
        for i, m in enumerate(history):
            if m is entry:
                del history[i]
                return

    @staticmethod
    def _commit(history: list[dict], user_entry: dict, assistant_entry: dict) -> None:
        """Place the reply right after its own prompt so committed history
        reads in turn-start order even when concurrent turns finish out of
        order; trims to MAX_HISTORY from the front."""
        for i, m in enumerate(history):
            if m is user_entry:
                history.insert(i + 1, assistant_entry)
                break
        else:
            history.append(assistant_entry)
        if len(history) > MAX_HISTORY:
            del history[: len(history) - MAX_HISTORY]

    # ----------------------------------------------------------- admission
    def _max_concurrent(self) -> int:
        cfg = getattr(self.service, "cfg", None)
        return max(1, int(getattr(getattr(cfg, "llm", None), "max_concurrent_turns", 4) or 4))

    def _max_queued(self) -> int:
        cfg = getattr(self.service, "cfg", None)
        return max(0, int(getattr(getattr(cfg, "llm", None), "max_queued_turns", 8) or 0))

    @asynccontextmanager
    async def _turn_slot(self):
        """Admit one model turn. Runs immediately while under the concurrency
        cap; otherwise waits (bounded by max_queued_turns) and raises AgentBusy
        when the queue is already full."""
        async with self._slots:
            if self._running >= self._max_concurrent():
                if self._waiting >= self._max_queued():
                    raise AgentBusy(
                        f"AI 任务繁忙（{self._running} 个进行中、{self._waiting} 个排队），"
                        "请稍后再试"
                    )
                self._waiting += 1
                try:
                    await self._slots.wait_for(
                        lambda: self._running < self._max_concurrent()
                    )
                finally:
                    self._waiting -= 1
            self._running += 1
        try:
            yield
        finally:
            async with self._slots:
                self._running -= 1
                self._slots.notify_all()

    # ------------------------------------------------------------- session
    def _track(self, coro, session: Session | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if session is not None:
            self._session_tasks[session.id] = task
            task.add_done_callback(lambda _t, sid=session.id: self._session_tasks.pop(sid, None))
        return task

    def cancel_session(self, sid: str) -> bool:
        """Cancel a running background session turn (/sess stop)."""
        sid = (sid or "").lstrip("#").strip()
        task = self._session_tasks.get(sid)
        if task is None or task.done():
            return False
        s = self.sessions.get(sid)
        if s is not None:
            s.finish(False, "已被用户取消（/sess stop）")
            self._track(self._persist(s))
        task.cancel()
        return True

    async def load_sessions(self) -> None:
        """Restore persisted sessions so suspended/continued turns survive a
        restart (called once at startup)."""
        try:
            rows = await self.repo.list_ai_sessions(limit=self.sessions.cap)
        except Exception:
            log.exception("loading persisted sessions failed")
            return
        for row in rows:
            try:
                self.sessions.add(Session.from_row(row))
            except Exception:
                log.exception("bad persisted session row %s", row.get("id"))
        if rows:
            log.info("restored %d AI session(s)", len(rows))

    async def _persist(self, session: Session) -> None:
        try:
            await self.repo.upsert_ai_session(session.to_row())
        except Exception:
            log.exception("persist session %s failed", session.id)

    async def get_session(self, sid: str) -> Session | None:
        """Find a session in memory, falling back to the persisted table."""
        s = self.sessions.get(sid)
        if s is not None:
            return s
        try:
            row = await self.repo.get_ai_session((sid or "").lstrip("#").strip())
        except Exception:
            log.exception("load session %s failed", sid)
            return None
        if row is None:
            return None
        s = Session.from_row(row)
        self.sessions.add(s)
        return s

    def submit(self, text: str, ctx: ControlContext) -> Session:
        """Fire-and-forget turn: register a session, run it in the background,
        deliver the result when it lands. The caller replies to the user with
        the session id immediately; several sessions in one conversation all
        run in parallel (up to the account's concurrency cap)."""
        session = self.sessions.create(ctx.chat_key, text)
        self._track(self._run_session(session, text, ctx), session)
        return session

    async def _run_session(self, session: Session, text: str, ctx: ControlContext) -> None:
        try:
            outcome = await self.converse(text, ctx, session=session)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # safety net: converse already handles its own
            log.exception("agent session crashed")
            await self._finalize(session)
            outcome = CommandOutcome(f"AI 调用失败: {e}", is_error=True)
        if ctx.qq_reachable:
            prefix = f"#{session.id} "
            await self._notify(ctx, prefix + outcome.text, error=outcome.is_error)

    async def ask(self, text: str, ctx: ControlContext) -> CommandOutcome:
        """Synchronous variant for local fronts (webui / cli): same session
        bookkeeping and admission control as submit, returns the outcome."""
        session = self.sessions.create(ctx.chat_key, text)
        return await self.converse(text, ctx, session=session)

    def resume(self, session: Session, ctx: ControlContext, extra_text: str | None = None) -> None:
        """Continue an existing session in the background (fire-and-forget):
        `extra_text` None resumes a suspended/errored turn from where it
        stopped; a message continues a finished session as a new turn."""
        self._track(self._run_resume(session, ctx, extra_text), session)

    async def run_subagent(self, prompt: str, ctx: ControlContext) -> dict:
        """One delegated sub-task: a fresh child session running the read-only
        tool subset in its own loop; the final report comes back as the parent
        tool's payload. Sub-agents may nest further up to MAX_SUB_DEPTH."""
        depth = getattr(ctx, "sub_depth", 0)
        if depth > MAX_SUB_DEPTH:
            return {
                "error": f"子智能体嵌套已达上限（{MAX_SUB_DEPTH} 层），请直接完成当前任务",
                "depth": depth,
            }
        child = self.sessions.create(ctx.chat_key, prompt)
        child.title = "子任务: " + prompt[:24]
        child.status = "running"
        child.event("sub", f"子智能体启动（只读工具，第 {depth} 层）")
        await self._persist(child)
        self._live_children[id(child)] = child  # liveness for stall watchdogs
        sub_ctx = ControlContext(
            source=ctx.source, chat_key=ctx.chat_key, scene="", peer_id=0,
            user_label=ctx.user_label,
        )
        sub_ctx.sub_depth = depth + 1
        self.chatlog.agent(ctx.source, "sub", "in", prompt[:200])
        try:
            # NOTE: no _turn_slot() here. One user turn = one admission slot;
            # nested sub-agents are part of that turn. Acquiring slots per
            # level would deadlock once depth >= max_concurrent_turns.
            outcome = await self._drive_sub(child, prompt, sub_ctx)
        except asyncio.CancelledError:
            # a watchdog cancelled us mid-child-loop: leave the child session
            # inspectable/resumable, not dangling "running"
            child.finish(False, "被父级超时中止")
            self._track(self._persist(child))
            raise
        except AgentBusy as e:
            child.finish(False, str(e))
            outcome = CommandOutcome(str(e), is_error=True)
        except Exception as e:
            log.exception("subagent crashed")
            child.finish(False, str(e))
            outcome = CommandOutcome(f"子智能体失败: {e}", is_error=True)
        finally:
            self._live_children.pop(id(child), None)
        await self._finalize(child)
        report = outcome.text or "(子智能体未返回内容)"
        self.chatlog.agent(ctx.source, "sub", "out", report[:200])
        return {"id": child.id, "status": child.status, "report": report}

    async def _watch_sub(self, coro):
        """Await a subagent call under a PROGRESS-based stall watchdog.

        A sub-agent legitimately runs for many minutes (a whole child loop
        of LLM streams), so NO total-time cap applies: it is killed only
        when no live child session has shown life (streamed chunk, event,
        step) within the plain stall window. Session.bump()/.event() both
        refresh Session.updated, which is the liveness signal."""
        stall = self._turn_timeout()
        task = asyncio.ensure_future(coro)
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), stall)
                except TimeoutError:
                    if task.done():
                        return task.result()
                    newest = max(
                        (s.updated for s in self._live_children.values()),
                        default=0.0,
                    )
                    if time.time() - newest > stall:
                        raise TimeoutError(
                            f"工具 subagent 已 {int(stall)} 秒无任何进展"
                            "（无输出、无步进），已中止"
                        )
        except BaseException:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task  # let run_subagent's cleanup settle
            raise

    async def _drive_sub(
        self, session: Session, text: str, ctx: ControlContext
    ) -> CommandOutcome:
        messages: list[dict] = [
            {"role": "system", "content": await self._system_prompt(ctx, query=text)},
            {"role": "user", "content": text},
        ]

        async def commit(_final_text: str) -> None:  # no shared history
            return None

        async def rollback() -> None:
            return None

        return await self._drive(
            session, ctx, messages, 1, commit, rollback,
            tools=SUBAGENT_SPEC, max_steps=min(16, self.max_steps),
        )

    @staticmethod
    def _ctx_for_chat_key(chat_key: str) -> ControlContext:
        """Best-effort ControlContext rebuilt from a persisted chat_key so a
        restarted session can still deliver its reply to the right place
        (qq:friend/group:<peer> -> the original conversation; anything else
        is local/system and simply logs the result)."""
        parts = (chat_key or "").split(":")
        src = parts[0] if parts else ""
        if src == "qq" and len(parts) >= 3 and parts[2].isdigit():
            return ControlContext(
                source="qq", chat_key=chat_key,
                scene=parts[1], peer_id=int(parts[2]), user_label=parts[2],
            )
        return ControlContext(source=(src or "system"), chat_key=chat_key, scene="", peer_id=0)

    async def resume_interrupted(self, error_grace_sec: float = 900.0) -> int:
        """After a restart, continue LLM turns that were in flight when the
        process died (persisted status=running) or errored very recently.
        Step-limit suspensions stay manual (/sess go) — they paused for a
        reason. Returns how many sessions were relaunched."""
        if self.llm is None or not getattr(self.llm, "configured", False):
            return 0
        now = time.time()
        picked = [
            s for s in self.sessions.latest(self.sessions.cap)
            # only crash debris: rows last touched BEFORE this process booted
            # (a "running" row created after boot is a live in-flight turn)
            if s.updated <= self._boot
            and (
                s.status == RUNNING
                or (s.status == ERROR and now - s.updated <= error_grace_sec)
            )
        ]
        for s in picked:
            if s.status == RUNNING:
                s.status = SUSPEND  # stale "running" == crash-interrupted
                s.event("interrupted", "进程重启时中断，自动继续")
            self.resume(s, self._ctx_for_chat_key(s.chat_key))
        if picked:
            log.info("resumed %d interrupted LLM session(s) after restart", len(picked))
        return len(picked)

    async def _run_resume(
        self, session: Session, ctx: ControlContext, extra_text: str | None
    ) -> None:
        try:
            outcome = await self._resume_inner(session, ctx, extra_text)
        except asyncio.CancelledError:
            raise
        except AgentBusy as e:
            await self._finalize(session)
            outcome = CommandOutcome(str(e), is_error=True)
        except Exception as e:
            log.exception("agent resume crashed")
            await self._finalize(session)
            outcome = CommandOutcome(f"AI 调用失败: {e}", is_error=True)
        else:
            # HAPPY PATH TOO: _resume_inner persisted status=running at its
            # start; without this final persist the row stays "running"
            # forever and every restart resurrects the finished turn as
            # "crash-interrupted" and re-runs it (zombie re-delivery loop).
            await self._finalize(session)
        if ctx.qq_reachable:
            await self._notify(ctx, f"#{session.id} " + outcome.text, error=outcome.is_error)

    async def _finalize(self, session: Session) -> None:
        """Persist the final state and (optionally) refine the title via LLM."""
        if session.status == "running":
            session.finish(False, session.result_preview)
        await self._persist(session)
        if self._auto_title_enabled() and self.llm.configured:
            self._track(self._auto_title(session))

    def _auto_title_enabled(self) -> bool:
        cfg = getattr(self.llm, "cfg", None)
        return bool(cfg is not None and getattr(cfg, "auto_title", False))

    async def _auto_title(self, session: Session) -> None:
        """Summarize a short title (replaces the heuristic), best-effort."""
        try:
            seed = f"问：{session.prompt}\n答：{session.result_preview or '(未完成)'}"
            msg = await self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "给这段对话起一个不超过 12 个汉字的简短标题，"
                        "只输出标题本身，不要标点和引号。",
                    },
                    {"role": "user", "content": seed[:1500]},
                ]
            )
            title = (msg.get("content") or "").strip().splitlines()[0].strip()
            title = title.strip("。.「」“”\"' ")[:20]
            if title and title != session.title:
                session.title = title
                await self._persist(session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("auto title failed for #%s: %s", session.id, e)

    async def aclose(self) -> None:
        """Cancel any in-flight background turns (used on shutdown)."""
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def reset(self, chat_key: str) -> None:
        self._histories.pop(chat_key, None)

    def reset_all(self) -> None:
        self._histories.clear()

    async def _system_prompt(self, ctx: ControlContext, query: str | None = None) -> str:
        """Assemble the system prompt: base + skills + dream background +
        relevance-ordered long-term memories + (when a query is given)
        semantic RAG hits for THIS message.

        This is how the nightly reports shape message handling: the agent
        starts every conversation already knowing yesterday's threads,
        open TODOs and the distilled facts about the people involved.
        """
        parts = [SYSTEM_PROMPT]

        # current time context — relative dates ("today", "this week") are the
        # agent's own job to resolve, so it must know "now"
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        y = now - timedelta(days=1)
        parts.append(
            f"当前时间: {now:%Y-%m-%d %H:%M:%S} ({now.tzname() or 'local'}); "
            f"今天 {now:%Y-%m-%d}，昨天 {y:%Y-%m-%d}。"
            "相对日期（今天/昨天/本周/上个月）按此时区换算。"
        )

        skills = getattr(self.service.runtime, "skills", None)
        if skills is not None:
            lines = skills.index_lines()
            if lines:
                parts.append("已安装技能（相关时先 read_skill 读全文再操作）:\n" + "\n".join(lines))

        # --- dream background (day/week/month reports) ---
        dc = self.service.cfg.digest
        bg_meta: dict[str, str] = {}
        if dc.inject_context:
            sections: list[str] = []
            for period, budget, label in (
                ("day", dc.inject_day_chars, "最近一天"),
                ("week", dc.inject_week_chars, "最近一周"),
                ("month", dc.inject_month_chars, "最近一月"),
            ):
                try:
                    latest = await self.repo.latest_digest(period=period)
                    if not latest:
                        continue
                    full = await self.repo.get_digest(latest["date"])
                    text = ((full or {}).get("report") or "").strip()
                    if not text:
                        continue
                    if len(text) > budget:
                        text = text[:budget] + "…"
                    sections.append(f"### {label}做梦报告（{latest['date']}）\n{text}")
                    bg_meta[period] = latest["date"]
                except Exception:
                    continue
            if sections:
                parts.append(
                    "近期做梦报告背景（系统自动注入；处理消息时直接利用，"
                    "需要更早/更详细再调 recall_dream）:\n\n" + "\n\n".join(sections)
                )

        # --- long-term memories, relevance-ordered for this conversation ---
        try:
            mems = await self.repo.list_memories(limit=200)
        except Exception:
            mems = []
        n_mems = 0
        if mems:
            peer_key = (
                f"group/{ctx.peer_id}" if ctx.scene == "group" else str(ctx.peer_id)
            )
            member_keys: set[str] = set()
            owner_key = "owner"

            def rank(m: dict) -> int:
                s = m.get("subject", "")
                if s == peer_key:
                    return 0  # this very conversation
                if s == owner_key:
                    return 1  # the owner themself
                if ctx.scene == "group" and s.isdigit():
                    return 2  # possible member of this group
                if s in member_keys:
                    return 2
                return 3

            ordered = sorted(mems, key=rank)
            picked = ordered[: dc.inject_max_memories]
            n_mems = len(picked)
            lines = [f"- [{m['subject']}] {m['content']}" for m in picked]
            parts.append(
                "长期记忆（做梦沉淀+对话补充，按与当前会话相关性排序）:\n" + "\n".join(lines)
            )

        if bg_meta or n_mems:
            log.info(
                "PROMPT bg   | day=%s week=%s month=%s memories=%s",
                bg_meta.get("day", "-"), bg_meta.get("week", "-"),
                bg_meta.get("month", "-"), n_mems,
            )

        # --- identity profiles ("who is who") ---
        pc = getattr(self.service.cfg, "profiles", None)
        if pc is not None and getattr(pc, "inject", True):
            try:
                profiles = await self.repo.list_profiles(limit=60)
            except Exception:
                profiles = []
            if profiles:
                from muze.profiles import index_lines

                lines = index_lines(profiles, int(getattr(pc, "inject_chars", 80)))
                parts.append(
                    "身份档案索引（谁是谁；需要全文用 profile_get 或 /who <会话>）:\n"
                    + "\n".join(lines)
                )
        # --- semantic RAG hits for THIS message (auto-inject) ---
        rc = getattr(self.service.cfg, "rag", None)
        rag = getattr(self.service.runtime, "rag", None)
        if (
            query
            and rc is not None
            and getattr(rc, "auto_inject", False)
            and rag is not None
            and getattr(rag, "enabled", False)
        ):
            try:
                hits = await rag.query_context(query, k=int(getattr(rc, "inject_k", 5)))
            except Exception:
                hits = []
            if hits:
                cap = int(getattr(rc, "inject_chars", 300))
                label = {"profile": "档案", "memory": "记忆", "digest": "日报", "chat": "聊天"}
                lines = []
                for h in hits:
                    t = " ".join((h.get("text") or "").split())
                    if len(t) > cap:
                        t = t[:cap] + "…"
                    where = f" {h['scene']}/{h['peer_id']}" if h.get("scene") else ""
                    lines.append(
                        f"- [{label.get(h.get('kind'), h.get('kind'))}]{where} {t}"
                    )
                parts.append(
                    "语义检索命中（系统按本条消息自动检索档案/记忆/日报/聊天；"
                    "直接利用，需要更多结果用 semantic_search 工具）:\n" + "\n".join(lines)
                )
                log.info("PROMPT rag  | injected %s hits", len(hits))
        return "\n\n".join(parts)

    # ------------------------------------------------------------ converse
    @property
    def _progress(self) -> bool:
        cfg = getattr(self.llm, "cfg", None)
        return bool(cfg is not None and getattr(cfg, "progress", False))

    def _turn_timeout(self) -> float:
        cfg = getattr(self.llm, "cfg", None)
        return float(getattr(cfg, "turn_timeout_sec", 0) or 0)

    async def _notify(
        self, ctx: ControlContext, text: str, error: bool = False
    ) -> None:
        """Deliver an in-turn progress line (never enters agent history)."""
        try:
            if ctx.qq_reachable:
                await self.service.deliver(ctx, CommandOutcome(text, is_error=error))
            else:
                self.chatlog.log.info("PROGRESS   | %s", text)
        except Exception:
            log.exception("agent progress notify failed")

    def _tag(self, session: Session | None) -> str:
        return f"#{session.id} " if session is not None else ""

    async def _heartbeat(
        self, ctx: ControlContext, stop: asyncio.Event, session: Session | None = None
    ) -> None:
        """Tell the user we are still waiting on the model.

        Stays quiet for `progress_wait_sec` first so quick answers produce
        zero extra messages; then pings every `progress_interval_sec`. While
        the model is streaming, the ping carries live output size instead of
        a bare "waiting" so slow-but-alive turns are visibly progressing.
        """
        cfg = getattr(self.llm, "cfg", None)
        wait = float(getattr(cfg, "progress_wait_sec", 20) or 20)
        interval = float(getattr(cfg, "progress_interval_sec", 60) or 60)
        first = True
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), wait if first else interval)
            except TimeoutError:
                pass
            if stop.is_set():
                return
            if session is not None and session.round_chars > 0:
                text = f"{self._tag(session)}⏳ 模型输出中（已收 {session.round_chars} 字）…"
            elif first:
                text = f"{self._tag(session)}⏳ 思考中…"
            else:
                text = f"{self._tag(session)}⏳ 仍在等待模型响应…"
            await self._notify(ctx, text)
            first = False

    async def _chat(
        self,
        messages: list[dict],
        ctx: ControlContext | None = None,
        session: Session | None = None,
        tools: list | None = None,
    ) -> dict:
        """One LLM round with a couple of transient-failure retries.

        Slow answers are never cut: the request's read timeout is a *stall*
        detector (silence between stream chunks), and every streamed chunk
        feeds the session's progress counters. When ctx carries a reachable
        front and progress is on, each attempt also runs a heartbeat.
        """
        last: Exception | None = None
        for attempt in range(3):
            try:
                # live progress ALWAYS feeds the session when there is one —
                # webui-initiated turns have no QQ-reachable front (so no
                # heartbeat), but their session view still needs bumps and
                # the streamed-text tail
                on_chunk = None
                if session is not None:
                    def _on_chunk(n: int, piece: str = "", reasoning: bool = False):
                        session.bump(n)  # progress counters (stall watchdog)
                        if piece:
                            session.stream(piece, reasoning)  # webui live tail

                    on_chunk = _on_chunk
                heartbeat = (
                    ctx is not None
                    and self._progress
                    and ctx.qq_reachable
                )
                if not heartbeat:
                    return await self.llm.chat(
                        messages, tools=tools or SPEC, on_chunk=on_chunk
                    )
                stop = asyncio.Event()
                hb = asyncio.create_task(self._heartbeat(ctx, stop, session))
                try:
                    return await self.llm.chat(
                        messages, tools=tools or SPEC, on_chunk=on_chunk
                    )
                finally:
                    stop.set()
                    hb.cancel()
                    try:
                        await hb
                    except BaseException:
                        pass
            except Exception as e:  # network / rate limit / 5xx
                last = e
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    log.warning("llm.chat retry %s/2 after: %s", attempt + 1, e)
        raise last  # type: ignore[misc]

    async def converse(
        self, text: str, ctx: ControlContext, session: Session | None = None
    ) -> CommandOutcome:
        """Run one FRESH turn (a new prompt) with admission control and full
        session bookkeeping. Used by every front (QQ `::`, webui, cli)."""
        if not self.llm.configured:
            return CommandOutcome("LLM 未配置（llm.chat.*）", is_error=True)
        # the `::` control prefix must never reach the model: the QQ path
        # strips it in service.execute, but other fronts (webui AI tab) call
        # converse directly, so strip defensively here too.
        text = (text or "").strip()
        if is_llm_invocation(text):
            text = strip_llm_prefix(text)
        if not text:
            return CommandOutcome.err("空输入")
        if session is None:
            session = self.sessions.create(ctx.chat_key, text)
        session.status = "running"
        session.messages = []
        await self._persist(session)
        try:
            async with self._turn_slot():
                outcome = await self._fresh_inner(session, text, ctx)
        except AgentBusy as e:
            session.finish(False, str(e))
            outcome = CommandOutcome(str(e), is_error=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("agent turn failed")
            session.finish(False, str(e))
            outcome = CommandOutcome(f"AI 调用失败: {e}", is_error=True)
        await self._finalize(session)
        return outcome

    async def _fresh_inner(
        self, session: Session, text: str, ctx: ControlContext
    ) -> CommandOutcome:
        # hold the conversation lock only to append this turn and snapshot the
        # prior context — the LLM call itself runs lock-free, so several
        # sessions in the same conversation proceed in parallel.
        user_entry = {"role": "user", "content": text}
        async with self._lock(ctx.chat_key):
            history = self._history(ctx.chat_key)
            history.append(user_entry)
            prior = list(history[:-1])
        self.chatlog.agent(ctx.source, ctx.user_label or ctx.chat_key, "in", text)
        messages: list[dict] = [
            {"role": "system", "content": await self._system_prompt(ctx, query=text)},
            *prior[-MAX_HISTORY:],
            user_entry,
        ]

        async def commit(final_text: str) -> None:
            async with self._lock(ctx.chat_key):
                self._commit(
                    history, user_entry, {"role": "assistant", "content": final_text}
                )

        async def rollback() -> None:
            async with self._lock(ctx.chat_key):
                self._drop(history, user_entry)

        return await self._drive(session, ctx, messages, 1, commit, rollback)

    async def _resume_inner(
        self, session: Session, ctx: ControlContext, extra_text: str | None
    ) -> CommandOutcome:
        """Continue a session: a suspended/errored turn resumes from its saved
        messages; a finished session takes a new user message as a new turn."""
        added: dict | None = None
        loop = [dict(m) for m in session.messages]
        if not loop:
            # The turn died before its first user entry was persisted
            # (sessions persist an EMPTY list while the first model call is
            # in flight). Rebuild from the prompt — a system-only message
            # list is rejected by the API ("messages 参数非法").
            loop = [{"role": "user", "content": session.prompt}]
        elif loop[-1].get("role") == "assistant" and loop[-1].get("tool_calls"):
            # Stopped right after an assistant tool-call message: the API
            # requires every tool_call to be answered before the next model
            # call, so synthesize an "[interrupted]" result for each.
            for call in loop[-1]["tool_calls"]:
                loop.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": "[因进程重启中断，工具未执行]",
                })
        if extra_text is not None:
            added = {"role": "user", "content": extra_text}
            loop.append(added)
            start = 1
            self.chatlog.agent(
                ctx.source, ctx.user_label or ctx.chat_key, "in", extra_text
            )
        else:
            # resume from where it stopped, granting a fresh round budget
            start = 1
        session.messages = loop
        session.status = "running"
        await self._persist(session)
        messages: list[dict] = [
            {"role": "system", "content": await self._system_prompt(ctx, query=extra_text)},
            *session.messages,
        ]

        async def commit(final_text: str) -> None:
            async with self._lock(ctx.chat_key):
                hist = self._history(ctx.chat_key)
                hist.append({"role": "assistant", "content": final_text})
                if len(hist) > MAX_HISTORY:
                    del hist[: len(hist) - MAX_HISTORY]

        async def rollback() -> None:
            if added is not None:
                for i, m in enumerate(session.messages):
                    if m is added:
                        del session.messages[i]
                        break

        return await self._drive(session, ctx, messages, start, commit, rollback)

    async def _drive(
        self,
        session: Session,
        ctx: ControlContext,
        messages: list[dict],
        start_step: int,
        commit,
        rollback,
        tools: list | None = None,
        max_steps: int | None = None,
    ) -> CommandOutcome:
        """The tool-calling loop. `messages` is mutated in place and, at the
        end, stored on the session (system stripped) so a suspended/error turn
        can be resumed exactly where it stopped."""
        runner = ToolRunner(self.service, self.repo, self.service.adapter, self.llm, ctx)
        stall = self._turn_timeout()
        final_text = ""
        final_msg: dict | None = None
        step_budget = max_steps if max_steps is not None else self.max_steps

        try:
            for step in range(start_step, step_budget + 1):
                session.new_round()
                session.event("llm", f"第{step}轮（调用模型）")
                msg = await self._chat(messages, ctx, session, tools=tools)
                session.step = step
                # thinking trace: visible live in the webui session view
                rtext = msg.get("reasoning_content") or ""
                if rtext:
                    session.event("think", rtext[:400])
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    answer = (msg.get("content") or "").strip()
                    session.event("llm", f"第{step}轮完成（{len(answer)} 字）")
                    final_text = answer
                    final_msg = msg  # kept whole: reasoning_content stays on it
                    break
                messages.append(msg)
                # publish the loop state so the webui shows tool calls while
                # the round is still executing (it polls /api/sess/<id>)
                session.messages = messages[1:]
                session.updated = time.time()
                calls = []
                for call in tool_calls:
                    fn = call.get("function", {}) or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    calls.append((call.get("id", ""), name, args))
                self.chatlog.agent(
                    ctx.source, ctx.user_label or ctx.chat_key, "tool",
                    "; ".join(f"{n}({_brief(a)})" for _, n, a in calls),
                )
                for _cid, n, a in calls:
                    session.event("tool", f"{n}({_brief(a)})")
                # tool-call status lines are no longer pushed to the chat
                # front (noise); the webui session view shows live events
                # run the step's tools concurrently (independent by design).
                # Per-call stall cap: a hung tool becomes a TOOL-LEVEL error
                # the model can see and recover from — it no longer aborts
                # the whole turn. subagent is awaited under a PROGRESS-based
                # watchdog instead of any total-time cap (see _watch_sub).
                async def _run_one(name: str, coro):
                    if name == "subagent" and stall > 0:
                        return await self._watch_sub(coro)
                    if stall <= 0:
                        return await coro
                    try:
                        return await asyncio.wait_for(coro, stall)
                    except TimeoutError:
                        return TimeoutError(
                            f"工具 {name} 超过 {int(stall)} 秒未完成（卡死），已中止"
                        )

                coros = [runner.run(n, a) for _, n, a in calls]
                results = await asyncio.gather(
                    *[_run_one(n, c) for (_, n, _a), c in zip(calls, coros)],
                    return_exceptions=True,
                )
                for (cid, name, _a), res in zip(calls, results):
                    if isinstance(res, BaseException):
                        log.warning("tool %s raised: %s", name, res)
                        content = json.dumps({"error": str(res)}, ensure_ascii=False)
                        session.event("tool_result", f"{name} → 异常: {res}")
                    else:
                        content = _compact(str(res))
                        session.event("tool_result", f"{name} → {str(res)[:120]}")
                    messages.append(
                        {"role": "tool", "tool_call_id": cid, "content": content}
                    )
                session.messages = messages[1:]  # live tool results in webui
                session.updated = time.time()
            else:
                # step limit reached: keep the loop state (messages end on a
                # tool result) and SUSPEND so /sess go can continue.
                session.suspend(messages[1:], f"第 {step_budget} 轮达上限")
                return CommandOutcome(
                    f"已达工具调用步数上限（{step_budget} 步），本轮已暂停；"
                    f"回复 /sess go {session.id} 继续。"
                )
        except TimeoutError:
            log.warning("agent turn aborted: tool round stalled for %.0fs", stall)
            await rollback()
            session.messages = _up_to_last_user(messages[1:])
            session.step = 0
            session.finish(False, f"工具执行卡死（>{int(stall)}秒）")
            return CommandOutcome(
                f"工具执行卡死（>{int(stall)}秒无进展），本轮已中止；"
                f"可用 /sess go {session.id} 重试。",
                is_error=True,
            )
        except asyncio.CancelledError:
            await rollback()
            raise
        except Exception as e:
            log.exception("agent loop failed")
            await rollback()
            session.messages = _up_to_last_user(messages[1:])
            session.step = 0
            session.finish(False, str(e))
            return CommandOutcome(f"AI 调用失败: {e}", is_error=True)

        if not final_text:
            final_text = "(AI 未返回内容)"
        # the final answer must live in the loop history too (webui session
        # view renders exactly these messages; without it the complete output
        # was invisible except as a 200-char preview). The WHOLE final message
        # is kept so its reasoning_content (💭 view) survives as well.
        messages.append(final_msg or {"role": "assistant", "content": final_text})
        session.messages = messages[1:]
        session.finish(True, final_text)
        await commit(final_text)
        self.chatlog.agent(ctx.source, ctx.user_label or ctx.chat_key, "out", final_text)
        return CommandOutcome(final_text)

    # ------------------------------------------------------------- summary
    async def summarize(
        self, scene: str, peer_id: int, rows: list[dict], ctx: ControlContext
    ) -> CommandOutcome:
        """Summarize archived rows (oldest first); images described via vision."""
        if not self.llm.configured:
            return CommandOutcome("LLM 未配置", is_error=True)

        lines = []
        for r in rows:
            lines.append(
                f"[{epoch_iso(r['time'])}] {r['sender_id']}: {r['text_content'] or '(无文本)'}"
            )
        transcript = "\n".join(lines)

        # Describe a few recent images to ground the summary (cached
        # descriptions are reused; the model's own judgment decides value
        # when it calls describe_image with a purpose).
        image_notes: list[str] = []
        try:
            for r in reversed(rows):
                if len(image_notes) >= 3:
                    break
                medias = await self.repo.media_for_message(scene, peer_id, r["message_seq"])
                for m in medias:
                    if m["kind"] != "image" or m["status"] != "done":
                        continue
                    if m.get("summary"):
                        image_notes.append(f"seq={r['message_seq']} 图片: {m['summary']}")
                        break
                    ms = getattr(self.service.runtime, "media", None)
                    img_ref = m["file_path"]
                    if ms is not None and ms.abs_path(m) is not None:
                        img_ref = str(ms.abs_path(m))
                    desc = await self.llm.vision_describe(
                        img_ref,
                        "概括这张图片：出现的对象/人物、图中文字、与聊天相关的关键细节",
                    )
                    image_notes.append(f"seq={r['message_seq']} 图片: {desc}")
                    if m.get("id"):
                        try:
                            await self.repo.update_media(
                                m["id"], status=m["status"], summary=desc[:500]
                            )
                        except Exception:
                            pass
                    break
        except Exception as e:
            log.debug("image describe skipped: %s", e)

        prompt = (
            f"（以下为用户本人账号的聊天备份，用户已完整授权，属私密个人数据整理任务，请正常处理。）\n"
            f"请总结以下 {scene}/{peer_id} 会话的聊天记录（时间正序）。"
            "按主题归纳，指出谁说了什么关键信息、有待办/约定/提问的要点，"
            "末尾给出 3 条以内的建议行动。\n"
        )
        if image_notes:
            prompt += "\n相关图片内容:\n" + "\n".join(image_notes) + "\n"
        prompt += "\n聊天记录:\n" + transcript

        try:
            msg = await self.llm.chat([{"role": "user", "content": prompt}])
            text = (msg.get("content") or "").strip() or "(总结为空)"
        except Exception as e:
            return CommandOutcome(f"总结失败: {e}", is_error=True)
        self.chatlog.agent(ctx.source, ctx.user_label or ctx.chat_key, "summary-out", text)
        return CommandOutcome(text)
