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
WRITE_TOOLS = {"schedule_task", "remember", "memory_update", "memory_delete", "profile_update", "followup_add", "followup_close"}

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
            "description": (
                "目录查询：conversations 会话列表 / friends 好友 / groups 群 /"
                " members 群成员（单群全量或按 name/uin 过滤；多群 group_ids 联查："
                "带过滤=此人在哪些群，无过滤=共同成员交集）/ whois 查名字"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["conversations", "friends", "groups", "members", "whois"],
                    },
                    "uin": {"type": "integer", "description": "members/whois: 按QQ号精确匹配"},
                    "group_id": {"type": "integer"},
                    "group_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "members 多群联查：2~10 个群",
                    },
                    "name": {
                        "type": "string",
                        "description": "members 过滤：匹配群名片/昵称/QQ号子串（不区分大小写）",
                    },
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
            "description": (
                "用视觉模型识别某条消息中的图片内容。是否值得调用由你判断："
                "结合当前请求、长期记忆和会话背景，只对携带信息量的图片调用"
                "（如截图、照片、文档/图表、用户明确问到的图）；"
                "与任务无关的备份图、装饰图、聊天灌水图不要调用。"
                "purpose 用一句话写清识别目标，结果会缓存，重复问同一张图不会重复调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "message_seq": {"type": "integer"},
                    "purpose": {
                        "type": "string",
                        "description": "识别目标，如'看清楚发票金额'、'这张截图里的报错'",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "表情/贴图默认跳过；确实需要识别时置 true",
                    },
                },
                "required": ["scene", "peer_id", "message_seq"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_media",
            "description": "发送图片/表情/文件/视频：归档媒体(media_id 或 scene/peer/message_seq)或本地文件(path)。受权限约束",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "media_id": {"type": "integer", "description": "归档媒体 id（与 message_seq 二选一）"},
                    "message_seq": {"type": "integer", "description": "取该消息的第一件媒体"},
                    "path": {"type": "string", "description": "本地文件绝对路径（如生成的图表）"},
                    "caption": {"type": "string", "description": "随附文本，可选"},
                },
                "required": ["scene", "peer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transcribe_voice",
            "description": (
                "转写某条语音消息并返回文本与逐词时间戳（word, start, end 秒）。"
                "已转写的直接返回缓存并回填正文（[语音转写] 前缀）；force=true 强制重转写"
                "（换识别后端后用于重新理解旧语音）。需要精确到'第几秒说了什么'时也用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                    "message_seq": {"type": "integer"},
                    "force": {
                        "type": "boolean",
                        "description": "忽略已有转写结果，重新识别",
                    },
                },
                "required": ["scene", "peer_id", "message_seq"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ocr_image",
            "description": "识别某条消息图片中的文字（OCR）",
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
            "name": "make_chart",
            "description": "生成柱状/折线图 PNG（labels/values 等长），随后可用 send_media 发送",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "values": {"type": "array", "items": {"type": "number"}},
                    "kind": {"type": "string", "enum": ["bar", "line"], "description": "默认 bar"},
                },
                "required": ["title", "labels", "values"],
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
                "读取本地文档/压缩包（QQ 收到的文件已自动落盘，配合 cli 的 msg/media 命令找到路径）。"
                "文本: txt/md/json/csv/xml/html；文档: docx/pdf/xlsx/xls/pptx/rtf，"
                "旧版 doc/ppt 会嗅探真实格式尽力提取；压缩包: zip/7z/tar/gz（rar 需机器装有 7-Zip）。"
                "压缩包先不传 entry 看文件列表（含小文本预览），再 entry=<文件名> 读其中一项。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "本地文件绝对路径"},
                    "entry": {
                        "type": "string",
                        "description": "压缩包内的成员文件名（仅压缩包需要）",
                    },
                },
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
            "name": "web_search",
            "description": "联网搜索，返回 [{title,url,snippet}]（需在 config 配置 research.provider）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "n": {"type": "integer", "description": "结果数，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": "联网研究闭环：搜索并抓取前几页正文，返回带摘录的结果供你综合",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "n": {"type": "integer", "description": "候选结果数，默认 5"},
                    "pages": {"type": "integer", "description": "抓取正文的页数，默认 3"},
                },
                "required": ["query"],
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
                    "content": {"type": "string", "description": "简明完整的中文事实（时间/对象/范围等要素齐全，不刻意压缩成一句话）"},
                    "kind": {"type": "string", "enum": ["identity", "preference", "relation", "fact"]},
                },
                "required": ["subject", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "检索长期记忆（按关键词/主题），返回 [{id,subject,kind,content}]",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "subject": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_update",
            "description": "修订一条长期记忆的内容（控制者会话外需审批）",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "content": {"type": "string"},
                },
                "required": ["id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": "停用（软删除）一条长期记忆（控制者会话外需审批）",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subagent",
            "description": (
                "把一个可独立完成的子任务交给子智能体（全新上下文、只读工具），拿回它的最终报告。"
                "适合大规模翻查/多步检索/独立调研——避免几十轮工具调用把主对话上下文撑爆。"
                "prompt 写成完整自包含的指令（目标、范围、期望输出格式），子智能体看不到当前对话。"
                "子智能体最多可再嵌套派生 2 层（共 3 层），按需分治。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "给子智能体的完整任务指令（自包含）",
                    },
                },
                "required": ["prompt"],
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
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "语义检索（向量）：按意思而非字面查找身份档案/长期记忆/梦境报告"
                "（开启 rag.chat 时也含聊天片段）。适合模糊回忆（“谁抱怨过加班”）。"
                "精确关键词/日期请用 search_archive；未启用时本工具会明确报错。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "description": "返回条数，默认配置值"},
                    "kind": {"type": "string", "enum": ["profile", "memory", "digest", "chat"]},
                    "scene": {"type": "string", "enum": ["friend", "group"]},
                    "peer_id": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "profile_get",
            "description": "读取某个会话的身份档案（谁是谁）。subject 形如 friend/123 或 group/456",
            "parameters": {
                "type": "object",
                "properties": {"subject": {"type": "string"}},
                "required": ["subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "profile_update",
            "description": (
                "创建/更新某会话的身份档案（谁是谁、昵称映射、账号归属、关系）。"
                "只在确有把握时写入；会与现有内容合并。subject 形如 friend/123 或 group/456"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content": {"type": "string", "description": "完整 Markdown 档案内容"},
                    "name": {"type": "string", "description": "会话显示名（可空）"},
                },
                "required": ["subject", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "followup_list",
            "description": (
                "查看系统跟踪的未结待办（做梦从消息中提取 + 用户委托登记，含序号/所属会话/"
                "开立日期/最后动静；序号是稠密编号 1..N，done 后自动补位不稀疏）。"
                "用户问\"我还有什么没办\"\"最近有什么待办\"时用它或 cli todo。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "可选：按关键词过滤"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "followup_add",
            "description": (
                "登记一条新的跟踪待办——用户下达任务/委托/约定时（\"这个活动需要写稿\"\"帮我记着周三是 deadline\"）"
                "必须立即调用，不要等夜里做梦；**用户布置给别人去办的事同样登记**（assignee=受托人，"
                "如\"让小王写推送\"→assignee=小王，完成以TA交付为准）。标题写清 谁+什么事+时限（如有）；"
                "有明确截止日填 due_date（10-30/10月30日/2026-10-30 均可）；临近时限可再配 schedule_task 定提醒。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "待办标题（≤40字，谁+什么事+时限）"},
                    "subject": {"type": "string", "description": "相关会话: group/123 或 QQ号，可空"},
                    "detail": {"type": "string", "description": "补充细节（时间地点等）"},
                    "assignee": {"type": "string", "description": "负责人：布置给别人时填受托人；用户自办则空"},
                    "due_date": {"type": "string", "description": "截止日：10-30 / 10月30日 / 2026-10-30"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "followup_close",
            "description": (
                "关闭一条跟踪待办——这就是\"完成信号\"。用户说某事已经完成/搞定了/不用再跟了"
                "（如「表交了」「这事别记了」）时必须调用。id 与 query 至少给一个；"
                "id 是 followup_list 显示的序号（稠密编号）；"
                "query 匹配多条时会返回候选清单。status: done=已完成, dropped=失效/不再跟踪。"
                "**交付≠完成**：有负责人的待办，负责人说交了不算完成，须等用户验收确认后才能关闭。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "待办序号（followup_list 查）"},
                    "query": {"type": "string", "description": "关键词匹配待办（id 缺省时用）"},
                    "status": {"type": "string", "enum": ["done", "dropped"]},
                    "note": {"type": "string", "description": "关闭依据（用户原话更好）"},
                },
            },
        },
    },
]

# Sub-agents run with a READ-ONLY tool set: they research, they never act.
# cli is kept (its write commands are approval-gated anyway) because msg/
# media/forward reads live there.
_SUBAGENT_EXCLUDE = {
    "send_message", "send_media", "schedule_task", "remember",
    "memory_update", "memory_delete", "profile_update",
    "followup_close", "make_chart",
}
# NOTE: "subagent" is deliberately NOT excluded — sub-agents may nest up to
# MAX_SUB_DEPTH levels (enforced at runtime in Agent.run_subagent, not by
# the spec, so the model sees the tool and gets a clear error past the cap)
SUBAGENT_SPEC = [
    s for s in SPEC if s["function"]["name"] not in _SUBAGENT_EXCLUDE
]


def _now_iso() -> str:
    from datetime import datetime

    return f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}"


_FACE_WORDS = ("表情", "贴图", "sticker", "emoji", "颜文字", "表情包")


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
            if isinstance(result, dict):
                result.setdefault("now", _now_iso())
            payload = json.dumps(result, ensure_ascii=False, default=str)
        except ProtocolError as e:
            payload = json.dumps({"error": f"协议端错误: {e}", "now": _now_iso()}, ensure_ascii=False)
        except Exception as e:
            log.warning("tool %s failed: %s", name, e)
            payload = json.dumps({"error": str(e), "now": _now_iso()}, ensure_ascii=False)
        log.info("LLM tool  | %s(%s) -> %.600s", name, _args_brief(args), payload)
        log.debug("LLM tool full | %s(%s) -> %s", name, _args_brief(args), payload)
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
        if name == "profile_update":
            return f"更新身份档案 [{args.get('subject', '')}]: {str(args.get('content', ''))[:120]}"
        if name == "followup_add":
            return f"登记待办: {str(args.get('title', ''))[:120]}"
        if name == "followup_close":
            st = args.get("status") or "done"
            return f"关闭待办 {'#' + str(args.get('id')) if args.get('id') else str(args.get('query', ''))[:60]} ({st}): {str(args.get('note', ''))[:80]}"
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
        out = {"count": len(rows), "matches": [self._row_brief(r) for r in rows]}
        # hybrid: when vector RAG is on, add semantically-similar chat hits the
        # keyword pass missed (fuzzy recall)
        rag = getattr(self.service.runtime, "rag", None)
        if rag is not None and getattr(rag, "enabled", False) and self._rag_has_chat():
            sem = await rag.search(
                q, k=3, kind="chat",
                scene=args.get("scene") or None,
                peer_id=args.get("peer_id"),
            )
            if sem:
                out["semantic"] = [
                    {
                        "conv": f"{h['scene']}/{h['peer_id']}",
                        "score": h.get("score"),
                        "text": h["text"][:400],
                    }
                    for h in sem
                ]
        return out

    def _rag_has_chat(self) -> bool:
        try:
            return bool(self.service.cfg.rag.enabled and self.service.cfg.rag.chat)
        except Exception:
            return False

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
                        f = await self.adapter.get_friend_info(int(uin))
                        out["friend_name"] = f.display
                        if f.user_remark:
                            out["friend_remark"] = f.user_remark
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
            if kind == "members":
                return await self._members(args, limit)
        except ProtocolError as e:
            return {"error": f"协议端错误: {e}"}
        return {"error": f"未知 kind: {kind}"}

    async def _members(self, args: dict, limit: int) -> dict:
        """Group rosters: one group (full list / filtered) or cross-group
        lookup ("which groups is this person in") / intersection."""
        gids: list[int] = []
        raw = args.get("group_ids")
        if isinstance(raw, list):
            for g in raw:
                try:
                    gids.append(int(g))
                except (TypeError, ValueError):
                    continue
        if not gids and args.get("group_id") is not None:
            gids = [int(args["group_id"])]
        if not gids:
            return {"error": "需要 group_id（单群）或 group_ids（多群联查）"}
        if len(gids) > 10:
            return {"error": f"一次最多联查 10 个群（收到 {len(gids)}），请分批"}
        limit = max(1, min(int(limit or 100), 200))
        name_q = str(args.get("name", "")).strip().lower()
        uin_q = args.get("uin")

        def match(m) -> bool:
            if uin_q is not None and int(uin_q) != m.user_id:
                return False
            if name_q:
                hay = f"{m.card_name or ''}\n{m.user_name or ''}\n{m.user_id}".lower()
                if name_q not in hay:
                    return False
            return True

        rosters: dict[int, list] = {}
        errors: dict[int, str] = {}
        for gid in gids:
            try:
                rosters[gid] = await self.adapter.get_group_member_list(gid)
            except Exception as e:
                errors[gid] = str(e)[:120]

        if len(gids) == 1:
            gid = gids[0]
            if gid not in rosters:
                return {"error": f"群 {gid} 成员列表拉取失败: {errors.get(gid, '?')}"}
            members = rosters[gid]
            hits = [m for m in members if match(m)]
            out = {
                "group_id": gid,
                "member_count": len(members),
                "returned": min(len(hits), limit),
                "members": [
                    {"uin": m.user_id, "name": m.user_name or "", "card": m.card_name or ""}
                    for m in hits[:limit]
                ],
            }
            if len(hits) > limit:
                out["truncated"] = True
                out["hint"] = "超出 limit 已截断；加 name/uin 过滤可精确定位"
            return out

        out: dict = {"groups": []}
        for gid in gids:
            e: dict = {"group_id": gid}
            if gid in rosters:
                e["member_count"] = len(rosters[gid])
                if name_q or uin_q is not None:
                    hit = next((m for m in rosters[gid] if match(m)), None)
                    e["found"] = hit is not None
                    if hit is not None:
                        e["uin"] = hit.user_id
                        e["display"] = hit.display
            else:
                e["error"] = errors.get(gid, "拉取失败")
            out["groups"].append(e)

        if name_q or uin_q is not None:
            out["mode"] = "lookup"  # "此人在哪些群"
            return out

        # no filter over multiple groups -> intersection (common members)
        good = [g for g in gids if g in rosters]
        if good:
            common: set[int] | None = None
            for gid in good:
                ids = {m.user_id for m in rosters[gid]}
                common = ids if common is None else (common & ids)
            common = common or set()
            by_uin: dict[int, Any] = {}
            for gid in good:
                for m in rosters[gid]:
                    by_uin.setdefault(m.user_id, m)
            out["mode"] = "intersection"
            out["common_count"] = len(common)
            out["common_members"] = [
                {"uin": u, "display": by_uin[u].display} for u in sorted(common)[:limit]
            ]
            if len(common) > limit:
                out["truncated"] = True
        return out

    def _media_abs(self, row: dict) -> str | None:
        """Resolve a media row's stored (store-relative) path to absolute."""
        ms = getattr(self.service.runtime, "media", None)
        if ms is not None:
            p = ms.abs_path(row)
            return str(p) if p else None
        return row.get("file_path")

    async def t_describe_image(self, args: dict) -> dict:
        purpose = str(args.get("purpose", "")).strip() or "详细描述图片内容"
        force = bool(args.get("force"))
        medias = await self.repo.media_for_message(
            args["scene"], args["peer_id"], args["message_seq"]
        )
        images = [m for m in medias if m["kind"] in ("image", "face") and m["status"] == "done"]
        if not images:
            summaries = [m.get("summary") or "" for m in medias if m["kind"] == "image"]
            return {"descriptions": [s for s in summaries if s] or "(无已下载图片)"}
        out: list[str] = []
        for m in images[:3]:
            # cached descriptions are reused (no repeat vision calls)
            if m.get("summary"):
                out.append(f"(复用已识别结果) {m['summary']}")
                continue
            # faces/stickers: recognized only when the goal (or force) asks
            if m["kind"] == "face" and not force and not any(
                w in purpose.lower() for w in _FACE_WORDS
            ):
                out.append("(跳过) 表情/贴图；如确需识别请带 force=true 再说一次")
                continue
            desc = await self.llm.vision_describe(self._media_abs(m) or m["file_path"], purpose)
            out.append(desc)
            if m.get("id"):
                try:  # cache for the next ask
                    await self.repo.update_media(
                        m["id"], status=m["status"], summary=desc[:500]
                    )
                except Exception:
                    pass
        return {"purpose": purpose, "descriptions": out}

    async def t_subagent(self, args: dict) -> dict:
        from muze.ai.agent import MAX_SUB_DEPTH

        agent = getattr(self.service, "agent", None)
        if agent is None:
            return {"error": "agent 未装配"}
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return {"error": "prompt 不能为空"}
        result = await agent.run_subagent(prompt, self.ctx)
        if "error" in result:  # e.g. nesting past MAX_SUB_DEPTH
            return result
        return {
            "sub_session": f"#{result['id']}",
            "status": result["status"],
            "report": result["report"],
        }

    async def t_send_message(self, args: dict) -> dict:
        from muze.reply import strip_markdown

        text = args["text"]
        if getattr(self.service.cfg.reply, "strip_markdown", False):
            text = strip_markdown(text)
        segments = [out_text(text)]
        if args.get("reply_seq"):
            segments.insert(0, out_reply(int(args["reply_seq"])))
        outcome = await self.service.request_send(
            args["scene"], args["peer_id"], segments, text, self.ctx, "agent"
        )
        return {"result": outcome.text, "is_error": outcome.is_error}

    async def t_send_media(self, args: dict) -> dict:
        from pathlib import Path

        from muze.provider.milky.models import out_file, out_image, out_record, out_video
        from muze.store.media import to_file_uri

        scene = args.get("scene")
        peer = args.get("peer_id")
        seg = None
        preview = ""

        if args.get("path"):
            p = Path(str(args["path"]))
            if not p.exists():
                return {"error": f"文件不存在: {p}"}
            kind = _kind_by_ext(p.suffix.lower())
            uri = to_file_uri(p)
            seg = _seg_for_kind(kind, uri, p.name, out_image, out_record, out_video, out_file)
            preview = f"[{kind}] {p.name}"
        else:
            row = None
            if args.get("media_id"):
                row = await self.repo.media_by_id(int(args["media_id"]))
            elif peer is not None and args.get("message_seq") is not None:
                rows = await self.repo.media_for_message(
                    scene, int(peer), int(args["message_seq"])
                )
                row = rows[0] if rows else None
            if not row:
                return {"error": "未找到媒体（给 media_id，或 scene/peer/message_seq，或 path）"}
            if row["status"] != "done" or not row.get("file_path"):
                return {"error": f"媒体不可用（status={row['status']}）"}
            abs_p = self._media_abs(row) or row["file_path"]
            seg = _seg_for_kind(
                row["kind"], to_file_uri(abs_p), row.get("file_name") or "",
                out_image, out_record, out_video, out_file,
            )
            preview = f"[{row['kind']}] {row.get('file_name') or Path(abs_p).name}"

        segments = [seg]
        caption = str(args.get("caption", "") or "").strip()
        if caption:
            if getattr(self.service.cfg.reply, "strip_markdown", False):
                from muze.reply import strip_markdown

                caption = strip_markdown(caption)
            segments.append(out_text(caption))
            preview += f" {caption}"
        outcome = await self.service.request_send(
            scene, int(peer), segments, preview, self.ctx, "agent"
        )
        return {"result": outcome.text, "is_error": outcome.is_error}

    async def t_ocr_image(self, args: dict) -> dict:
        medias = await self.repo.media_for_message(
            args["scene"], args["peer_id"], args["message_seq"]
        )
        imgs = [
            m for m in medias
            if m["kind"] in ("image", "face") and m["status"] == "done" and m.get("file_path")
        ]
        if not imgs:
            return {"error": "该消息没有已下载的图片"}
        # local rapidocr first (free, instant); fall back to the vision model
        ocr = getattr(self.service.runtime, "ocr", None)
        source = "llm"
        if ocr is not None:
            try:
                provider, _note = ocr.resolve()
            except Exception:
                provider = ""
            if provider == "local":
                out = []
                for m in imgs[:3]:
                    text = await ocr.extract_text(self._media_abs(m) or m["file_path"])
                    out.append(text)
                return {"text": "\n---\n".join(out), "source": "local-ocr"}
        if self.llm is None:
            return {"error": "未配置视觉模型"}
        out = []
        for m in imgs[:3]:
            out.append(
                await self.llm.vision_describe(
                    self._media_abs(m) or m["file_path"],
                    "识别并逐字输出图片中的所有文字（保留换行），不要解释。",
                )
            )
        return {"text": "\n---\n".join(out), "source": source}

    async def t_transcribe_voice(self, args: dict) -> dict:
        from muze.perception import transcribe_media_row

        medias = await self.repo.media_for_message(
            args["scene"], args["peer_id"], args["message_seq"]
        )
        recs = [
            m for m in medias
            if m["kind"] == "record" and m["status"] == "done" and m.get("file_path")
        ]
        if not recs:
            return {"error": "该消息没有已下载的语音"}
        asr = getattr(self.service.runtime, "asr", None)
        if asr is None:
            return {"error": "语音转写未启用（ingest.asr）"}
        force = bool(args.get("force"))
        out_text, out_ts = [], []
        for m in recs[:2]:  # a message rarely carries more voice segments
            if not force and (m.get("summary") or "").startswith("[转写]"):
                # cached: surface it with the stored timestamps
                text = m["summary"][4:].lstrip()
                try:
                    ts = json.loads(m.get("asr_ts") or "[]")
                except Exception:
                    ts = []
                out_text.append(text)
                out_ts.extend(ts)
                continue
            try:
                text, ts = await transcribe_media_row(
                    asr, self.repo, m,
                    getattr(self.service.runtime, "media", None),
                )
            except Exception as e:
                return {"error": f"转写失败: {e}"}
            out_text.append(text)
            out_ts.extend(ts)
        return {
            "text": "\n".join(t for t in out_text if t) or "（语音无有效内容）",
            "timestamps": out_ts[:400],
            "note": "时间戳为 [词, 开始秒, 结束秒]；正文已回填消息",
        }

    async def t_make_chart(self, args: dict) -> dict:
        import asyncio

        from muze.mediaops import make_chart

        labels = args.get("labels") or []
        values = args.get("values") or []
        if not isinstance(labels, list) or not isinstance(values, list) or len(labels) != len(values):
            return {"error": "labels/values 必须等长且非空"}
        try:
            path = await asyncio.to_thread(
                make_chart,
                self.service.cfg.storage.data_dir,
                str(args.get("title", "chart")),
                labels,
                values,
                str(args.get("kind", "bar")),
            )
        except Exception as e:
            return {"error": f"绘图失败: {e}"}
        return {"path": str(path), "note": "已生成 PNG；用 send_media(path=...) 发送"}

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
                note=str(args.get("note", ""))[:200], **kwargs,
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
        entry = args.get("entry") or None
        if entry:
            entry = str(entry).strip()
        try:
            text = await __import__("asyncio").to_thread(read_document, path, entry)
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}
        return {"path": path, "entry": entry, "content": text}

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

    def _search_client(self):
        client = getattr(self, "_search", None)
        if client is None:
            from muze.research import SearchClient

            client = SearchClient(self.service.cfg.research)
            self._search = client
        return client

    async def t_web_search(self, args: dict) -> dict:
        from muze.research import SearchError, search_hint

        client = self._search_client()
        if not client.configured:
            return {"error": search_hint(self.service.cfg.research)}
        try:
            hits = await client.search(str(args.get("query", "")))
        except SearchError as e:
            return {"error": str(e)}
        finally:
            await client.close()
        return {"count": len(hits), "results": hits}

    async def t_research(self, args: dict) -> dict:
        from muze.research import SearchError, search_hint

        client = self._search_client()
        if not client.configured:
            return {"error": search_hint(self.service.cfg.research)}
        try:
            return await client.research(
                str(args.get("query", "")),
                args.get("n"),
                int(args.get("pages", 3)),
            )
        except SearchError as e:
            return {"error": str(e)}
        finally:
            await client.close()

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

    async def t_memory_search(self, args: dict) -> dict:
        q = str(args.get("query", "")).strip()
        if not q:
            return {"error": "query 不能为空"}
        rows = await self.repo.search_memories(
            q, subject=args.get("subject") or None, limit=int(args.get("limit", 30))
        )
        return {
            "count": len(rows),
            "memories": [
                {"id": r["id"], "subject": r["subject"], "kind": r["kind"], "content": r["content"]}
                for r in rows
            ],
        }

    async def t_memory_update(self, args: dict) -> dict:
        content = str(args.get("content", "")).strip()
        if not content:
            return {"error": "content 不能为空"}
        ok = await self.repo.update_memory(int(args["id"]), content)
        return {"updated": True} if ok else {"error": f"记忆 #{args['id']} 不存在"}

    async def t_memory_delete(self, args: dict) -> dict:
        ok = await self.repo.retire_memory(int(args["id"]))
        return {"deleted": True} if ok else {"error": f"记忆 #{args['id']} 不存在"}

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

    async def t_semantic_search(self, args: dict) -> dict:
        rag = getattr(self.service.runtime, "rag", None)
        if rag is None or not getattr(rag, "enabled", False):
            return {"error": "语义检索未启用（需 rag.enabled 且配置 rag.embedding）"}
        q = str(args.get("query", "")).strip()
        if not q:
            return {"error": "query 不能为空"}
        peer = args.get("peer_id")
        hits = await rag.search(
            q,
            k=args.get("k"),
            kind=args.get("kind") or None,
            scene=args.get("scene") or None,
            peer_id=int(peer) if peer not in (None, "") else None,
        )
        return {"query": q, "count": len(hits), "results": hits}

    async def t_profile_get(self, args: dict) -> dict:
        subject = str(args.get("subject", "")).strip()
        if not subject:
            return {"error": "subject 不能为空（friend/123 或 group/456）"}
        p = await self.repo.get_profile(subject)
        if not p:
            return {"subject": subject, "found": False}
        return {
            "subject": subject, "found": True, "name": p["name"],
            "content": p["content"], "updated_at": epoch_iso(p["updated_at"]),
        }

    async def t_profile_update(self, args: dict) -> dict:
        subject = str(args.get("subject", "")).strip()
        content = str(args.get("content", "")).strip()
        if not subject or not content:
            return {"error": "subject/content 不能为空"}
        await self.repo.upsert_profile(subject, str(args.get("name", "") or ""), content)
        log.info("PROFILE    | updated %s (%s chars)", subject, len(content))
        return {"ok": True, "subject": subject}

    async def t_followup_list(self, args: dict) -> dict:
        q = str(args.get("query", "") or "").strip()
        rows = await self.repo.list_followups(status="open", limit=50)
        if q:
            rows = [
                r for r in rows
                if q in r["title"] or q in (r["detail"] or "") or q in r["subject"]
            ]
        # id = the DENSE display number (1..N over the open list in open
        # order), so what the model sees matches /todo and stays compact
        # after closes; the DB id never leaks out
        from muze.digest import numbered_followups

        nos = {r["id"]: n for n, r in numbered_followups(
            await self.repo.list_followups(status="open", limit=50)
        )}
        return {
            "count": len(rows),
            "followups": [
                {
                    "id": nos[r["id"]], "subject": r["subject"], "title": r["title"],
                    "detail": r["detail"], "opened_date": r["opened_date"],
                    "last_date": r["last_date"], "assignee": r.get("assignee") or "",
                    "due_date": r.get("due_date") or "",
                    "delivered_date": r.get("delivered_date") or "",
                }
                for r in rows[:30]
            ],
            "hint": "id 为序号；交付≠完成：有负责人且 delivered_date 非空的在等用户验收，验收确认后才能 followup_close；用户委托新任务用 followup_add 登记",
        }

    async def t_followup_add(self, args: dict) -> dict:
        from muze.digest import local_date as _local_date
        from muze.digest import parse_due_date as _parse_due

        title = str(args.get("title", "") or "").strip()
        if len(title) < 4:
            return {"error": "标题太短，写清 谁+什么事+时限"}
        assignee = str(args.get("assignee", "") or "").strip()
        due = _parse_due(str(args.get("due_date", "") or ""))
        fid = await self.repo.add_followup(
            str(args.get("subject", "") or "").strip(),
            title,
            str(args.get("detail", "") or ""),
            source="user", opened_date=_local_date(),
            assignee=assignee, due_date=due,
        )
        if fid is None:
            return {"error": "已有一模一样的未结待办"}
        log.info("FOLLOW     | + #%s [%s] %s (via tool, assignee=%s due=%s)",
                 fid, args.get("subject", ""), title[:80], assignee or "-", due or "-")
        hint = "已入待办，每晚做梦前自动检查完成度"
        if assignee:
            hint = f"已入待办（负责人 {assignee}，等TA交付），每晚做梦前自动检查完成度"
        return {"added": True, "title": title, "assignee": assignee, "due_date": due, "hint": hint}

    async def t_followup_close(self, args: dict) -> dict:
        from muze.digest import numbered_followups

        st = args.get("status")
        st = st if st in ("done", "dropped") else "done"
        fid = args.get("id")
        if fid is None or not str(fid).isdigit():
            q = str(args.get("query", "") or "").strip()
            if not q:
                return {"error": "id 或 query 至少给一个（followup_list 查序号）"}
            rows = await self.repo.find_open_followups(q)
            if len(rows) != 1:
                nos = {r["id"]: n for n, r in numbered_followups(
                    await self.repo.list_followups(status="open", limit=50)
                )}
                return {
                    "error": f"「{q}」匹配 {len(rows)} 条未结待办，需明确 id（序号）",
                    "candidates": [
                        {"id": nos[r["id"]], "title": r["title"]} for r in rows[:8]
                    ],
                    "hint": "followup_list 查全量",
                }
            fid = rows[0]["id"]
        else:
            # digits are DENSE display numbers -- resolve positionally over
            # the open list (open order), never as the raw DB id
            open_rows = await self.repo.list_followups(status="open", limit=50)
            target = dict(numbered_followups(open_rows)).get(int(fid))
            if not target:
                return {"error": f"序号 {fid} 不存在（followup_list 查当前序号）"}
            fid = target["id"]
        ok = await self.repo.close_followup(int(fid), status=st, source="user")
        if not ok:
            return {"error": f"待办 #{fid} 不存在或已关闭"}
        log.info("FOLLOW     | - #%s closed (%s, via tool): %s",
                 fid, st, str(args.get("note", ""))[:80])
        return {"closed": True, "status": st}


def _args_brief(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False, default=str)
    return s[:300]


def _kind_by_ext(ext: str) -> str:
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return "image"
    if ext in (".mp4", ".mov", ".mkv", ".webm"):
        return "video"
    if ext in (".amr", ".silk", ".mp3", ".wav", ".ogg"):
        return "record"
    return "file"


def _seg_for_kind(kind, uri, name, out_image, out_record, out_video, out_file) -> dict:
    if kind in ("image", "face"):
        return out_image(uri)
    if kind == "record":
        return out_record(uri)
    if kind == "video":
        return out_video(uri)
    return out_file(uri, name or "")
