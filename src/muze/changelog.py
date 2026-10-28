"""Release notes for the copilot's own self-model.

Two jobs (user request 2026-10-28):
1. the agent injects the latest entries, so it answers "有什么新功能/怎么用"
   from FACTS instead of stale training habits, and never teaches syntax
   that a recent version replaced;
2. the nightly memory pass compares long-term memories against these notes
   and retires entries that contradict shipped behavior (e.g. a memory
   teaching the pre-alpha.148 raw-id /todo syntax).

Append a entry on EVERY feature release -- newest first, behavior-facing
wording (what changed for the USER and for YOU, the model), short.
"""

RELEASE_NOTES: list[dict] = [
    {
        "version": "0.1.0-alpha.153",
        "date": "2026-10-28",
        "changes": [
            "修复：自进化改挂到启动富集流水线（与身份档案/RAG 索引同一条已验证路径），此前独立任务在盒上从未执行。",
        ],
    },
        "date": "2026-10-28",
        "changes": [
            "修复：自进化启动任务因未持引用被垃圾回收，从未执行（现持有引用并留日志）。",
        ],
    },
    {
        "version": "0.1.0-alpha.151",
        "date": "2026-10-28",
        "changes": [
            "自进化：每次版本更新后自动对照变更日志修自己的习惯记忆（retire 过时用法、沉淀新习惯 /evolve 可手动触发）；习惯以小段固定块注入，不再每条消息携带变更日志。",
            "能力需求清单：工作中遇到能力缺口或被迫奇技淫巧时用 capability_wish 上报（自动去重）；/wishes 查看、/wishes close <序号> 关闭。",
            "提示词单一注册表 muze/prompts.py（共 10 个）+ 漂移守卫测试：新工具没教、关键规则离开提示词会直接挂测试。",
        ],
    },
    {
        "version": "0.1.0-alpha.150",
        "date": "2026-10-28",
        "changes": [
            "待办验收语义：负责人说\"交了\"只算已交付待验收，不再直接完成；必须用户验收确认（或 /done）才结，用户打回则清除交付标记继续跟。",
            "/todo 里\"待验收\"条目会标\"等你验收\"，晨报同。",
        ],
    },
    {
        "version": "0.1.0-alpha.149",
        "date": "2026-10-28",
        "changes": [
            "待办支持委托：/todo add @负责人 <事项> [due:10-30]；assignee=受托人、due_date=截止，逾期自动 ⚠ 提醒催办。",
        ],
    },
    {
        "version": "0.1.0-alpha.148",
        "date": "2026-10-28",
        "changes": [
            "待办编号改为稠密序号：/done、/todo drop、followup_close 引用的数字都是当前清单从 1 连续编号，done 后自动补位（旧的数据库 id 写法已废弃）。",
            "新入口：/todo add 立即登记；agent 有 followup_add 工具，用户委托当场入待办。",
            "每晚做梦前逐条检查所有未结待办的完成度（对照当日消息原文）。",
        ],
    },
    {
        "version": "0.1.0-alpha.147",
        "date": "2026-10-28",
        "changes": [
            "计划任务不再弹控制台窗口（S4U + wscript 静默）。",
        ],
    },
    {
        "version": "0.1.0-alpha.146",
        "date": "2026-10-26",
        "changes": [
            "/full-restart 走一次性计划任务（绝对路径），26 秒左右完整重启回来，不再依赖 5 分钟兜底。",
        ],
    },
    {
        "version": "0.1.0-alpha.144",
        "date": "2026-10-26",
        "changes": [
            "脚本运行器：/script <名称.ps1> [参数] 或 /script:名称.ps1，日志在 data/script_logs，/scriptlog 看尾部。",
        ],
    },
    {
        "version": "0.1.0-alpha.143",
        "date": "2026-10-25",
        "changes": [
            "重启续跑：服务重启后 AI 会话从检查点自动恢复（\"restored N AI sessions\"），不必从头再来。",
        ],
    },
]


def recent_notes(n: int = 3) -> str:
    """Render the n newest entries for prompt injection."""
    out = []
    for e in RELEASE_NOTES[: max(1, int(n))]:
        out.append(
            f"[{e['version']} / {e['date']}]\n"
            + "\n".join(f"- {c}" for c in e["changes"])
        )
    return "\n\n".join(out)
