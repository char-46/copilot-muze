"""Agent tool schema definitions and execution against the archive.

Tools are executed with the requesting ControlContext bound in, so
`send_message` goes through the same confirmation flow as commands.
"""

from __future__ import annotations

import json
import logging

from muze.chatlog import epoch_iso
from muze.core.service import ControlContext, ControlService
from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.provider.milky.models import out_reply, out_text
from muze.store.repo import Repository
from muze.version import get_version

log = logging.getLogger("muze.ai")

SPEC = [
    {
        "type": "function",
        "function": {
            "name": "list_conversations",
            "description": "列出会话（好友私聊与群），按最近活跃排序",
            "parameters": {
                "type": "object",
                "properties": {
                    "active_within_hours": {
                        "type": "integer",
                        "description": "只看最近 N 小时内活跃的会话，默认 72",
                    },
                    "limit": {"type": "integer", "description": "默认 20，最大 100"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "按关键词搜索已存档的消息全文",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "limit": {"type": "integer", "description": "默认 20，最大 50"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_messages",
            "description": "读取某会话最近的消息（含文本内容与媒体状态）",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "limit": {"type": "integer", "description": "默认 30，最大 100"},
                },
                "required": ["scene", "peer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_message_detail",
            "description": "查看单条消息的完整内容（所有消息段）",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "message_seq": {"type": "integer"},
                },
                "required": ["scene", "peer_id", "message_seq"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_image",
            "description": "用视觉模型识别某条消息中的图片内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "message_seq": {"type": "integer"},
                },
                "required": ["scene", "peer_id", "message_seq"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forwarded_messages",
            "description": "展开合并转发消息的内容（若协议端支持）",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "message_seq": {"type": "integer"},
                },
                "required": ["scene", "peer_id", "message_seq"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "以被控号的名义向某会话发送文本消息。受确认机制约束："
                "通常返回一个待确认编号，需要控制者 /confirm 后才真正发出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "text": {"type": "string"},
                    "reply_seq": {
                        "type": "integer",
                        "description": "可选：引用回复某条消息的 seq",
                    },
                },
                "required": ["scene", "peer_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_conversation_read",
            "description": "把某会话标记为已读（若协议端支持）",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                },
                "required": ["scene", "peer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_status",
            "description": "查看系统状态（版本、消息量、补拉状态、能力）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class ToolRunner:
    def __init__(
        self,
        service: ControlService,
        repo: Repository,
        adapter: ProtocolAdapter,
        llm,
        ctx: ControlContext,
    ) -> None:
        self.service = service
        self.repo = repo
        self.adapter = adapter
        self.llm = llm
        self.ctx = ctx

    async def run(self, name: str, args: dict) -> str:
        try:
            handler = getattr(self, f"t_{name}")
            result = await handler(args)
            return json.dumps(result, ensure_ascii=False, default=str)
        except ProtocolError as e:
            return json.dumps({"error": f"协议端错误: {e}"}, ensure_ascii=False)
        except Exception as e:
            log.warning("tool %s failed: %s", name, e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ----------------------------------------------------------- helpers
    def _row_brief(self, r: dict) -> dict:
        return {
            "seq": r["message_seq"],
            "sender": r["sender_id"],
            "time": epoch_iso(r["time"]),
            "text": (r["text_content"] or "")[:200],
            "recalled": bool(r["recalled"]),
        }

    # ------------------------------------------------------------- tools
    async def t_list_conversations(self, args: dict) -> dict:
        rows = await self.repo.list_conversations(
            active_within_h=args.get("active_within_hours", 72),
            limit=min(args.get("limit", 20), 100),
        )
        convs = []
        for r in rows:
            name = r["name"]
            if not name:
                try:
                    if r["scene"] == "group":
                        name = await self.adapter.ensure_group_name(r["peer_id"])
                    else:
                        name = await self.adapter.ensure_friend_name(r["peer_id"])
                except Exception:
                    name = ""
            convs.append(
                {
                    "scene": r["scene"],
                    "peer_id": r["peer_id"],
                    "name": name,
                    "messages": r["message_count"],
                    "last_active": epoch_iso(r["last_active_at"]),
                }
            )
        return {"conversations": convs}

    async def t_search_messages(self, args: dict) -> dict:
        rows = await self.repo.search_messages(
            args["query"],
            scene=args.get("scene"),
            peer_id=args.get("peer_id"),
            limit=min(args.get("limit", 20), 50),
        )
        return {
            "matches": [
                {**self._row_brief(r), "scene": r["scene"], "peer_id": r["peer_id"]}
                for r in rows
            ]
        }

    async def t_get_recent_messages(self, args: dict) -> dict:
        rows = await self.repo.get_messages(
            args["scene"], args["peer_id"], limit=min(args.get("limit", 30), 100)
        )
        rows = list(reversed(rows))
        out = []
        for r in rows:
            item = {**self._row_brief(r)}
            medias = await self.repo.media_for_message(r["scene"], r["peer_id"], r["message_seq"])
            if medias:
                item["media"] = [
                    {
                        "kind": m["kind"],
                        "status": m["status"],
                        "file": m["file_path"],
                        "name": m["file_name"],
                        "summary": m["summary"],
                    }
                    for m in medias
                ]
            out.append(item)
        return {"messages": out}

    async def t_get_message_detail(self, args: dict) -> dict:
        r = await self.repo.get_message_row(args["scene"], args["peer_id"], args["message_seq"])
        if not r:
            return {"error": "消息不存在"}
        medias = await self.repo.media_for_message(args["scene"], args["peer_id"], args["message_seq"])
        return {
            **self._row_brief(r),
            "raw": json.loads(r["raw_json"]),
            "media": [
                {"kind": m["kind"], "status": m["status"], "file": m["file_path"]}
                for m in medias
            ],
        }

    async def t_describe_image(self, args: dict) -> dict:
        medias = await self.repo.media_for_message(
            args["scene"], args["peer_id"], args["message_seq"]
        )
        images = [m for m in medias if m["kind"] == "image" and m["status"] == "done"]
        if not images:
            summaries = [m.get("summary") or "" for m in medias if m["kind"] == "image"]
            return {"descriptions": [s for s in summaries if s] or "(无已下载图片)"}
        out = []
        for m in images[:3]:
            desc = await self.llm.vision_describe(m["file_path"], "详细描述这张图片的内容")
            out.append(desc)
        return {"descriptions": out}

    async def t_get_forwarded_messages(self, args: dict) -> dict:
        r = await self.repo.get_message_row(args["scene"], args["peer_id"], args["message_seq"])
        if not r:
            return {"error": "消息不存在"}
        raw = json.loads(r["raw_json"])
        forward_id = ""
        for seg in raw.get("segments", []):
            if seg.get("type") == "forward":
                forward_id = seg.get("data", {}).get("forward_id", "")
        if not forward_id:
            return {"error": "该消息不是合并转发"}
        msgs = await self.adapter.get_forwarded_messages(forward_id)
        from muze.chatlog import render_segment

        return {
            "messages": [
                {
                    "sender": m.sender_name or str(m.sender_id),
                    "time": epoch_iso(m.time),
                    "content": " ".join(render_segment(s) for s in m.segments),
                }
                for m in msgs
            ]
        }

    async def t_send_message(self, args: dict) -> dict:
        text = args["text"]
        segments = [out_text(text)]
        if args.get("reply_seq"):
            segments.insert(0, out_reply(int(args["reply_seq"])))
        outcome = await self.service.request_send(
            args["scene"], args["peer_id"], segments, text, self.ctx, "agent"
        )
        return {"result": outcome.text, "is_error": outcome.is_error}

    async def t_mark_conversation_read(self, args: dict) -> dict:
        rows = await self.repo.get_messages(args["scene"], args["peer_id"], limit=1)
        if not rows:
            return {"result": "会话无消息"}
        try:
            await self.adapter.mark_read(args["scene"], args["peer_id"], rows[0]["message_seq"])
            return {"result": "已标记已读"}
        except ProtocolError as e:
            return {"result": f"失败: {e}"}

    async def t_system_status(self, args: dict) -> dict:
        stats = await self.repo.stats()
        bf = self.service.runtime.backfiller.status() if self.service.runtime.backfiller else {}
        return {
            "version": get_version(),
            "stats": stats,
            "backfill": bf,
            "capabilities": self.adapter.caps.summary_lines(),
        }
