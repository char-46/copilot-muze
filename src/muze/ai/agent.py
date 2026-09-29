"""Conversational agent over the message archive (tool-calling loop)."""

from __future__ import annotations

import json
import logging

from muze.ai.llm import LLMClient
from muze.ai.tools import SPEC, ToolRunner
from muze.chatlog import ChatLog, epoch_iso
from muze.core.service import CommandOutcome, ControlContext, ControlService
from muze.store.repo import Repository

log = logging.getLogger("muze.ai")

MAX_HISTORY = 30  # per control conversation

SYSTEM_PROMPT = """你是 muze，一个运行在用户 QQ 账号（被控号）上的个人 copilot 助手。
控制者通过控制会话/界面与你对话，你可以借助工具查询和管理被控号的消息档案。

要点：
- 消息档案包含好友私聊与群聊的历史（含离线期间漫游补拉的和用户自己发出的消息）。
- 查询优先用 cli 工具（和 /help 的命令完全一致），如 cli({"command":"search 关键词"})、
  cli({"command":"summary group/123 60"})；不确定命令就 cli({"command":"help"})。
- 需要逐条看消息内容时才用 get_recent_messages。
- 图片内容用 describe_image 识别；本地文档用 read_document；网页/在线文档用 fetch_url。
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

    def _system_prompt(self) -> str:
        skills = getattr(self.service.runtime, "skills", None)
        if skills is None:
            return SYSTEM_PROMPT
        lines = skills.index_lines()
        if not lines:
            return SYSTEM_PROMPT
        return SYSTEM_PROMPT + "\n\n已安装技能（相关时先 read_skill 读全文再操作）:\n" + "\n".join(lines)

    # ------------------------------------------------------------ converse
    async def converse(self, text: str, ctx: ControlContext) -> CommandOutcome:
        if not self.llm.configured:
            return CommandOutcome("LLM 未配置（llm.chat.*）", is_error=True)
        history = self._history(ctx.chat_key)
        history.append({"role": "user", "content": text})
        self.chatlog.agent(ctx.source, ctx.user_label or ctx.chat_key, "in", text)

        runner = ToolRunner(self.service, self.repo, self.service.adapter, self.llm, ctx)
        messages: list[dict] = [
            {"role": "system", "content": self._system_prompt()},
            *history[:-1][-MAX_HISTORY:],
            {"role": "user", "content": text},
        ]

        final_text = ""
        try:
            for _ in range(self.max_steps):
                msg = await self.llm.chat(messages, tools=SPEC)
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    final_text = (msg.get("content") or "").strip()
                    break
                messages.append(msg)
                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    self.chatlog.agent(
                        ctx.source, ctx.user_label or ctx.chat_key, "tool",
                        f"{name}({args})",
                    )
                    result = await runner.run(name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": result,
                        }
                    )
            else:
                final_text = "（达到工具调用步数上限，已中止；请缩小问题范围重试）"
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
