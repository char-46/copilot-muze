"""Conversational agent over the message archive (tool-calling loop)."""

from __future__ import annotations

import asyncio
import json
import logging

from muze.ai.llm import LLMClient
from muze.ai.tools import SPEC, ToolRunner
from muze.chatlog import ChatLog, epoch_iso
from muze.core.service import CommandOutcome, ControlContext, ControlService
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
- 联网检索用 web_search（要链接）或 research（搜索+抓取正文，推荐）；未配置搜索时如实说明无法联网。
- 计划任务（"两小时后/每隔…"）用 schedule_task，时间换算是你的职责。
- 被要求代发消息时调用 send_message；系统通常要求控制者确认后才真正发出，如实转达确认提示。
- 列出的技能（skills）与任务相关时，先 read_skill 读全文再照做。
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

    # ------------------------------------------------------------- history
    def _history(self, chat_key: str) -> list[dict]:
        return self._histories.setdefault(chat_key, [])

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
    async def _chat(self, messages: list[dict]) -> dict:
        """One LLM round with a couple of transient-failure retries."""
        last: Exception | None = None
        for attempt in range(3):
            try:
                return await self.llm.chat(messages, tools=SPEC)
            except Exception as e:  # network / rate limit / 5xx
                last = e
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    log.warning("llm.chat retry %s/2 after: %s", attempt + 1, e)
        raise last  # type: ignore[misc]

    async def converse(self, text: str, ctx: ControlContext) -> CommandOutcome:
        if not self.llm.configured:
            return CommandOutcome("LLM 未配置（llm.chat.*）", is_error=True)
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
        try:
            for _ in range(self.max_steps):
                msg = await self._chat(messages)
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    final_text = (msg.get("content") or "").strip()
                    break
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
                # run the step's tools concurrently (independent by design)
                results = await asyncio.gather(
                    *(runner.run(n, a) for _, n, a in calls),
                    return_exceptions=True,
                )
                for (cid, name, _a), res in zip(calls, results):
                    if isinstance(res, BaseException):
                        log.warning("tool %s raised: %s", name, res)
                        content = json.dumps({"error": str(res)}, ensure_ascii=False)
                    else:
                        content = _compact(str(res))
                    messages.append(
                        {"role": "tool", "tool_call_id": cid, "content": content}
                    )
            else:
                final_text = (
                    "（已达工具调用步数上限，已用现有结果作答；"
                    "如仍需更多步骤，请缩小范围或回复 ::继续）"
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
