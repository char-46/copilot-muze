# copilot-muze

[English](README.md) | **简体中文**

个人 QQ 副驾：归档每一条消息（含漫游拉回的已读消息）、保存媒体，并让你通过命令和自然语言操控自己的 QQ 账号 —— 操作入口有 QQ 控制会话、WebUI 和 CLI。

```
muze 0.1.0-alpha.56+build.g<sha>.<utc-time>
```

## 架构

```
┌─────────────────────────────────────────────┐
│ 协议端（可插拔，Milky 标准）                  │
│   LLBot（推荐） / Lagrange.Milky             │
│   HTTP /api + WebSocket /event               │
└─────────────────┬───────────────────────────┘
                  │ Milky (access_token, localhost)
┌─────────────────▼───────────────────────────┐
│ muze (Python 3.11+, asyncio)                 │
│  provider 抽象（预留 raw-Lagrange 接缝）      │
│  ingest: 实时事件 + 漫游补拉                  │
│  store:  SQLite (WAL) + 媒体文件              │
│  core:   ControlService（命令/审批）          │
│  ai:     LLM 抽象 + agent 工具               │
│  fronts: QQ 控制会话 / WebUI / CLI            │
└─────────────────────────────────────────────┘
```

应用核心只依赖 `ProtocolAdapter` 接口（`src/muze/provider/base.py`）。
Milky 适配器是第一个实现；今后可以在同一接口后面加一个 raw Lagrange
适配器（LagrangeV2 `Lagrange.Core.NativeAPI` 的 C-ABI 绑定，进程内运行），
其他代码一行不动。能力探测（`/status`）上报协议端实际支持什么，并按需降级。

### 为什么选 Milky

- `get_history_messages` 漫游拉取可以找回副驾离线期间错过的消息 —— 包括
  已读的和自发出去的（标准里没有 `message_sent` 推送事件，靠周期重扫补上）。
  每个会话维护**可恢复的水位**（`last_seq` 高水位 + 持久化的游标/目标，
  记录未完成的深拉），一次被中断、被页数上限截断或被崩溃杀掉的拉取会从
  停下的地方继续，而不是留下只有 `/sync force` 才能补的洞。账号库还记录
  `login_at` / `last_seen_at` / `last_shutdown_at` / `last_offline_at`，
  下次启动时离线窗口一目了然。
- **被强制下线能感知并恢复**：事件套接字每次（重）连都会产生合成的
  `bot_online` / `bot_offline` 帧。真正的 `bot_offline`（QQ 被踢）或套接字
  掉线时，应用记录断档并通知控制者；套接字在
  `ingest.backfill.reconnect_gap_sec`（默认 60 秒）之后恢复时，立即执行一次
  增量漫游拉取（而不是等周期重扫），并汇报找回多少条消息。进程重启则由
  `on_start` 拉取兜底。
- `get_resource_temp_url` + 文件下载 API 让 muze 在临时链接过期前把每张
  图片 / 语音 / 视频 / 文件落盘。Blob 按魔数嗅探（纠正扩展名），SILK 语音
  解码为可播放的 `.wav`（原始 `.silk` 也保留）。
- 标准与实现无关：协议端可以互换（LLBot <-> Lagrange.Milky），应用零改动。

## 安装

1. **协议端** —— 运行 [LLBot](https://github.com/LLOneBot/LuckyLilliaBot)
   （默认无头纯协议模式；可选 PMHQ hook 模式），用要辅助的 QQ 号登录，
   记下它的 Milky HTTP 端口与 token。
   *备选：*[LagrangeV2](https://github.com/LagrangeDev/LagrangeV2) 的
   Lagrange.Milky（预期存在文件分段缺失 / start_seq 分页问题；muze 会探测并降级）。
2. **muze** ——

   ```bash
   uv sync
   copy config.example.yaml config.yaml   # 然后编辑
   uv run muze serve -c config.yaml
   ```

   需填写：`milky.base_url/access_token`、`control.admin_users`（你的另一个
   QQ 号）、`llm.chat.*`（任意 OpenAI 兼容端点：GLM / DeepSeek / MiMo …），
   可选配一个 `vision` 模型用于图片理解。

3. **前端**
   - **QQ 控制会话**：用管理员 QQ 号私聊该账号，或在配置的 `control_group`
     里说话（`@bot` 或 `/命令` 触发）。
   - **WebUI**：`http://127.0.0.1:8300/` —— 每次启动的 token 打在日志里。
     会话列表、媒体预览、全文搜索、AI 对话、待审批项、日志尾巴、能力面板。
   - **CLI**：`uv run muze cli` —— 同一套命令；输入即执行（本地控制台就是
     你自己）。`muze serve` 在前台终端运行时也会在自己的 stdin 上开一个
     交互控制台（`/help`、`/shutdown`、Ctrl+C；设 `MUZE_FORCE_CONSOLE=1` 强制开启）。

## 部署（一键更新、自动重启）

日常服务跑在 **`muze daemon`** —— 一个极小的监督器之下：

- `/restart`（或源码变更时的 `/reload`）让 `serve` 以 rc=100 退出，daemon
  **立刻**把它重新拉起 —— 手工改 config/skills 时的热更新走的就是这条路。
- 崩溃按指数退避重启（5 秒 → 60 秒，稳定运行一段时间后重置）。
- Windows 上子进程放在带 KILL_ON_JOB_CLOSE 的 Job Object 里：daemon 因
  **任何**原因死掉，内核都会立刻杀掉 serve，不会再出现孤儿 serve 占着
  web 端口或数据库的情况。

部署机（一个普通的 git checkout + venv）由三个脚本分工：

- `scripts/deploy.ps1` —— 首次安装：前置检查、`uv sync`、配置引导、
  可选登录自启任务，然后启动。
- `scripts/update.ps1` —— **只做更新，任何时候都不碰运行中的服务**
  （不停、不启、不重启）：checkout 最新 tag（或装 whl）+ `uv sync`。
  切到新代码由你决定：发 `/restart`（控制台 / WebUI / `muze cli`），
  serve 原地 re-exec——或在 daemon 下以 rc=100 退出由其立刻重拉。
- `scripts/start.ps1` —— **唯一**管理进程的脚本：启动（默认）/
  `-Stop` / `-Restart`，带启动验证。

```powershell
# 首次安装（依赖 + 配置引导 + 可选开机自启任务）
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Extras rag-local -InstallTask

# 更新（一条命令；运行中的服务继续跑旧代码）
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
# ……之后你随时：发 /restart（控制台 / WebUI / muze cli）

# 变体
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Tag v0.1.0-alpha.57  # 固定某个 tag
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Extras rag-local,rag-onnx
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -SkipPull             # 只同步依赖
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Stop | -Restart]     # 生命周期
```

更新也可以走 **whl** —— 适合无法访问 git 的部署机。在开发机 `uv build`
出包，拷贝（或托管）whl 后让 `update.ps1` 指向它：装 whl 代替 git 拉取，
切换到新代码同样靠 `/restart`（或 `start.ps1 -Restart`）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel C:\dist\copilot_muze-0.1.0a57-py3-none-any.whl
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel https://host/copilot_muze-0.1.0a57-py3-none-any.whl -Extras rag-local
```

whl 只装 `muze` 包本身：`scripts\` 和仓库级 `skills\` 沿用最初检出
（用户技能可放 `data\skills`）；`config.yaml` / `data/` 两种路线都不会
被动到。

注意：

- `uv sync` 可能在服务运行时执行。纯 Python 更新没问题（已加载的
  `.py` 不加锁），但**原生依赖**变更可能撞上被锁的 DLL —— 那就
  `start.ps1 -Stop` → 重跑 `update.ps1` → `start.ps1`（脚本会这么提示）。
- `start.ps1 -Stop/-Restart` 的停止是硬杀（Windows 无法向另一个控制台
  里的进程发 Ctrl-C）—— 这里是安全的：SQLite WAL + 按会话的漫游水位，
  `on_start` 拉取会把停机期间错过的每条消息都找回来。零停机切换优先用
  `/restart`。
- **extras 会被记住**（`data\.update-extras`）：第一次 `-Extras rag-local`
  之后，后续裸跑也会带上（`uv sync` 是精确同步，否则会把它们卸掉）。
  要改就再传一次 `-Extras`。
- `git checkout --force` 会丢弃对已跟踪文件的本地修改 —— 部署检出上不要
  改代码。`config.yaml` 与 `data/` 不入库，永远不会被动到。
- `-InstallTask` 注册一个登录自启计划任务（`muze-autostart`），通过
  `start.ps1` 拉起服务；删除用
  `Unregister-ScheduledTask -TaskName muze-autostart`。

`update.ps1` 会打印 旧版本 → 新版本，并 tail 启动日志，更新失败立刻可见。注意：

- **停止是硬杀**（Windows 无法向另一个控制台里的进程发 Ctrl-C）。这里是
  安全的：SQLite WAL + 按会话的漫游水位，`on_start` 拉取会把停机期间错过的
  每条消息都找回来。
- `git checkout --force` 会丢弃对已跟踪文件的本地修改 —— 部署检出上不要改
  代码。`config.yaml` 与 `data/` 不入库，永远不会被动到。
- `-InstallTask` 注册一个登录自启计划任务（`muze-autostart`），通过
  `update.ps1 -SkipPull` 拉起服务；删除用
  `Unregister-ScheduledTask -TaskName muze-autostart`。

## 命令（QQ / Web / CLI 同一套实现）

| 命令 | 效果 |
|---|---|
| `/status` | 版本+构建、运行时长、归档统计、能力 |
| `/sync [force\|时长] [目标]` | 立刻漫游补拉；`force` 无视水位把**所有**能漫游的都拉回来；时长（`2d`、`12h`、`30m`、`1d12h`、`1个月`）只重拉这个近段窗口（同样无视水位）—— 重复消息按 upsert 去重，两种方式都能补洞；若已有拉取在跑，其周期汇报会转发到这里 |
| `/unread` | 最近活跃的会话及预览 |
| `/search <关键词> [n]` | 归档全文搜索 |
| `/day <日期> [目标]` | 某天发生了什么（`YYYY-MM-DD`/`today`/`yesterday`）；可限定目标或看全局 |
| `/between <d1> <d2> [目标] [n]` | 日期范围查询 |
| `/top [天数]` | 最活跃的会话（历史或近段） |
| `/range <目标>` | 该会话本地归档：条数 + 最早/最晚 |
| `/msg <scene/peer> <seq>` | 单条消息详情 + 媒体状态 |
| `/media <scene/peer> <seq>` | 提取已下载的媒体（把图片发回来） |
| `/summary <scene/peer> [n]` | LLM 摘要（含图片描述） |
| `/send` / `/reply <目标> [seq] <文本>` | 以被控账号的身份发送 |
| `/pending` `/confirm [id]` `/cancel [id]` | 管理延迟发送 |
| `/markread <目标> [seq]` | 标记会话已读（若支持） |

## 命令 / LLM 隔离

输入被严格路由 —— 普通聊天只归档、绝不回复：

- **`/命令`** —— 命令。`/help` 分两节列出：应用级（进程级：
  help/version/read/fetch/reload/restart/shutdown —— 与账号无关的一切）和
  本账号（操作该账号的归档/数据）。用 `@slash(..., scope="app" | "bot")`
  注册作用域。
- **`::消息`** —— 唯一能到达 LLM 的入口（两个冒号；半角/全角都行：
  `::` `：：`）。
- 其他 —— 不回复。

多 bot 安全（一个进程里多个账号）：

- **多 bot 群**：`@bot /命令` 或 `@bot ::消息` → 只有被 @ 的 bot 回答。
  裸 `/命令` / `::消息` → 只有**主** bot（`accounts.milky[]` 第一项）回答
  —— 控制者的消息永远不会收到 N 份回复。
- **控制者同时是 bot 账号**（自己的 QQ 在 `authority` 里）：该账号自己发出
  的消息（`is_self`）永不触发命令 —— 除非是真正的自聊（peer == self）。
  所以 1000000001→bot2 只有 bot2 回答；1000000001 自言自语仍然有效。

`::` 输入交给 AI agent，它可以查询归档、看图（vision 模型）、展开合并
转发、起草发送。

Agent 回合是异步的、带会话跟踪的：

- `::` 消息**立即**回执一个短会话 id（`#a1b2c3 已受理`）。回合随后在后台
  运行 —— 一个忙会话不会阻塞其他会话，且单个会话可以**同时**挂多个会话
  （不排历史队列）：它们共享该会话的短期历史，但每次编辑都原子提交，
  回复按各自发起顺序落位，并发下上下文依然连贯。模型回合按账号准入：
  最多 `llm.max_concurrent_turns` 个同时跑，`llm.max_queued_turns` 个可以
  等待，再多就直接以 忙 拒绝，不堆积。
- 会话**持久化**（SQLite `ai_sessions`）：撞到轮数上限
  （`llm.agent_max_steps`，默认 48）时该回合置为 `suspend` 并保存循环状态；
  `error` 回合保留干净的恢复点。`/sess list` 列出近期会话及自动标题
  （先启发式生成，`llm.auto_title` 开启时再用便宜的 LLM 覆盖一次）。
- `/sess <id>` 展示会话全程：标题、提示词、每轮模型输出接收字数、每个
  工具调用及其紧凑结果、最终状态。`/sess go [id]` 恢复挂起/出错的回合
  （省略 id 则恢复**全部**）；`/sess <id> <消息>` 在已完成的会话上带着
  上下文继续追问。两者都能跨重启。（旧 `::继续` 现在是 `/sess go` 的别名。）
- 回合运行期间 agent 发送紧凑的进度行 —— 模型流式输出时是
  `⏳ 模型输出中（已收 N 字）`（静默期则是 `⏳ 思考中…`，每
  `progress_interval_sec` 再提醒一次），每轮工具是 `🔧 工具名`。
- 慢 ≠ 卡死：聊天请求走流式，超时（`llm.chat.timeout`）是相邻数据块之间的
  **静默**上限 —— 慢但在出字的模型永远不会被掐，而死连接和挂死的工具
  （`llm.turn_timeout_sec`）会。

### 身份档案 & RAG

- **身份档案**（`profiles.*`）：LLM 周期性地为每个活跃会话蒸馏一份简短的
  markdown「谁是谁」文档（`profiles` 表）—— 成员、指向同一个人的昵称/别名、
  哪个账号是谁、关系。一个紧凑索引注入每次 agent 提示词；`/who
  [friend/123|group/456]` 读取一份，agent 可用 `profile_get` / `profile_update`
  读写（后者受写权限门控）。
- **向量检索**（`rag.*`，默认关）：`rag.enabled` 且 `rag.embedding` 可用
  时，身份档案、长期记忆与梦日志被索引进 **sqlite-vec**；`semantic_search`
  工具（以及合并进 `search_archive` 的 `semantic` 块）回答关键词搜不到的
  模糊回忆类问题（「谁抱怨过加班」）。设 `rag.chat: true` 还可以按会话
  增量地索引连续聊天窗口。
- **本地优先的 embedding**：默认 `Qwen/Qwen3-Embedding-0.6B`
  （`rag.embedding.backend: st`，安装 `uv sync --extra rag-local`；ONNX 用
  `--extra rag-onnx`）。**设备**由资源守护器逐轮选择：模型装得进空闲显存
  *且* GPU 空闲时用 GPU，否则用 CPU 并动态降低线程数。远程 OpenAI 兼容
  端点仅在配置了（`rag.embedding.remote_model`）时作为回退。一个模型 id ==
  一个向量空间，换模型即重建索引。
- **闲时调度、可随时打断**：索引只在 AC 电源、CPU/GPU 低于空闲阈值且用户
  安静超过 `rag.min_idle_sec` 时进行；条件一旦破坏立即停下（游标记录进度，
  下轮续跑），批间节流，连续失败后退避（冷却），空闲时可卸载本地模型。

### 权限（agent 与命令）

- **读操作无条件放行**：归档搜索/浏览、结构化的 `search_archive` /
  `activity` / `directory`、消息详情、媒体、统计、梦、记忆（`memory_search`）、
  身份档案（`profile_get`）、语义检索（`semantic_search`）、技能、
  `fetch_url`、`read_document`、`describe_image`、`ocr_image`、`make_chart`、
  `web_search`、`research`。
- **写操作只在控制者会话里自由执行**：控制会话（控制群 / authority 私聊）
  或本地前端（CLI/WebUI/控制台）。这覆盖 `send_message`、`send_media`
  （归档媒体或本地文件/图表）、`schedule_task`、`remember`、`memory_update`、
  `memory_delete`、`profile_update` 以及可写的 `cli` 命令。
- **其他任何地方，每个写操作都路由给控制者**：发送（文本/媒体）、定时
  任务、记忆写入、可写的 `cli` 命令都会变成一条发给控制者的待审批请求
  （`/confirm <id>` 批准，`/cancel <id>` 拒绝）。`/help` 用 `[写]` 标出写命令。

### 联网调研（agent）

`web_search` 找链接，`research(query, pages)` 先搜再抓前几页正文做综合 ——
一个闭环的研究流程。在 `research.provider` 里选后端：`tavily` / `brave` /
`serper` / `searxng` / `duckduckgo`（外加 `api_key` / `base_url`）。未配置时
agent 会说明自己上不了网。

## 存储与日志

- `data/<uin>/muze.db` —— 每账号 SQLite WAL：消息（按 scene/peer/seq 去重）、
  媒体索引、待执行动作。`serve` + `cli` 并发安全。
- `data/blobs/<kind>/<sha[:2]>/<sha256><ext>` —— 全账号**共享**的内容寻址
  媒体库，三重去重：
  - **下载前去重**（零网络）：商城表情按 `emoji_id`、文件按协议端给的
    `file_hash` —— 同一张表情在十个群里刷屏也只拉一次；
  - **内容寻址**（零重复磁盘）：任何下载出相同字节的文件都落到同一个
    sha256 路径；
  - **跨账号**：共享的 `data/blobs/index.db` 把去重键映射到 blob，账号 B
    直接复用账号 A 的下载。
  类型：image / video / record / file / face（商城表情像图片一样归档并在
  webui 里渲染）。
- 媒体生命周期：`pending -> downloading -> done`（blob 已存）或 `failed`
  （瞬时失败 —— 维护循环重试）。协议端报 **HTTP 404/410**（QQ 清理了旧漫游
  文件）时该行转为 **`gone`** 永不重试；历史上的 failed-404 由维护循环扫成
  `gone`。
- `data/logs/muze.log` —— 每条聊天消息（收/补拉/发/撤回）渲染成单行日志，
  外加控制、agent、审批与媒体事件。永不滚动。
- 小程序/卡片链接：light_app 卡片的标题/简介/跳转链接被提取进
  `text_content`（可全文搜索）；外部网页由 agent 工具 `fetch_url` /
  `read_document` 按需抓取解析。

## 版本

带预发布 + 构建元数据的 semver（`0.1.0-alpha.13+build.g<sha>.<ts>`）；
每个功能提交都递增预发布号并打 tag。`muze version`、`/status` 和 WebUI
都会显示完整字符串。

## 账号与配置结构

配置（v2，自 0.1.0-alpha.27 起破坏性变更）把协议登录收在 `accounts` 下，
一个登录一条；多种协议实现与同一实现的多个实例可以并排列出
（ob11 / lagrange 是预留位，milky 目前已接通）：

```yaml
control:
  controller: [10000]       # 控制者 QQ 号（谁可以下命令）
  control_group: 0          # 用作控制室的群号；0 = 关闭
accounts:
  milky:
    - name: main
      base_url: "http://127.0.0.1:3010"     # LLBot #1
    - name: alt
      base_url: "http://127.0.0.1:3011"     # LLBot #2（另一个 QQ）
      controller: [20002]                   # 该账号的额外控制者
      digest: {enabled: false}              # 任意小节都可按账号覆盖
  ob11: []                  # 预留
  lagrange: []              # 预留
storage: {data_dir: ./data}
web: {port: 8300}           # 应用级：一个 webui 服务所有账号
```

- **按账号自动建数据目录**：每个账号拿到 `<data_dir>/<QQ号>/`
  （muze.db + media/），由登录的 uin 推导 —— 账号之间绝不混数据，不需要
  为存储写任何按账号配置。
- **顶层小节是共享默认值**；每个账号条目把自己的覆盖深度合并上去。`web`
  是应用级的，不能按账号覆盖。
- **`control.controller`** 是控制者列表（谁：前身是顶层 `authority`，更早
  是 `control.admin_users`）；**`control.control_group`** 是控制室（在哪里）。
  账号条目的 `controller` 与全局取并集。旧顶层 `authority` / 按账号的
  `authority` 键仍被接受并并入（软迁移）。
- 一个进程跑所有账号；任一账号退出会停下其他账号，daemon 再重启整个进程。
  控制台用 `/use <name>` 切换账号；`muze cli -i <name>` 直接选一个。
- webui 头部有账号选择器 —— 每个标签页（聊天、搜索、AI、待审批、设置、
  日志、状态）都作用于所选账号。

## 快速配置

```bash
uv run muze cfg init                    # 交互式向导 -> 最小配置
uv run muze cfg show                    # 整份配置（密钥打码）
uv run muze cfg show accounts.milky.0.base_url   # 单个点路径
uv run muze cfg set web.port 8301       # 改文件（重写 YAML，注释丢失）
```

WebUI 有 **Settings** 标签页（分组表单；密钥保持打码，除非你输入新值），
改动即时生效 —— 持久化请编辑 `config.yaml` 后运行 `/reload`。

## 远程调试（中继机）

在没有路由到目标机的开发机上调试运行中的实例——两侧都只有*出站*连接，
都不需要公网 IP。控制面就是 WebUI（`POST /api/command` 与 CLI 命令面 1:1），
由一条经中继机的 ssh 反向隧道承载。

muze 盒上一次性准备（生成仅可转发的密钥，中继拿不到 shell）：

```powershell
ssh-keygen -t ed25519 -N "" -C muze-debug-tunnel -f data\.ssh\muze_tunnel
# 将 .pub 内容以【单行】追加到中继的 authorized_keys：
#   restrict,port-forwarding,command="/bin/false",
#   permitopen="127.0.0.1:1",permitlisten="127.0.0.1:18300" ssh-ed25519 AAAA...
# （无 shell、无 pty、无跳板，中继侧绑定被钉死在回环 18300）
```

然后在 `config.yaml`：

```yaml
web:
  tunnel:                     # 由 serve 托管：保活 `ssh -N -R` 子进程
    enabled: true
    host: relay.example.com   # 中继上现成的 sshd 承担全部工作
    user: root
    key_file: data/.ssh/muze_tunnel
    remote_port: 18300        # 必须等于上面 permitlisten 钉的端口
```

开发机侧：

```powershell
ssh -L 8300:127.0.0.1:18300 root@relay.example.com   # 跳板，保持打开
muze cli --remote http://127.0.0.1:8300 --remote-token <webui token>
```

`--remote` 驱动的是*运行中的*实例（同样的命令、同样的 `::` 会话、实时状态），
而非在本地再嵌入一份；token 也可走 `MUZE_REMOTE_TOKEN` 或开发机
`config.yaml` 的 `remote:` 段。凭据层层独立：中继不出现新的公网监听
（转发端口只绑其回环）、隧道密钥只能转发到那一个钉死端口、WebUI 本身
还要 bearer token。

## 测试

```bash
uv run pytest -q
```

## 许可证

CC BY-SA 4.0，详见 [LICENSE](LICENSE)。
