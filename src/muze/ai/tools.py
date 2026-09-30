"""Agent tool schema definitions and execution against the archive.

Design (why not MCP): one generic `cli` tool drives every slash command —
tiny schema footprint, discoverable via `cli('help')`, no giant tool
definitions eating context. A few focused tools remain where a plain
command reply is a poor fit (image description, skills, sends).
"""

from __future__ import annotations

import json
import logging

from muze.chatlog import epoch_iso
from muze.core.service import ControlContext, ControlService
from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.provider.milky.models import out_reply, out_text
from muze.store.repo import Repository

log = logging.getLogger("muze.ai")

# Permission model: reads are unconditional anywhere; writes run freely in
# the controller's session but are routed to the controller for approval in
# any other conversation (see ControlService.requires_approval).
# send_message routes itself via ControlService.request_send.
WRITE_TOOLS = {"schedule_task", "remember"}

SPEC = [
    {
        "type": "function",
        "function": {
            "name": "cli",
            "description": (
                "执行 muze 内部命令（与 /help 列出的一致，不要带斜杠）。"
                "例: cli({\"command\": \"search 报价 20\"})、cli({\"command\": \"msg group/123 456\"})。"
                "查询/搜索/读取消息/展开转发/标记已读/定时任务等都走这个工具；"
                "归档规模与时间范围（最早/最新消息日期）用 cli({\"command\": \"status\"})；"
                "按日期查消息用 day <YYYY-MM-DD|today|yesterday> [会话] 或 between <起> <止> [会话]；"
                "会话活跃排行 top [天数]；单会话时间范围 range <会话>；"
                "不确定用法时先 cli({\"command\": \"help\"})。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "命令行（不含斜杠），如 'unread'、'summary friend/123 60'",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_messages",
            "description": "读取某会话最近的消息（含文本与媒体状态），需要逐条看内容时用",
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
            "name": "search_archive",
            "description": "全文搜索归档消息，返回结构化 JSON（推荐优先于 cli search）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "limit": {"type": "integer", "description": "默认 20，最大 100"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activity",
            "description": "活动统计（结构化）：某天 day / 区间 range / 会话排行 top / 发言人 senders",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["day", "range", "top", "senders"]},
                    "date": {"type": "string", "description": "day 模式：YYYY-MM-DD/today/yesterday"},
                    "start": {"type": "string", "description": "range 起始日"},
                    "end": {"type": "string", "description": "range 结束日"},
                    "days": {"type": "integer", "description": "top 模式近 N 天，0=全史"},
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "directory",
            "description": "目录查询：conversations 会话列表 / friends 好友 / groups 群 / whois 查名字",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["conversations", "friends", "groups", "whois"]},
                    "uin": {"type": "integer"},
                    "group_id": {"type": "integer"},
                    "active_h": {"type": "number", "description": "conversations: 仅最近 N 小时活跃"},
                    "limit": {"type": "integer"},
                },
                "required": ["kind"],
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
            "name": "send_message",
            "description": (
                "以被控号的名义向某会话发送文本。受确认机制约束：通常返回待确认编号，"
                "需控制者 /confirm 后才真正发出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "text": {"type": "string"},
                    "reply_seq": {"type": "integer", "description": "可选：引用回复的消息 seq"},
                },
                "required": ["scene", "peer_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": (
                "创建计划任务：到点后系统会把 prompt 原样交给 AI 执行，并把结果发送到控制者的"
                "控制会话。用户说「两小时后提醒我X」「每两小时发一次总结」「明早9点…」时用这个工具。"
                "你必须把用户的自然语言时间换算成参数：相对时间→delay_sec；"
                "绝对时间(如'明天早上9点')→at_iso(本地时区ISO)；周期→interval_sec。"
                "prompt 写成给未来 AI 的完整指令，如「总结 friend/123 最近 50 条并给出待办」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "到期执行的指令（给 AI 的自然语言或 /命令）"},
                    "delay_sec": {"type": "integer", "description": "多少秒后执行（一次性，与 at_iso/interval_sec 互斥）"},
                    "at_iso": {"type": "string", "description": "绝对执行时间，本地时区 ISO 格式"},
                    "interval_sec": {"type": "integer", "description": "周期执行的间隔秒数"},
                    "note": {"type": "string", "description": "任务备注（用户原话）"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "读取本地文档全文（QQ 收到的文件已自动落盘，配合 cli 的 msg/media 命令找到路径）。"
                "支持 docx/txt/md/html/csv/json；doc/pdf 暂不支持会明确报错。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "本地文件绝对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "抓取 URL 的文本内容（网页/在线文档）。遇到金山文档/腾讯文档等需要 JS 或登录的页面"
                "会返回具体可行的替代建议；QQ 小程序卡片先读 doc-sources 技能找直链。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_dream",
            "description": (
                "回顾「做梦报告」（层级总结：日报/周报/月报）。查历史聊天内容时优先用这个而非翻原始消息："
                "问'昨天/某天'用日期 YYYY-MM-DD；问'上周/某周'用周报 YYYY-Www（如 2026-W40，一份抵七天）；"
                "问'上个月/某月趋势'用月报 YYYY-MM。缺省=最新日报。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "YYYY-MM-DD | YYYY-Www | YYYY-MM；缺省=最新日报",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "把一条长期有用的事实写入长期记忆（身份/关系/偏好/约定）。"
                "用户主动告诉你关于他自己或联系人stable的信息时、或对话中确认了重要事实时调用。"
                "临时性内容不要记。subject 用 'owner'（被控号主人）、'<QQ号>' 或 'group/<群号>'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content": {"type": "string", "description": "一句话中文事实"},
                    "kind": {"type": "string", "enum": ["identity", "preference", "relation", "fact"]},
                },
                "required": ["subject", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": (
                "阅读一个已安装技能的完整说明（先 cli({\"command\":\"skills\"}) 看列表）。"
                "当任务与某技能描述相关时，务必先读该技能再操作。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
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
        if name in WRITE_TOOLS and self.service.requires_approval(self.ctx):
            return await self._gate(name, args)
        try:
            handler = getattr(self, f"t_{name}")
            result = await handler(args)
            payload = json.dumps(result, ensure_ascii=False, default=str)
        except ProtocolError as e:
            payload = json.dumps({"error": f"协议端错误: {e}"}, ensure_ascii=False)
        except Exception as e:
            log.warning("tool %s failed: %s", name, e)
            payload = json.dumps({"error": str(e)}, ensure_ascii=False)
        log.info("LLM tool  | %s(%s) -> %s", name, _args_brief(args), payload[:4000])
        return payload

    def _describe(self, name: str, args: dict) -> str:
        if name == "send_message":
            return f"发送消息到 {args.get('scene')}/{args.get('peer_id')}: {str(args.get('text', ''))[:120]}"
        if name == "schedule_task":
            return f"创建计划任务: {str(args.get('prompt', ''))[:120]}"
        if name == "remember":
            return f"写入长期记忆 [{args.get('subject', 'owner')}]: {str(args.get('content', ''))[:120]}"
        if name == "cli":
            return f"执行命令: /{args.get('command', '')}"
        return f"{name}({_args_brief(args)})"

    async def _gate(self, name: str, args: dict) -> str:
        """Outside the controller's session: submit the write for approval."""
        desc = self._describe(name, args)
        outcome = await self.service.request_operation(name, args, self.ctx, desc)
        payload = json.dumps(
            {
                "needs_approval": True,
                "is_error": outcome.is_error,
                "message": outcome.text,
                "note": "当前会话无写权限，已转交控制者审批；批准后自动执行",
            },
            ensure_ascii=False,
        )
        log.info("LLM tool  | %s(%s) GATED -> %s", name, _args_brief(args), payload[:400])
        return payload

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
    async def t_cli(self, args: dict) -> dict:
        command = str(args.get("command", "")).strip().lstrip("/")
        if not command:
            return {"error": "empty command", "hint": "cli({\"command\":\"help\"})"}
        from muze.core.service import COMMANDS

        spec = COMMANDS.get(command.split()[0])
        if spec is not None and spec.write and self.service.requires_approval(self.ctx):
            desc = self._describe("cli", {"command": command})
            outcome = await self.service.request_operation(
                "cli", {"command": command}, self.ctx, desc
            )
            return {
                "needs_approval": True,
                "output": outcome.text,
                "note": "该命令会改动外部状态，当前会话无写权限，已提交控制者审批",
            }
        outcome = await self.service.run_command(command, self.ctx)
        if outcome.is_error:
            return {"is_error": True, "output": outcome.text}
        return {
            "output": outcome.text,
            "media": outcome.media_paths,
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

    async def t_search_archive(self, args: dict) -> dict:
        q = str(args.get("query", "")).strip()
        if not q:
            return {"error": "query 不能为空"}
        rows = await self.repo.search_messages(
            q,
            scene=args.get("scene") or None,
            peer_id=args.get("peer_id"),
            limit=min(int(args.get("limit", 20)), 100),
        )
        return {"count": len(rows), "matches": [self._row_brief(r) for r in rows]}

    async def t_activity(self, args: dict) -> dict:
        import time as _t

        from muze.core.service import ControlService

        mode = str(args.get("mode", "")).strip()
        scene = args.get("scene") or None
        peer = args.get("peer_id")
        limit = int(args.get("limit", 40))
        if mode == "day":
            start, end = ControlService.parse_day(str(args.get("date", "today")))
            convs = await self.repo.day_counts(start, end, limit=limit)
            out = {
                "date": args.get("date", "today"),
                "total": sum(c["n"] for c in convs),
                "conversations": [
                    {"conv": f"{c['scene']}/{c['peer_id']}", "name": c.get("name", ""), "n": c["n"]}
                    for c in convs
                ],
            }
            if scene and peer is not None:
                msgs = await self.repo.messages_between(scene, peer, start, end, limit=300)
                out["messages"] = [self._row_brief(r) for r in msgs]
                out["senders"] = [
                    {"sender": r["sender_id"], "n": r["n"]}
                    for r in await self.repo.sender_counts(start, end, scene, peer, limit=20)
                ]
            return out
        if mode == "range":
            start, _ = ControlService.parse_day(str(args.get("start", "")))
            _, end = ControlService.parse_day(str(args.get("end", "")))
            convs = await self.repo.day_counts(start, end, limit=limit)
            return {
                "range": [args.get("start"), args.get("end")],
                "total": sum(c["n"] for c in convs),
                "conversations": [
                    {"conv": f"{c['scene']}/{c['peer_id']}", "n": c["n"]} for c in convs
                ],
            }
        if mode == "top":
            days = int(args.get("days", 0) or 0)
            since = int(_t.time() - days * 86400) if days else 0
            rows = await self.repo.top_conversations(since, limit=int(args.get("limit", 15)))
            return {
                "days": days,
                "conversations": [
                    {"conv": f"{c['scene']}/{c['peer_id']}", "name": c.get("name", ""), "n": c["n"]}
                    for c in rows
                ],
            }
        if mode == "senders":
            if not (scene and peer is not None):
                return {"error": "senders 需要 scene 和 peer_id"}
            days = int(args.get("days", 7) or 7)
            end = int(_t.time()) + 1
            rows = await self.repo.sender_counts(
                end - days * 86400, end, scene, peer, limit=int(args.get("limit", 20))
            )
            return {"senders": [{"sender": r["sender_id"], "n": r["n"]} for r in rows]}
        return {"error": f"未知 mode: {mode}"}

    async def t_directory(self, args: dict) -> dict:
        from muze.chatlog import epoch_iso as _iso

        kind = str(args.get("kind", "")).strip()
        limit = int(args.get("limit", 50))
        try:
            if kind == "conversations":
                active_h = args.get("active_h")
                rows = await self.repo.list_conversations(
                    active_within_h=float(active_h) if active_h else None, limit=limit
                )
                return {
                    "conversations": [
                        {
                            "conv": f"{r['scene']}/{r['peer_id']}",
                            "name": r.get("name", ""),
                            "messages": r.get("messages") or r.get("message_count"),
                            "last_active": _iso(r.get("last_active_at")),
                        }
                        for r in rows
                    ]
                }
            if kind == "friends":
                fl = await self.adapter.get_friend_list()
                return {
                    "friends": [
                        {
                            "uin": getattr(f, "user_id", 0),
                            "name": getattr(f, "user_name", "") or getattr(f, "nickname", ""),
                        }
                        for f in fl
                    ][:limit]
                }
            if kind == "groups":
                gl = await self.adapter.get_group_list()
                return {
                    "groups": [
                        {"group_id": getattr(g, "group_id", 0), "name": getattr(g, "group_name", "")}
                        for g in gl
                    ][:limit]
                }
            if kind == "whois":
                out: dict = {}
                uin = args.get("uin")
                gid = args.get("group_id")
                if uin:
                    try:
                        out["friend_name"] = await self.adapter.ensure_friend_name(int(uin))
                    except Exception:
                        out["friend_name"] = ""
                    if gid:
                        try:
                            out["member_name"] = await self.adapter.get_group_member_name(
                                int(gid), int(uin)
                            )
                        except Exception:
                            out["member_name"] = ""
                if gid:
                    try:
                        out["group_name"] = await self.adapter.ensure_group_name(int(gid))
                    except Exception:
                        out["group_name"] = ""
                return out
        except ProtocolError as e:
            return {"error": f"协议端错误: {e}"}
        return {"error": f"未知 kind: {kind}"}

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

    async def t_send_message(self, args: dict) -> dict:
        text = args["text"]
        segments = [out_text(text)]
        if args.get("reply_seq"):
            segments.insert(0, out_reply(int(args["reply_seq"])))
        outcome = await self.service.request_send(
            args["scene"], args["peer_id"], segments, text, self.ctx, "agent"
        )
        return {"result": outcome.text, "is_error": outcome.is_error}

    async def t_schedule_task(self, args: dict) -> dict:
        scheduler = self.service.runtime.scheduler
        if scheduler is None:
            return {"error": "scheduler 未装配"}
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return {"error": "prompt 不能为空"}
        kwargs: dict = {}
        if args.get("interval_sec"):
            kwargs["interval_sec"] = int(args["interval_sec"])
        elif args.get("at_iso"):
            from datetime import datetime

            try:
                dt = datetime.fromisoformat(str(args["at_iso"]))
                if dt.tzinfo is None:
                    dt = dt.astimezone()
                kwargs["at_ts"] = int(dt.timestamp())
            except ValueError as e:
                return {"error": f"at_iso 解析失败: {e}"}
        elif args.get("delay_sec") is not None:
            kwargs["delay_sec"] = int(args["delay_sec"])
        else:
            return {"error": "需要 delay_sec / at_iso / interval_sec 之一"}
        try:
            task = await scheduler.create(
                prompt=prompt, ctx=self.ctx, created_by="agent",
                note=str(args.get("note", ""))[:200],
            )
        except ValueError as e:
            return {"error": str(e)}
        from muze.chatlog import epoch_iso

        return {
            "created": True,
            "task_id": task["id"],
            "kind": task["kind"],
            "next_run": epoch_iso(task["next_run_at"]),
            "hint": "已创建；可用 cli({\"command\":\"tasks\"}) 查看，取消用 cli({\"command\":\"canceltask <id>\"})",
        }

    async def t_read_document(self, args: dict) -> dict:
        from muze.docs import read_document

        path = str(args.get("path", "")).strip()
        try:
            text = await __import__("asyncio").to_thread(read_document, path)
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}
        return {"path": path, "content": text}

    async def t_fetch_url(self, args: dict) -> dict:
        from muze.docs import fetch_url

        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return {"error": "仅支持 http(s) URL"}
        try:
            text = await fetch_url(url)
        except Exception as e:
            return {"error": str(e)}
        return {"url": url, "content": text}

    async def t_recall_dream(self, args: dict) -> dict:
        date = str(args.get("date", "")).strip()
        if date:
            row = await self.repo.get_digest(date)
        else:
            latest = await self.repo.latest_digest()
            row = await self.repo.get_digest(latest["date"]) if latest else None
        if not row:
            return {"error": "没有做梦报告", "hint": "cli({\"command\":\"dream list\"})"}
        return {"date": row["date"], "report": row["report"][:8000]}

    async def t_remember(self, args: dict) -> dict:
        content = str(args.get("content", "")).strip()
        if not content:
            return {"error": "content 不能为空"}
        subject = str(args.get("subject", "owner")).strip() or "owner"
        kind = str(args.get("kind", "fact")).strip()
        mid = await self.repo.add_memory(kind, subject, content, source="chat")
        log.info("MEM        | + [%s/%s] %s (via remember tool)", subject, kind, content)
        return {"saved": True, "id": mid}

    async def t_read_skill(self, args: dict) -> dict:
        skills = self.service.runtime.skills
        if skills is None:
            return {"error": "skills 未启用"}
        name = str(args.get("name", "")).strip()
        sk = skills.get(name)
        if sk is None:
            return {
                "error": f"技能 {name} 不存在",
                "available": [s.name for s in skills.list()],
            }
        return {"name": sk.name, "body": sk.body[:8000]}


def _args_brief(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False, default=str)
    return s[:300]
