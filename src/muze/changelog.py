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
        "version": "0.1.0-alpha.165",
        "date": "2026-10-31",
        "changes": [
            "网页正文能力保证：fetch_url 静态直取不足（正文<400字的反爬壳/JS 页）或被拒（403/412 类）时，自动用系统 Edge/Chrome 无头模式渲染后重提正文（零新依赖，可 fetch.browser_cmd 指定浏览器、fetch.render_enabled 关闭）；两边谁长用谁，仍失败保留原有替代建议。",
        ],
    },
    {
        "version": "0.1.0-alpha.164",
        "date": "2026-10-31",
        "changes": [
            "链接标题识别：后台自动抓取聊天记录里 http(s) 链接的网页标题，追加为「[链接] 标题」（每 URL 只抓一次、不支持 JS 页），任务与报告从此能说清链接是什么（可 ingest.link_titles 关闭）。",
            "结单备注与进度同步：/done 支持批量序号加备注（/done 3 5 已发布到公众号），备注随关闭入库；/todo 新增「最近完成」段展示关闭详情；日报注入当日已结待办与备注，交接去向/发布结果对群里可见。批量结单先解析全部序号再统一提交，中途有误整批不提交。",
            "环节移交即完成（通用语义）：送审、交接给他人、转平台发布即视为本环节完成并关闭（备注写去向），最终结果需跟踪则另立新条目；不再让已交接的待办挂着。",
            "能力上报强化：网页类任务拿不到结果（JS 页无正文、需登录/交互/截图）必须记入能力清单——此前清单为空。",
        ],
    },
    {
        "version": "0.1.0-alpha.163",
        "date": "2026-10-28",
        "changes": [
            "待办序号漂移防护：清单中途变化（别的会话 done、做梦增删、自动过期）后 /done 不会误关其它条目——系统记住你最近一次 /todo 看到的编号，按当时那条精确关闭；那条已被关闭就拒绝并提示刷新。/todo 与 /done 仍是纯数据库操作，零 token。",
            "待办子任务分级：同一任务下用 /todo add ... under:<序号> 或 followup_add 的 parent 挂子任务，显示为 #3.1/#3.2；/done 3.1 单独关闭，关闭父任务连带关闭其子任务；父任务已关的子任务自动升为顶层，不会出现无法关闭的孤儿编号。",
            "输出图片统一外边距：渲染图四周固定白边（多页之间留间隔），图表边距加大，不再贴边。",
        ],
    },
    {
        "version": "0.1.0-alpha.162",
        "date": "2026-10-28",
        "changes": [
            "待办截止日补齐：布置原文写明活动/交付日期时必须填 due_date——「成稿后交/完成后发我」不再被视为无截止，同一活动布置给多人时逐条填相同日期；模型回短格式（10-30、10月30日15:30）也会归一化为 YYYY-MM-DD 再入库（此前会原样存储导致逾期判断失灵）。",
        ],
    },
    {
        "version": "0.1.0-alpha.161",
        "date": "2026-10-28",
        "changes": [
            "系统提示词禁止主动征求许可：不再问「要不要我…」「需要我…吗」「你希望我怎么处理」，能做的直接做完再汇报结果；只有缺少无法从档案/上下文推断的关键信息时才提一个具体问题并附默认方案（代发消息的系统确认流程不受影响）。漂移守卫测试锁定该规则。",
        ],
    },
    {
        "version": "0.1.0-alpha.160",
        "date": "2026-10-28",
        "changes": [
            "充值后不再被旧熔断挡住：用户主动命令 /dream、/balance 会先解除账户熔断再尝试（实测充值到账后半小时内 /dream 仍被缓存的 402 拒绝；若账户确实没钱，第一次调用失败会立即重新熔断）。",
        ],
    },
    {
        "version": "0.1.0-alpha.159",
        "date": "2026-10-28",
        "changes": [
            "身份档案进一步降频：默认改为夜间做梦捎带刷新（一天最多一次，六小时维护循环触发已关），且增量归纳只喂老档案+自上次归纳以来的新消息，不再重读整个窗口。",
            "新增成本统计模块：每笔 LLM 调用打功能标签（做梦/价值打分/档案/代理/总结等，账本另存 costs.features.json），/cost 输出按模型+按功能双分解，可回答某功能总花多少。",
        ],
    },
    {
        "version": "0.1.0-alpha.158",
        "date": "2026-10-28",
        "changes": [
            "身份档案不再每次重建：只有会话有新消息才重新生成（此前每轮无条件重写全部 30 个会话，约占全部开销三分之二），并加每日调用预算与失败熔断。",
            "LLM 账户故障（402/余额不足/配额）统一层：判定只在 muze/ai/llm.py，后台链路统一走 bg_chat 短路跳过，不再各功能各自判 402。",
        ],
    },
    {
        "version": "0.1.0-alpha.157",
        "date": "2026-10-28",
        "changes": [
            "记忆提炼不再注入变更日志：过时记忆的清理交给每版本一次的习惯校准即可，重复注入既常驻增本又污染提示词关键字。",
        ],
    },
    {
        "version": "0.1.0-alpha.156",
        "date": "2026-10-28",
        "changes": [
            "待办同步强制登记：【当日原始布置】里每条明确的任务布置一律登记（同一件事布置给不同人的按人分别记），不再由模型自行判断\"可能不用跟\"。",
        ],
    },
    {
        "version": "0.1.0-alpha.156",
        "date": "2026-10-28",
        "changes": [
            "待办同步强制登记：【当日原始布置】里每条明确的任务布置一律登记（同一件事布置给不同人的按人分别记），不再由模型自行判断\"可能不用跟\"。",
        ],
    },
    {
        "version": "0.1.0-alpha.155",
        "date": "2026-10-28",
        "changes": [
            "待办提取兜底：做梦同步不再只吃报告——当天你发出的布置/指令类消息（安静会话里的孤句）直采后一并喂给同步，补做旧日期的梦也能补上被报告省略的委托任务。",
        ],
    },
    {
        "version": "0.1.0-alpha.154",
        "date": "2026-10-28",
        "changes": [
            "修复：/evolve 取 LLM 客户端的方式（此前报 ControlService 无 llm 属性）；部署脚本增加 wheel 内源码可解析校验，杜绝陈轮/坏轮上盒。",
        ],
    },
    {
        "version": "0.1.0-alpha.153",
        "date": "2026-10-28",
        "changes": [
            "修复：版本更新后的习惯校准改挂到启动富集流水线（与身份档案/RAG 索引同一条已验证路径），此前独立任务在盒上从未执行。",
        ],
    },
    {
        "version": "0.1.0-alpha.152",
        "date": "2026-10-28",
        "changes": [
            "修复：自进化启动任务因未持引用被垃圾回收，从未执行（现持有引用并留日志）。",
        ],
    },
    {
        "version": "0.1.0-alpha.151",
        "date": "2026-10-28",
        "changes": [
            "习惯校准：版本更新后自动对照变更日志清理过时记忆、补上新用法习惯（/evolve 手动触发）。",
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


def validate_notes() -> list[str]:
    """Shape check for manual edits: every entry needs its three keys and
    a version string. Guards against the recurring typo of inserting a new
    entry that drops its opening dict brace (which once made this module
    unparseable and shipped to the box)."""
    problems = []
    for i, e in enumerate(RELEASE_NOTES):
        for k in ("version", "date", "changes"):
            if k not in e:
                problems.append(f"entry {i} missing {k!r}")
        if e.get("version") and not str(e["version"]).startswith("0."):
            problems.append(f"entry {i} odd version: {e['version']!r}")
        if not isinstance(e.get("changes"), list) or not e.get("changes"):
            problems.append(f"entry {i} has no changes")
    return problems
