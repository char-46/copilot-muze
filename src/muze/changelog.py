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
        "version": "0.1.0-alpha.179",
        "date": "2026-11-07",
        "changes": [
            "防风控下沉到协议契约层（用户方向 2026-11-07：连续发相同消息会被风控）：①ProtocolError 增加契约语义 kind（refused=协议明确拒发、肯定没送达；network=传输失败/超时、送达与否未知——「已送达但响应丢失」正是昨晚 11 连发的根源；risk=本地预算拦截），调用方不再靠字符串嗅探；②ProtocolAdapter 公开发送面内置守卫并经 __init_subclass__ 自动包装所有后端实现（Milky 现有，未来 OB11/OB12/Lagrange 同契）——同会话同内容指纹 10 分钟窗口内直接抑制（网络性失败的指纹保留以抑制盲重发，明确拒发的指纹清除允许重发）、同会话发送节奏 ≥0.6s+抖动、滚动小时预算 160 次封顶；③配置新增 risk 段（dupe_window_sec/min_gap_sec/gap_jitter_sec/send_hourly_budget）。即便上层重试逻辑再出错，契约层也兜底不重发。",
        ],
    },
    {
        "version": "0.1.0-alpha.178",
        "date": "2026-11-07",
        "changes": [
            "投递绝不重复一批（实发 2026-11-07 凌晨）：①做梦日报对控制群整夜重发 11 次——每次「失败」的发送其实已送达、只是响应丢了（网络错误被当拒发走了分段重发），且 parked 重试每次都整文重发。现在：网络类错误不再同步分段重发（可能已送达，宁可报未确认）；parked 重试先查本地库的自身回执证据（自己发出的消息回显，正文头或转发链标题命中即视为已送达）直接撤销，重试也封顶 12 次；②晨报对全员连发 3 遍——某好友目标发送崩溃连带进程退出，守护拉起后任务 due-now 重跑全量广播。现在晨报每目标每天只尝试一次（送达/拒绝/崩溃都记档 meta，重启重跑只补未尝试的目标）。",
        ],
    },
    {
        "version": "0.1.0-alpha.177",
        "date": "2026-11-06",
        "changes": [
            "修正 a176 的打包校验问题：main.py 文档字符串里的 data\\protocol_online 是无效转义序列，Python 3.14 的编译校验报 SyntaxWarning 导致 deploy_wheel 断言失败（行为无变化）。",
        ],
    },
    {
        "version": "0.1.0-alpha.176",
        "date": "2026-11-06",
        "changes": [
            "重连补拉一批（实发 2026-11-06）：①连接池中毒修复——协议端重启后半开 keep-alive 连接让 POST 永久超时（milky 已恢复但 muze 一直报空错误 get_login_info），网络错误即弃会话、下次重建；②通知只发一次——「✅ 协议端已恢复连接」连发两条，是已送达但响应超时被判定失败后 chunked 兜底又重发了一遍，系统通知改为单次投递；③补拉防风控——重连补拉目标按离线窗口收敛（本地库查活跃会话，刚登录不再拉全名册两个大接口），单 worker 顺序拉取；④协议离线挂起更新——update.ps1 在协议端离线且服务在跑时拒绝更新（data\\protocol_online 标志，-Force 可越过），离线期翻版本无法验证且会掩盖重连回归（本次死连接池 bug 就是活例）。",
        ],
    },
    {
        "version": "0.1.0-alpha.175",
        "date": "2026-11-06",
        "changes": [
            "协议端离线不做梦（用户指示 2026-11-06：没连接到协议实现就不做梦，连接到以后拉消息补做）：调度门控在协议断连/降级启动期间完全不领任务（此前 LLBot 被手动关掉时 01:00 照样空跑出罐头日报，白做）；任务不丢失——到点未领的做梦/晨报在重连补拉完成、门打开的第一个 tick 立即补做，间隔任务重锚不漂移。",
        ],
    },
    {
        "version": "0.1.0-alpha.174",
        "date": "2026-11-06",
        "changes": [
            "停机期日报过期重做（用户 2026-11-06：为防风控手动关掉 LLBot，停机期间的梦基于残缺档案、补拉后永不刷新）：日报落档时记录当日已归档消息数（stats.msg_count）；每晚做梦除了补缺天，还会重做「档案比报告所见多出 20 条以上」的天（收据一并重置、收尾重跑），旧格式无计数器的报告仅识别罐头空日报；重做会在汇报里注明「档案补齐后重做」。LLBot 重开后补拉落地，下一个做梦夜自动重做停机各天。",
        ],
    },
    {
        "version": "0.1.0-alpha.173",
        "date": "2026-11-06",
        "changes": [
            "晨报疯发根因修复 + 本地命令开放给管理员（用户指示 2026-11-06）：①被打断的周期任务当天最多重置 due-now 两次（此前进程崩溃循环 + 5 分钟自启守望每轮都把晨报重发一遍，实发 11-04 06:00-07:40 约 19 份），超过即重锚到下一个原定时刻槽；②停机带原因日志（shutting down (原因)，下次崩溃循环一行定位）；③/restart、/shutdown 等本地命令管理员会话（controller 好友/控制群）可直接使用。",
        ],
    },
    {
        "version": "0.1.0-alpha.172",
        "date": "2026-11-03",
        "changes": [
            "做梦收尾自愈（实发 2026-11-03 01:08：发版重启打断了做梦，日报已存但待办同步/记忆提炼没跑，此后每晚「无缺口」直接跳过，收尾永远缺失）：每天收尾环节（完成度检查/待办同步/记忆提炼）落 meta 收据 dream_posts:<日期>；被打断的第二天晚上自动从已存报告补跑缺的环节并如实汇报「补跑了上次被打断的收尾」；完成度检查同样按收据去重，不再重复烧一遍全表核查（昨晚实测多花 ~1.4 万 token）。",
        ],
    },
    {
        "version": "0.1.0-alpha.171",
        "date": "2026-11-03",
        "changes": [
            "做梦与发版的时序保护 + 过期链接停试（用户指示 2026-11-03）：①00:05 日全量拉取未完成时调度器不领任务，做梦自动顺延到拉取落地（间隔任务重锚到原时刻，不漂移不重复）；②/restart 与 /full-restart 在做梦进行中自动排队，做完再翻转（上限 30 分钟，防卡死）；③媒体下载失败计数（schema v21 media.attempts），超过 ingest.media_max_retries（默认 3，0=永远重试）不再重试——QQ 文件 CDN 的过期链接不再每 10 分钟刷一遍失败告警。",
        ],
    },
    {
        "version": "0.1.0-alpha.170",
        "date": "2026-11-03",
        "changes": [
            "显存治理防乒乓 + 电池卸载（用户反馈 2026-11-02 电脑很卡：游戏贴着驱逐线时 embedding 每 60s 晋升↔驱逐整模型重载，4 核 + 数 GB 抖动）：晋升需 used% ≤ 驱逐线-20（gpu_promote_gap_pct），被驱逐后 15 分钟内不再晋升（promote_cooldown_sec，连击翻倍封顶 1 小时，稳定驻留 30 分钟复位）；无 AC 电源（psutil 电池）直接卸载本地 embedding 模型并禁止晋升（battery_unload，台式机无电池传感器不受影响）。",
        ],
    },
    {
        "version": "0.1.0-alpha.169",
        "date": "2026-11-02",
        "changes": [
            "负责人列显示收紧：名片式长名（名字 部门 手机号）在表格里只留名字（完整名片仍在合并转发正文行）；修一个回归——assignee 键解析后表格误用解析前的原始键；最近完成的备注列放宽到 60 字，减少截半句。",
        ],
    },
    {
        "version": "0.1.0-alpha.168",
        "date": "2026-11-02",
        "changes": [
            "待办负责人列去键化：dream 有时把 friend/号码 存进 assignee——展示层自动换成缓存昵称，无缓存时用标题里的受托人名（受托人：事项 格式），都没有才显示 好友+号码；同时待办同步提示词明确 assignee 一律填人名、号码只进 subject。",
        ],
    },
    {
        "version": "0.1.0-alpha.167",
        "date": "2026-11-02",
        "changes": [
            "/todo 与日报可读性重做：图片改为表格（#/事项/负责人/状态/截止 分列，按内容加权列宽、斑马纹、逾期红字、子任务缩进），正文不再是一整片混着 friend/qq 号的文字；纯文本合并转发改为一条一行（标题/负责·状态·截止/来源·日期 各占一行），来处显示缓存昵称而非裸 friend/12345；明确节点布局的命令无论长短都走合并转发；日报提示词改为手机扫读优先（短行、条目化、一行一个信息点）。",
        ],
    },
    {
        "version": "0.1.0-alpha.166",
        "date": "2026-11-01",
        "changes": [
            "新增 /model 命令统管所有本地模型：无参列出各模型（embedding/ASR/OCR）加载状态与所在设备；load=全部预载；unload=全部卸载并清显存缓存；ram/vram=已加载模型原地迁内存/显存（embedding 原地迁移，qwen-asr 重载迁移；faster-whisper 与 rapidocr 仅支持 CPU 会如实说明）。",
        ],
    },
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
