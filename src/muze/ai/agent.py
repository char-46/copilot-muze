"""Conversational agent over the message archive (tool-calling loop)."""

from __future__ import annotations

import asyncio
import json
import logging

from muze.ai.llm import LLMClient
from muze.ai.sessions import Session, SessionStore
from muze.ai.tools import SPEC, ToolRunner
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

SYSTEM_PROMPT = """你是 muze，一个运行在用户 QQ 账号（被控号）上的个人 copilot 助手。
控制者通过控制会话/界面与你对话，你可以借助工具查询和管理被控号的消息档案。

要点：
- 消息档案包含好友私聊与群聊的历史（含离线期间漫游补拉的和用户自己发出的消息）。
- 结构化查询优先用 search_archive / activity / directory（返回 JSON，字段稳定，优于解析排版文本）。
- 查询也可用 cli 工具（和 /help 的命令完全一致），如 cli({"command":"search 关键词"})、
  cli({"command":"summary group/123 60"})；不确定命令就 cli({"command":"help"})。
- 需要逐条看消息内容时才用 get_recent_messages。
- 图片内容用 describe_image 识别；本地文档用 read_document；网页/在线文档用 fetch_url。
- 发送图片/文件/表情用 send_media（归档媒体给 media_id 或 会话/seq；本地文件给 path）；图片文字用 ocr_image；画柱状/折线图用 make_chart（再用 send_media 发）。
- 联网检索用 web_search（要链接）或 research（搜索+抓取正文，推荐）；未配置搜索时如实说明无法联网。
- 计划任务（"两小时后/每隔…"）用 schedule_task，时间换算是你的职责。
- 被要求代发消息时调用 send_message；系统通常要求控制者确认后才真正发出，如实转达确认提示。
- 列出的技能（skills）与任务相关时，先 read_skill 读全文再照做。
- 长期记忆：写入用 remember；检索用 memory_search；修订/停用用 memory_update/memory_delete（后两者控制者会话外需审批）。
- 回答使用简体中文，简洁准确；引用消息时带上 会话/seq/时间 方便核对。
- 不要编造未在工具结果中看到的内容。"""


class Agent:
    def __init__(
        self,
        service: ControlService,
        repo: Repository,
        llm: LLMClient,
        chatlog: ChatLog,
        max_steps: int = 8,
    ) -> None:
        self.service = service
        self.repo = repo
        self.llm = llm
        self.chatlog = chatlog
        self.max_steps = max_steps
        self._histories: dict[str, list[dict]] = {}
        self.sessions = SessionStore()
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------- history
    def _history(self, chat_key: str) -> list[dict]:
        return self._histories.setdefault(chat_key, [])

    def _lock(self, chat_key: str) -> asyncio.Lock:
        """Serialize turns within ONE conversation; different conversations
        run concurrently (async dispatch)."""
        return self._locks.setdefault(chat_key, asyncio.Lock())

    # ------------------------------------------------------------- session
    def submit(self, text: str, ctx: ControlContext) -> Session:
        """Fire-and-forget turn: register a session, run it in the background
        (per-conversation lock), deliver the result when it lands. The caller
        replies to the user with the session id immediately."""
        session = self.sessions.create(ctx.chat_key, text)
        asyncio.create_task(self._run_session(session, text, ctx))
        return session

    async def _run_session(self, session: Session, text: str, ctx: ControlContext) -> None:
        async with self._lock(ctx.chat_key):
            try:
                outcome = await self.converse(text, ctx, session=session)
            except Exception as e:  # safety net: converse already handles its own
                log.exception("agent session crashed")
                outcome = CommandOutcome(f"AI 调用失败: {e}", is_error=True)
        session.finish(ok=not outcome.is_error, preview=outcome.text)
        if ctx.qq_reachable:
            prefix = f"#{session.id} "
            await self._notify(ctx, prefix + outcome.text, error=outcome.is_error)

    async def ask(self, text: str, ctx: ControlContext) -> CommandOutcome:
        """Synchronous variant for local fronts (webui / cli): same session
        bookkeeping and per-conversation lock, returns the final outcome."""
        session = self.sessions.create(ctx.chat_key, text)
        async with self._lock(ctx.chat_key):
            outcome = await self.converse(text, ctx, session=session)
        session.finish(ok=not outcome.is_error, preview=outcome.text)
        return outcome

    def reset(self, chat_key: str) -> None:
        self._histories.pop(chat_key, None)

    def reset_all(self) -> None:
        self._histories.clear()

    async def _system_prompt(self, ctx: ControlContext) -> str:
        """Assemble the system prompt: base + skills + dream background +
        relevance-ordered long-term memories.

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
                heartbeat = (
                    ctx is not None
                    and self._progress
                    and ctx.qq_reachable
                )
                if not heartbeat:
                    return await self.llm.chat(messages, tools=SPEC)
                stop = asyncio.Event()
                hb = asyncio.create_task(self._heartbeat(ctx, stop, session))
                try:
                    if session is None:
                        return await self.llm.chat(messages, tools=SPEC)
                    return await self.llm.chat(
                        messages, tools=SPEC, on_chunk=session.bump
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
        history = self._history(ctx.chat_key)
        history.append({"role": "user", "content": text})
        self.chatlog.agent(ctx.source, ctx.user_label or ctx.chat_key, "in", text)

        runner = ToolRunner(self.service, self.repo, self.service.adapter, self.llm, ctx)
        messages: list[dict] = [
            {"role": "system", "content": await self._system_prompt(ctx)},
            *history[:-1][-MAX_HISTORY:],
            {"role": "user", "content": text},
        ]

        final_text = ""
        stall = self._turn_timeout()

        async def tool_loop() -> str:
            for step in range(1, self.max_steps + 1):
                if session is not None:
                    session.new_round()
                    session.event("llm", f"第{step}轮（调用模型）")
                msg = await self._chat(messages, ctx, session)
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    answer = (msg.get("content") or "").strip()
                    if session is not None:
                        session.event("llm", f"第{step}轮完成（{len(answer)} 字）")
                    return answer
                messages.append(msg)
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
                if session is not None:
                    for _cid, n, a in calls:
                        session.event("tool", f"{n}({_brief(a)})")
                if self._progress and ctx.qq_reachable:
                    names = "、".join(dict.fromkeys(n for _, n, _ in calls if n))
                    await self._notify(ctx, f"{self._tag(session)}🔧 {names}")
                # run the step's tools concurrently (independent by design);
                # stall cap catches tools that hang — NOT slow LLM output,
                # which is bounded only by silence between stream chunks
                coros = [runner.run(n, a) for _, n, a in calls]
                results = (
                    await asyncio.wait_for(
                        asyncio.gather(*coros, return_exceptions=True), stall
                    )
                    if stall > 0
                    else await asyncio.gather(*coros, return_exceptions=True)
                )
                for (cid, name, _a), res in zip(calls, results):
                    if isinstance(res, BaseException):
                        log.warning("tool %s raised: %s", name, res)
                        content = json.dumps({"error": str(res)}, ensure_ascii=False)
                        if session is not None:
                            session.event("tool_result", f"{name} → 异常: {res}")
                    else:
                        content = _compact(str(res))
                        if session is not None:
                            session.event("tool_result", f"{name} → {str(res)[:120]}")
                    messages.append(
                        {"role": "tool", "tool_call_id": cid, "content": content}
                    )
            return ""  # step limit exhausted

        try:
            final_text = await tool_loop()
            if not final_text:
                final_text = (
                    "（已达工具调用步数上限，已用现有结果作答；"
                    "如仍需更多步骤，请缩小范围或回复 ::继续）"
                )
        except TimeoutError:
            log.warning("agent turn aborted: tool round stalled for %.0fs", stall)
            history.pop()  # drop the unfinished turn
            if session is not None:
                session.event("error", f"工具执行卡死（>{int(stall)}秒）")
            return CommandOutcome(
                f"工具执行卡死（>{int(stall)}秒无进展），本轮已中止；请稍后重试。",
                is_error=True,
            )
        except Exception as e:
            log.exception("agent loop failed")
            history.pop()  # drop failed turn
            return CommandOutcome(f"AI 调用失败: {e}", is_error=True)

        if not final_text:
            final_text = "(AI 未返回内容)"
        history.append({"role": "assistant", "content": final_text})
        if len(history) > MAX_HISTORY:
            del history[: len(history) - MAX_HISTORY]
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

        # Describe a few recent images to ground the summary.
        image_notes: list[str] = []
        try:
            for r in reversed(rows):
                if len(image_notes) >= 3:
                    break
                medias = await self.repo.media_for_message(scene, peer_id, r["message_seq"])
                for m in medias:
                    if m["kind"] == "image" and m["status"] == "done":
                        desc = await self.llm.vision_describe(
                            m["file_path"], "一两句话概括这张图片"
                        )
                        image_notes.append(f"seq={r['message_seq']} 图片: {desc}")
                        break
        except Exception as e:
            log.debug("image describe skipped: %s", e)

        prompt = (
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
