# alpha.73 批次计划（已确认范围）

范围：A 修 bug + B 群成员列表 + C 刷新保持(tab+页内状态) + D 数据tab。联网搜索本轮不动（用户自行配置）。

## A. schedule_task 参数被拒（bug 修复）
- 根因：`ai/tools.py:875` `scheduler.create(...)` 组装了 `kwargs`（delay_sec/at_ts/interval_sec）但没传 `**kwargs`，scheduler 抛 `need one of at_ts / delay_sec / interval_sec`。
- 修：`create(prompt=..., ctx=..., created_by="agent", note=..., **kwargs)`；scheduler 错误文案对齐工具层参数名（at_iso）。
- 测试：`t_schedule_task` 用 delay_sec → 断言 `created: True`（现无任何 agent 层 schedule 测试，属覆盖缺口）。

## B. LLM 信息获取补强（含多群联查）
1. `provider/milky/adapter.py` 新增 `get_group_member_list(group_id)`：
   - Milky API `get_group_member_list`（与既有 `get_group_member_info` 同族 snake_case 命名；GroupMember 模型已有 user_id/user_name(nickname 别名)/card_name(card 别名)/display）。
   - **列表缓存**：`dict[group_id, (ts, members)]`，TTL 10 分钟——多群联查/同轮重复查询不再重复打协议端；拿到全量列表时顺带预热 `_member_names`。
   - `provider/base.py` 加默认 `NotSupported` 方法。
2. `ai/tools.py` `directory` 扩展 `kind="members"`，多群联查语义：
   - **单群** `group_id`：无过滤 → 全量成员（limit 默认 100，超限 truncated:true 提示加 name 过滤）；带 `uin`/`name` 过滤 → 仅匹配项。
   - **多群** `group_ids`（2~10 个）：
     - 带 `uin`/`name` 过滤 → 「此人在哪些群」：逐群给出 {group, found, display}；
     - 无过滤 → **交集模式**：只返回共同成员 + 每群 member_count（不返回全量列表，防 token 爆炸）。
   - 超过 10 个群报错提示 LLM 分批；部分群拉取失败 → 逐群标 error，其余照常（partial 成功）。
   - `name` 匹配 card_name/user_name 子串（不区分大小写）；`uin` 精确。
3. `whois` 富化：挂 `get_friend_info` 拿 user_name/user_remark。
4. schema 同步：directory 的 kind enum + 参数说明（group_ids/name/uin）。
5. 测试：FakeAdapter 带成员列表 → 单群截断标记、uin 跨群联查、交集模式、>10 报错、部分失败、adapter 缓存命中不重复调用。

## C. WebUI 刷新保持 tab + 页内状态
- `app.js`：
  - tab 点击 handler 提为 `switchTab(b)` 函数；点击时 `lsSet("muze_tab", name)`；启动时恢复（校验 tab 存在，默认 chats）。
  - 页内状态：AI 页 `aiMode`/当前会话 id 存 `muze_ai_sess`（新对话用空标记），`loadAiSessions` 后恢复选中并 `openSession`；会话页最后 scene/peer 存 `muze_chat`，`loadConversations` 后恢复选中（不存在则忽略）。
  - 恢复失败静默降级（storageOK 机制已有）。

## D. WebUI「数据」只读 tab
- `web/server.py` 新增 GET（全部 token 鉴权、只读）：
  - `/api/tasks` → `repo.list_tasks(active_only=False)`
  - `/api/digests` → `repo.list_digests(...)`；`/api/digests/{date}` → `repo.get_digest(date)`（报告全文）
  - `/api/profiles` → `repo.list_profiles()`
  - `/api/media` → 媒体库查询（按 kind/conversation 过滤，repo 若无现成方法则补一个只读 list 查询；文件本体复用 `/api/mediafile/{id}`）
- `index.html` 新 tab 按钮「数据」+ pane：四个子区（任务列表含状态/下次运行、报告列表+查看、档案列表+内容、媒体库网格）。取消任务复用 `/api/command`（已有）。
- 不做 raw SQL 浏览器。

## 顺序与门禁
A → C → B → D，每步 `pytest -q` 全绿；完成后三处升版 0.1.0-alpha.73、单行 commit、tag，报告 hash+tag。
