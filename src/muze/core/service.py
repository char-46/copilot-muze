"""ControlService: one command/confirmation core shared by QQ, WebUI and CLI.

Frontends only differ in how they obtain input and deliver output; every
command, the agent, and the send-confirmation flow live here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from dataclasses import dataclass, field

from muze.chatlog import ChatLog, conv_id, render_segment, size_fmt
from muze.config import MuzeConfig, is_control_conversation
from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.provider.milky.models import out_image, out_reply, out_text
from muze.store.media import to_file_uri
from muze.store.repo import Repository
from muze.version import epoch_iso, get_version, uptime_since

log = logging.getLogger("muze.core")


@dataclass
class ControlContext:
    source: str  # qq | web | cli
    chat_key: str  # stable per conversation, used for agent history
    scene: str  # control conversation scene (qq) or "" (web/cli)
    peer_id: int
    user_label: str = ""
    auto_confirm: bool = False  # CLI/local console sends immediately

    @property
    def qq_reachable(self) -> bool:
        return bool(self.scene)


@dataclass
class CommandOutcome:
    text: str = ""
    media_paths: list[str] = field(default_factory=list)
    is_error: bool = False


class ControlService:
    def __init__(
        self,
        cfg: MuzeConfig,
        adapter: ProtocolAdapter,
        repo: Repository,
        chatlog: ChatLog,
        runtime: "Runtime",
    ) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.repo = repo
        self.chatlog = chatlog
        self.runtime = runtime
        self.agent = None  # injected after AI layer construction

    # ------------------------------------------------------------- helpers
    def parse_target(self, arg: str) -> tuple[str, int]:
        if "/" in arg:
            scene, _, peer = arg.partition("/")
        else:
            scene, peer = "friend", arg
        peer_i = int(peer)
        if scene not in ("friend", "group"):
            raise ValueError(f"unknown scene: {scene}")
        return scene, peer_i

    @staticmethod
    def _target_label(adapter: ProtocolAdapter, scene: str, peer: int) -> str:
        if scene == "group":
            name = getattr(adapter, "cached_group_name", lambda _p: "")(peer)
            return f"{conv_id(scene, peer)} {name}".strip()
        name = getattr(adapter, "cached_friend_name", lambda _p: "")(peer)
        return f"{conv_id(scene, peer)} {name}".strip()

    # -------------------------------------------------------------- routes
    async def execute(self, text: str, ctx: ControlContext) -> CommandOutcome:
        text = (text or "").strip()
        if not text:
            return CommandOutcome()
        self.chatlog.ctrl(ctx.source, ctx.user_label or ctx.chat_key, text)

        if text.startswith("/"):
            try:
                parts = shlex.split(text)
            except ValueError:
                parts = text.split()
            cmd, args = parts[0][1:].lower(), parts[1:]
            handler = getattr(self, f"cmd_{cmd}", None)
            if handler is None:
                return CommandOutcome(f"未知命令 /{cmd}，试试 /help", is_error=True)
            try:
                return await handler(args, ctx)
            except (ValueError, IndexError) as e:
                return CommandOutcome(f"参数错误: {e}", is_error=True)
            except ProtocolError as e:
                return CommandOutcome(f"协议端错误: {e}", is_error=True)

        # natural language -> agent
        if self.agent is None:
            return CommandOutcome(
                "未配置 LLM（llm.chat.base_url/api_key/model），无法处理自然语言；命令仍可用，/help 查看",
                is_error=True,
            )
        return await self.agent.converse(text, ctx)

    # ------------------------------------------------------------- sending
    async def deliver(self, ctx: ControlContext, outcome: CommandOutcome) -> None:
        """Send an outcome back into the QQ control conversation (if any)."""
        if not ctx.qq_reachable or not (outcome.text or outcome.media_paths):
            return
        segments: list[dict] = []
        if outcome.text:
            segments.append(out_text(outcome.text[:3000]))
        for p in outcome.media_paths[:9]:
            segments.append(out_image(to_file_uri(p)))
        try:
            if ctx.scene == "group":
                await self.adapter.send_group_message(ctx.peer_id, segments)
            else:
                await self.adapter.send_private_message(ctx.peer_id, segments)
            self.chatlog.ctrl_reply(ctx.source, ctx.user_label or ctx.chat_key, outcome.text)
        except ProtocolError as e:
            log.warning("deliver to %s failed: %s", ctx.chat_key, e)

    async def send_as_bot(
        self, scene: str, peer_id: int, segments: list[dict], preview: str
    ) -> CommandOutcome:
        """Actually send a message as the controlled account; logs it."""
        if scene == "group":
            res = await self.adapter.send_group_message(peer_id, segments)
        else:
            res = await self.adapter.send_private_message(peer_id, segments)
        self.chatlog.sent(scene, peer_id, res.message_seq, preview)
        return CommandOutcome(f"已发送到 {self._target_label(self.adapter, scene, peer_id)} (seq={res.message_seq})")

    async def request_send(
        self,
        scene: str,
        peer_id: int,
        segments: list[dict],
        preview: str,
        ctx: ControlContext,
        created_by: str,
    ) -> CommandOutcome:
        """Send now, or create a pending confirmation (per config/context)."""
        if ctx.auto_confirm or not self.cfg.control.confirm_all_sends:
            return await self.send_as_bot(scene, peer_id, segments, preview)
        action_id = await self.repo.create_action(
            "send",
            scene,
            peer_id,
            {"segments": segments, "preview": preview[:500]},
            ctx.scene,
            ctx.peer_id,
            created_by,
        )
        self.chatlog.confirm(action_id, f"pending send -> {conv_id(scene, peer_id)}: {preview}")
        return CommandOutcome(
            f"待确认 #{action_id}: 将以被控号向 {self._target_label(self.adapter, scene, peer_id)} 发送:\n"
            f"-----\n{preview[:800]}\n-----\n"
            f"回复 /confirm {action_id} 发送，/cancel {action_id} 取消"
        )

    # ------------------------------------------------------------- commands
    async def cmd_help(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        return CommandOutcome(
            "命令列表:\n"
            "/status - 运行状态、版本与能力\n"
            "/version - 版本与构建信息\n"
            "/sync [group/123|friend/456] - 立即漫游补拉\n"
            "/pending - 待确认的代发\n"
            "/confirm [id] - 确认代发\n"
            "/cancel [id] - 取消代发\n"
            "/unread - 最近活跃会话概览\n"
            "/search <关键词> [数量] - 搜索已存消息\n"
            "/msg <scene/peer> <seq> - 查看单条消息\n"
            "/media <scene/peer> <seq> - 提取该消息的媒体文件\n"
            "/summary <scene/peer> [条数] - LLM 总结会话\n"
            "/send <scene/peer> <内容> - 以被控号发消息\n"
            "/reply <scene/peer> [seq] <内容> - 以被控号回复\n"
            "/markread <scene/peer> [seq] - 标记已读\n"
            "其余文字将交给 AI 助手理解（需配置 llm）"
        )

    async def cmd_version(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        return CommandOutcome(get_version())

    async def cmd_status(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        stats = await self.repo.stats()
        caps = self.adapter.caps
        bf = self.runtime.backfiller.status() if self.runtime.backfiller else {}
        lines = [
            f"muze {get_version()}",
            f"self={self.runtime.self_id} uptime={uptime_since(self.runtime.started_at)}",
            f"backfill: running={bf.get('running')} last_new={bf.get('last_new_messages')} "
            f"last_run={epoch_iso(bf.get('last_run_at')) or '-'}",
            f"conversations={stats['conversations']} messages={stats['messages']} "
            f"(by scene: {stats.get('messages_by_scene')})",
            f"media done/failed/pending={stats['media']['done']}/{stats['media']['failed']}"
            f"/{stats['media']['pending']} size={size_fmt(stats['media']['bytes'])}",
            f"pending confirmations={stats['pending_actions']} agent={'on' if self.agent else 'off'}",
            "capabilities:",
            *[f"  {line}" for line in caps.summary_lines()],
        ]
        return CommandOutcome("\n".join(lines))

    async def cmd_sync(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if not self.runtime.backfiller:
            return CommandOutcome("backfiller 不可用", is_error=True)
        if args:
            scene, peer = self.parse_target(args[0])
            n = await self.runtime.backfiller.run_once([(scene, peer)])
            return CommandOutcome(f"sync {conv_id(scene, peer)} 完成，新增 {n} 条")
        asyncio.create_task(self.runtime.backfiller.run_once())
        return CommandOutcome("全量补拉已启动（后台执行），稍后用 /status 查看结果")

    async def cmd_pending(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        rows = await self.repo.list_pending_actions(
            ctx.scene or None, ctx.peer_id or None
        )
        if not rows:
            return CommandOutcome("没有待确认的操作")
        lines = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            lines.append(
                f"#{r['id']} [{r['created_by']}] -> {conv_id(r['scene'], r['peer_id'])}: "
                f"{payload.get('preview', '')[:80]}"
            )
        return CommandOutcome("\n".join(lines) + "\n/confirm id 或 /cancel id")

    async def confirm_action(self, action_id: int, ctx: ControlContext) -> CommandOutcome:
        """Confirm a pending send from any privileged front (Web/CLI)."""
        action = await self.repo.get_action(action_id)
        if not action or action["status"] != "pending":
            return CommandOutcome(f"#{action_id} 不存在或已失效", is_error=True)
        await self.repo.set_action_status(action_id, "executed")
        payload = json.loads(action["payload_json"])
        outcome = await self.send_as_bot(
            action["scene"], action["peer_id"], payload.get("segments", []),
            payload.get("preview", ""),
        )
        self.chatlog.confirm(action_id, f"confirmed by {ctx.user_label or ctx.chat_key}")
        return outcome

    async def cancel_action(self, action_id: int, ctx: ControlContext) -> CommandOutcome:
        action = await self.repo.get_action(action_id)
        if not action or action["status"] != "pending":
            return CommandOutcome(f"#{action_id} 不存在或已失效", is_error=True)
        await self.repo.set_action_status(action_id, "cancelled")
        self.chatlog.confirm(action_id, f"cancelled by {ctx.user_label or ctx.chat_key}")
        return CommandOutcome(f"已取消 #{action_id}")

    async def cmd_confirm(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        rows = await self.repo.list_pending_actions(ctx.scene or None, ctx.peer_id or None)
        if not rows:
            return CommandOutcome("没有待确认的操作", is_error=True)
        action = rows[0]
        if args:
            wanted = int(args[0])
            action = next((r for r in rows if r["id"] == wanted), None)
            if action is None:
                return CommandOutcome(f"#{wanted} 不存在或不属于当前控制会话", is_error=True)
        ok = await self.repo.set_action_status(action["id"], "executed")
        if not ok:
            return CommandOutcome(f"#{action['id']} 已失效", is_error=True)
        payload = json.loads(action["payload_json"])
        outcome = await self.send_as_bot(
            action["scene"], action["peer_id"], payload.get("segments", []),
            payload.get("preview", ""),
        )
        self.chatlog.confirm(action["id"], f"confirmed by {ctx.user_label or ctx.chat_key}")
        return outcome

    async def cmd_cancel(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        rows = await self.repo.list_pending_actions(ctx.scene or None, ctx.peer_id or None)
        if not rows:
            return CommandOutcome("没有待确认的操作", is_error=True)
        if args:
            wanted = int(args[0])
            rows = [r for r in rows if r["id"] == wanted]
            if not rows:
                return CommandOutcome(f"#{wanted} 不存在", is_error=True)
        for r in rows:
            await self.repo.set_action_status(r["id"], "cancelled")
            self.chatlog.confirm(r["id"], "cancelled")
        return CommandOutcome(f"已取消 {len(rows)} 项")

    async def cmd_unread(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        rows = await self.repo.list_conversations(active_within_h=24, limit=20)
        if not rows:
            return CommandOutcome("近 24h 没有会话活动")
        lines = []
        for r in rows:
            msgs = await self.repo.get_messages(r["scene"], r["peer_id"], limit=1)
            preview = (msgs[0]["text_content"][:40] if msgs else "") or "(无文本)"
            label = self._target_label(self.adapter, r["scene"], r["peer_id"])
            lines.append(f"{label} [{r['message_count']}条] {epoch_iso(r['last_active_at'])}: {preview}")
        return CommandOutcome("近24h活跃会话:\n" + "\n".join(lines))

    async def cmd_search(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if not args:
            return CommandOutcome("用法: /search <关键词> [数量]", is_error=True)
        query = args[0]
        limit = int(args[1]) if len(args) > 1 else 20
        rows = await self.repo.search_messages(query, limit=limit)
        if not rows:
            return CommandOutcome(f"没有匹配 “{query}” 的消息")
        lines = []
        for r in rows[:limit]:
            label = self._target_label(self.adapter, r["scene"], r["peer_id"])
            lines.append(
                f"{label} seq={r['message_seq']} {epoch_iso(r['time'])} "
                f"[{r['sender_id']}] {r['text_content'][:60]}"
            )
        return CommandOutcome(f"“{query}” 命中 {len(rows)} 条:\n" + "\n".join(lines[:limit]))

    async def cmd_msg(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if len(args) < 2:
            return CommandOutcome("用法: /msg <scene/peer> <seq>", is_error=True)
        scene, peer = self.parse_target(args[0])
        seq = int(args[1])
        row = await self.repo.get_message_row(scene, peer, seq)
        if not row:
            return CommandOutcome(f"{conv_id(scene, peer)} seq={seq} 不存在", is_error=True)
        raw = json.loads(row["raw_json"])
        medias = await self.repo.media_for_message(scene, peer, seq)
        media_desc = ", ".join(
            f"{m['kind']}:{m['status']}{'=' + m['file_path'] if m['file_path'] else ''}"
            for m in medias
        ) or "无媒体"
        from muze.provider.milky.models import IncomingMessage

        msg = IncomingMessage.from_raw(raw)
        segs = " ".join(render_segment(s) for s in msg.segments)
        return CommandOutcome(
            f"{self._target_label(self.adapter, scene, peer)} seq={seq}\n"
            f"sender={row['sender_id']} time={epoch_iso(row['time'])} "
            f"recalled={bool(row['recalled'])} source={row['source']}\n"
            f"内容: {segs}\n媒体: {media_desc}"
        )

    async def cmd_media(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if len(args) < 2:
            return CommandOutcome("用法: /media <scene/peer> <seq>", is_error=True)
        scene, peer = self.parse_target(args[0])
        seq = int(args[1])
        rows = await self.repo.media_for_message(scene, peer, seq)
        done = [r for r in rows if r["status"] == "done" and r["file_path"]]
        if not done:
            return CommandOutcome(f"seq={seq} 没有已下载成功的媒体（共 {len(rows)} 项记录）", is_error=True)
        paths = [r["file_path"] for r in done]
        desc = "\n".join(f"{r['kind']} {r['file_name'] or ''} {size_fmt(r['file_size'])} -> {r['file_path']}" for r in done)
        return CommandOutcome(f"seq={seq} 媒体 {len(done)} 项:\n{desc}", media_paths=paths)

    async def cmd_summary(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if not args:
            return CommandOutcome("用法: /summary <scene/peer> [条数]", is_error=True)
        if self.agent is None:
            return CommandOutcome("未配置 LLM，无法总结", is_error=True)
        scene, peer = self.parse_target(args[0])
        limit = int(args[1]) if len(args) > 1 else 40
        rows = await self.repo.get_messages(scene, peer, limit=limit)
        if not rows:
            return CommandOutcome(f"{conv_id(scene, peer)} 没有已存消息", is_error=True)
        rows = list(reversed(rows))  # oldest first for the model
        return await self.agent.summarize(scene, peer, rows, ctx)

    async def cmd_send(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if len(args) < 2:
            return CommandOutcome("用法: /send <scene/peer> <内容>", is_error=True)
        scene, peer = self.parse_target(args[0])
        body = " ".join(args[1:])
        return await self.request_send(scene, peer, [out_text(body)], body, ctx, "command")

    async def cmd_reply(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if len(args) < 2:
            return CommandOutcome("用法: /reply <scene/peer> [seq] <内容>", is_error=True)
        scene, peer = self.parse_target(args[0])
        rest = args[1:]
        reply_seq: int | None = None
        if rest and rest[0].isdigit() and len(rest) >= 2:
            reply_seq = int(rest[0])
            rest = rest[1:]
        if not rest:
            return CommandOutcome("缺少回复内容", is_error=True)
        body = " ".join(rest)
        segments = [out_reply(reply_seq), out_text(body)] if reply_seq else [out_text(body)]
        return await self.request_send(scene, peer, segments, body, ctx, "command")

    async def cmd_markread(self, args: list[str], ctx: ControlContext) -> CommandOutcome:
        if not args:
            return CommandOutcome("用法: /markread <scene/peer> [seq]", is_error=True)
        scene, peer = self.parse_target(args[0])
        seq = int(args[1]) if len(args) > 1 else None
        if seq is None:
            rows = await self.repo.get_messages(scene, peer, limit=1)
            if not rows:
                return CommandOutcome("会话无消息", is_error=True)
            seq = rows[0]["message_seq"]
        try:
            await self.adapter.mark_read(scene, peer, seq)
            return CommandOutcome(f"已标记 {conv_id(scene, peer)} 读到 seq={seq}")
        except ProtocolError as e:
            return CommandOutcome(f"标记失败: {e}", is_error=True)

    # ------------------------------------------------------------ qq route
    def is_controller(self, scene: str, peer_id: int, sender_id: int) -> bool:
        """Is this sender allowed in this conversation?"""
        cfg = self.cfg.control
        if scene == "friend":
            return cfg.private_control and sender_id in cfg.admin_users
        if scene == "group":
            return bool(cfg.control_group) and peer_id == cfg.control_group and sender_id in cfg.admin_users
        return False

    def qq_should_trigger(self, scene: str, peer_id: int, sender_id: int, mention_self: bool, text: str) -> bool:
        if not self.is_controller(scene, peer_id, sender_id):
            return False
        if scene == "friend":
            return True
        if self.cfg.control.group_trigger == "all":
            return True
        return mention_self or text.startswith("/")


class Runtime:
    """Small bag of app state shared with the control layer."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.self_id = 0
        self.backfiller = None  # Backfiller, injected by main
        self.log_path = ""
