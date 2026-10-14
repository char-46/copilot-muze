# copilot-muze

**English** | [简体中文](README.zh-CN.md)

Personal QQ copilot: archives every message (including already-read ones pulled
via roaming), saves media, and lets you drive your QQ account through commands
and natural language — from a QQ control chat, a WebUI, or a CLI.

```
muze 0.1.0-alpha.56+build.g<sha>.<utc-time>
```

## Architecture

```
┌─────────────────────────────────────────────┐
│ Protocol side (pluggable, Milky standard)    │
│   LLBot (recommended) / Lagrange.Milky       │
│   HTTP /api + WebSocket /event               │
└─────────────────┬───────────────────────────┘
                  │ Milky (access_token, localhost)
┌─────────────────▼───────────────────────────┐
│ muze (Python 3.11+, asyncio)                 │
│  provider abstraction (raw-Lagrange seam)    │
│  ingest: realtime events + roaming backfill  │
│  store:  SQLite (WAL) + media files          │
│  core:   ControlService (commands/confirm)   │
│  ai:     LLM abstraction + agent tools       │
│  fronts: QQ control chat / WebUI / CLI       │
└──────────────────────────────────────────────┘
```

The application core depends only on the `ProtocolAdapter` interface
(`src/muze/provider/base.py`). The Milky adapter is the first implementation;
a raw Lagrange (LagrangeV2 `Lagrange.Core.NativeAPI` C-ABI bindings, in-process)
adapter can be added later behind the same interface without touching anything
else. Capability probing (`/status`) reports what the active protocol side
actually supports and degrades lazily.

### Why Milky

- `get_history_messages` roaming pull recovers messages that arrived while the
  copilot was offline — including already-read and self-sent ones (there is no
  `message_sent` push event in the standard, so periodic rescans pick those up).
  Each conversation keeps **resumable watermarks** (`last_seq` high-water plus a
  persisted cursor/target for an unfinished deep pull), so an interrupted,
  page-capped or crash-killed pull continues where it stopped instead of leaving
  a hole that only `/sync force` could fill. The account DB also records
  `login_at` / `last_seen_at` / `last_shutdown_at` / `last_offline_at` so the
  downtime window is visible on the next start.
- **Forced logout is noticed and recovered**: the event socket (re)connect emits
  synthetic `bot_online` / `bot_offline` frames. On a real `bot_offline` (QQ
  kicked) or a dropped socket the app records the gap and tells the controller;
  when the socket comes back after `ingest.backfill.reconnect_gap_sec` (default
  60s) it immediately runs an incremental roaming pull (not just the periodic
  rescan) and reports how many messages were recovered. A process restart does
  the same via the `on_start` pull.
- `get_resource_temp_url` + file download APIs let muze persist every image /
  voice / video / file to disk before temp URLs expire. Blobs are sniffed by
  magic bytes (correct extension), and SILK voice is decoded to a playable
  `.wav` (the raw `.silk` is kept too).
- The standard is implementation-agnostic: protocol sides can be swapped
  (LLBot <-> Lagrange.Milky) with zero application changes.

## Setup

1. **Protocol side** — run [LLBot](https://github.com/LLOneBot/LuckyLilliaBot)
   (headless pure-protocol mode by default; PMHQ hook mode optional) and log in
   with the QQ account to be assisted. Note its Milky HTTP port & token.
   *Alternative:* Lagrange.Milky from
   [LagrangeV2](https://github.com/LagrangeDev/LagrangeV2) (expect missing file
   segments / start_seq paging; muze detects and degrades).
2. **muze** —

   ```bash
   uv sync
   copy config.example.yaml config.yaml   # then edit
   uv run muze serve -c config.yaml
   ```

   Fill in: `milky.base_url/access_token`, `control.admin_users` (your other
   QQ uins), `llm.chat.*` (any OpenAI-compatible endpoint: GLM / DeepSeek /
   MiMo ...), and optionally a `vision` model for image understanding.

3. **Fronts**
   - **QQ control chat**: private-message the account from an admin uin, or
     talk in the configured `control_group` (trigger with `@bot` or `/command`).
   - **WebUI**: `http://127.0.0.1:8300/` — the per-startup token is printed in
     the log. Conversations, media preview, full-text search, AI chat, pending
     confirmations, log tail, capability panel.
   - **CLI**: `uv run muze cli` — same command set; sends execute immediately
     (your local console is you). `muze serve` also opens an interactive
     console on its own stdin when running in a foreground terminal
     (`/help`, `/shutdown`, Ctrl+C; set `MUZE_FORCE_CONSOLE=1` to force).

## Deployment (one-click update, auto restart)

Day-to-day the service runs under **`muze daemon`**, a tiny supervisor:

- `/restart` (or `/reload` on code change) makes `serve` exit with rc=100 and
  the daemon relaunches it **immediately** — that is the hot-update path when
  you edit config/skills by hand.
- Crashes get exponential backoff (5s → 60s, reset after a stable run).
- On Windows the child sits in a Job Object with KILL_ON_JOB_CLOSE: if the
  daemon dies for *any* reason the kernel kills serve instantly, so an
  orphaned serve can never hold the web port or the database.

The deployment box (a plain git checkout + venv) is wrapped by three scripts:

- `scripts/deploy.ps1` — first install: prerequisites, `uv sync`, config
  bootstrap, optional logon auto-start task, then start.
- `scripts/update.ps1` — **update only, never touches the running service**
  (no stop, no start, no restart): checkout latest tag (or install a wheel)
  + `uv sync`. Flipping the live service to the new code is yours: `/restart`
  (console / WebUI / `muze cli`) re-execs serve in place — or exits rc=100
  and the daemon relaunches it.
- `scripts/start.ps1` — the ONLY script that manages processes:
  start (default) / `-Stop` / `-Restart`, with startup verification.

```powershell
# first install (dependencies + config bootstrap + optional logon auto-start)
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Extras rag-local -InstallTask

# UPDATE (one command; the running service keeps serving the old code)
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
# ...then whenever you like:  /restart   (console / WebUI / muze cli)

# variants
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Tag v0.1.0-alpha.57  # pin a tag
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Extras rag-local,rag-onnx
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -SkipPull             # deps only
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Stop | -Restart]     # lifecycle
```

Updates can also ship as a **wheel** — for boxes without git access. Build
with `uv build` on the dev machine, copy (or host) the wheel, and point
`update.ps1` at it: it installs the wheel instead of git pulling; the same
`/restart` (or `start.ps1 -Restart`) flips the running service:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel C:\dist\copilot_muze-0.1.0a57-py3-none-any.whl
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel https://host/copilot_muze-0.1.0a57-py3-none-any.whl -Extras rag-local
```

The wheel carries only the `muze` package: `scripts\` and the repo-level
`skills\` stay from the original checkout (user skills can live in
`data\skills`), and `config.yaml` / `data\` are never touched by either route.

Notes:

- `uv sync` may run while the service is up. Pure-python updates are fine
  (loaded `.py` files are not locked), but a **native dependency** update can
  hit a locked DLL — then: `start.ps1 -Stop`, rerun `update.ps1`,
  `start.ps1` (the script tells you the same).
- `start.ps1 -Stop/-Restart` stops with a hard kill (Windows cannot Ctrl-C a
  process attached to another console) — safe here: SQLite WAL plus
  per-conversation roaming watermarks, and the `on_start` pull recovers every
  message missed while down. For zero-downtime flips prefer `/restart`.
- **Extras are remembered** in `data\.update-extras`: the first
  `-Extras rag-local` sticks and later plain runs keep it (`uv sync` is exact
  and would otherwise uninstall them). Pass `-Extras` again to change.
- `git checkout --force` discards local edits to tracked files — a deployment
  checkout must not carry code changes. `config.yaml` and `data/` are
  untracked and never touched.
- `-InstallTask` registers a logon scheduled task (`muze-autostart`) that
  starts the service via `start.ps1`; remove it with
  `Unregister-ScheduledTask -TaskName muze-autostart`.

## Commands (QQ / Web / CLI share one implementation)

| command | effect |
|---|---|
| `/status` | version+build, uptime, archive stats, capabilities |
| `/sync [force\|时长] [target]` | roaming backfill now; `force` ignores the watermark and pulls EVERYTHING to the roaming end; a duration (`2d`, `12h`, `30m`, `1d12h`, `1个月`) re-pulls just that recent window (also ignoring the watermark) — duplicates are upsert-deduped, so both recover holes; if a pull is already running, its periodic reports are forwarded here |
| `/unread` | recently active conversations with previews |
| `/search <kw> [n]` | full-text search over the archive |
| `/day <date> [target]` | what happened on a day (`YYYY-MM-DD`/`today`/`yesterday`); scoped or overview |
| `/between <d1> <d2> [target] [n]` | date-range query |
| `/top [days]` | busiest conversations (all-time or recent) |
| `/range <target>` | that conversation's local archive: count + earliest/latest |
| `/msg <scene/peer> <seq>` | single message detail + media status |
| `/media <scene/peer> <seq>` | extract downloaded media (sends images back) |
| `/summary <scene/peer> [n]` | LLM summary incl. image descriptions |
| `/send` / `/reply <target> [seq] <text>` | send as the controlled account |
| `/pending` `/confirm [id]` `/cancel [id]` | manage deferred sends |
| `/markread <target> [seq]` | mark conversation read (if supported) |

## Command / LLM isolation

Input is strictly routed — plain chatter is archived but never answered:

- **`/command`** — commands. `/help` lists them in two sections: 应用级
  (process-level: help/version/read/fetch/reload/restart/shutdown — anything
  account-independent) vs 本账号 (operates on this account's archive/data).
  Register scope with `@slash(..., scope="app" | "bot")`.
- **`::message`** — the ONLY way to reach the LLM (two colons; half/full
  width both work: `::` `：：`).
- anything else — no reply.

Multi-bot safety (several accounts in one process):

- **Group with several bots**: `@bot /cmd` or `@bot ::msg` → only the
  addressed bot answers. Bare `/cmd` / `::msg` → only the **primary** bot
  (first `accounts.milky[]` entry) answers — a controller message never
  gets N replies.
- **Controller who is also a bot account** (own QQ in `authority`): the
  account's own outgoing messages (`is_self`) never trigger commands —
  except a true self-chat (peer == self). So 1000000001→bot2 is answered
  by bot2 alone; 1000000001 talking to itself still works.

`::` input goes to the AI agent, which can query the archive, describe
images (vision model), expand forwarded messages, and draft sends.

Agent turns are asynchronous and session-tracked:

- A `::` message is acknowledged **immediately** with a short session id
  (`#a1b2c3 已受理`). The turn then runs in the background — one busy
  conversation never blocks others, and a single conversation can hold
  **several sessions at once** (no history queue): they share that
  conversation's short-term history but each edit is committed atomically,
  and replies land in the order their prompts were sent, so contexts stay
  coherent under concurrency. Model turns are admitted per account: up to
  `llm.max_concurrent_turns` run at once, `llm.max_queued_turns` more may
  wait, and beyond that a turn is refused with 忙 rather than piling up.
- Sessions are **persisted** (SQLite `ai_sessions`): hitting the round limit
  (`llm.agent_max_steps`, default 48) leaves the turn `suspend` with its loop
  state saved; `error` turns keep a clean resume point. `/sess list` shows the
  recent sessions with an auto title (heuristic immediately, refined by a cheap
  LLM pass when `llm.auto_title` is on).
- `/sess <id>` shows the full trace of a session: title, prompt, every model
  round with received output size, every tool call and its compact result, and
  the final status. `/sess go [id]` resumes a suspended/errored turn (omit the
  id to resume **all** of them); `/sess <id> <消息>` continues a finished
  session with a new message, keeping its context. Both survive a restart.
  (The old `::继续` is now an alias for `/sess go`.)
- While a turn runs the agent sends compact progress lines — `⏳ 模型输出中
  （已收 N 字）` when the model is streaming (or `⏳ 思考中…` while silent,
  re-pinged every `progress_interval_sec`) and `🔧 工具名` per tool round.
- Slow ≠ stuck: chat requests stream, and the timeout (`llm.chat.timeout`)
  is a *silence* limit between chunks — a slow but producing model is never
  cut, while a dead connection and hung tools (`llm.turn_timeout_sec`) are.

### Identity profiles & RAG

- **Identity profiles** (`profiles.*`): the LLM periodically distills a short
  markdown "who is who" doc per active conversation (`profiles` table) —
  members, nicknames/aliases that map to the same person, which account is
  whom, relations. A compact index is injected into every agent prompt;
  `/who [friend/123|group/456]` reads one, and the agent can read/write them
  with `profile_get` / `profile_update` (the latter is write-gated).
- **Vector retrieval** (`rag.*`, opt-in): with `rag.enabled` and a usable
  `rag.embedding`, profiles, long-term memories and dream digests are indexed
  into a **sqlite-vec** store; the `semantic_search` tool (and a `semantic`
  block merged into `search_archive`) answers fuzzy-recall queries
  ("谁抱怨过加班") that keyword search misses. Set `rag.chat: true` to also
  index consecutive chat windows (per conversation, incremental).
- **Local-first embeddings**: default `Qwen/Qwen3-Embedding-0.6B`
  (`rag.embedding.backend: st`, install `uv sync --extra rag-local`; ONNX via
  `--extra rag-onnx`). The **device** is chosen per pass by a resource
  governor: GPU when the model fits in free VRAM *and* the GPU is quiet,
  otherwise CPU with a dynamically reduced thread count. A remote
  OpenAI-compatible endpoint is used only as a fallback when configured
  (`rag.embedding.remote_model`). One model id == one vector space, so
  changing the model rebuilds the index.
- **Idle-scheduled & interruptible**: indexing runs only on AC power, while
  CPU/GPU are below their idle thresholds and the user has been quiet for
  `rag.min_idle_sec`; it stops promptly when that stops being true (cursors
  resume next pass), throttles between batches, backs off (cooldown) after
  repeated failures, and can unload the local model when idle.

### Permissions (agent & commands)

- **Reads are unconditional anywhere**: archive search/browse, structured
  `search_archive` / `activity` / `directory`, message detail, media, stats,
  dreams, memories (`memory_search`), identity profiles (`profile_get`),
  semantic recall (`semantic_search`), skills, `fetch_url`, `read_document`,
  `describe_image`, `ocr_image`, `make_chart`, `web_search`, `research`.
- **Writes run freely only in the controller's session**: the control
  conversation (control group / authority DM) or a local front (CLI/WebUI/
  console). That covers `send_message`, `send_media` (archive media or a
  local file/chart), `schedule_task`, `remember`, `memory_update`,
  `memory_delete`, `profile_update`, and write-capable `cli` commands.
- **Anywhere else, every write is routed to the controller**: sends
  (text/media), scheduled tasks, memory writes, and write-capable `cli`
  commands become a pending request sent to the controller
  (`/confirm <id>` approves, `/cancel <id>` rejects). `/help` marks write
  commands with `[写]`.

### Research (agent)

`web_search` finds links and `research(query, pages)` searches then fetches
the top pages' text for synthesis — a closed research loop. Pick a backend in
`research.provider`: `tavily` / `brave` / `serper` / `searxng` / `duckduckgo`
(plus `api_key` / `base_url`). Unconfigured, the agent says it can't go online.

## Storage & logs

- `data/<uin>/muze.db` — per-account SQLite WAL: messages (dedup by
  scene/peer/seq), media index, pending actions. Safe for concurrent
  `serve` + `cli`.
- `data/blobs/<kind>/<sha[:2]>/<sha256><ext>` — content-addressed media
  store **shared by all accounts**, deduped three ways:
  - **pre-download** (zero network): market-face stickers by `emoji_id`,
    files by the protocol-provided `file_hash` — the same sticker spammed
    in ten groups is fetched exactly once;
  - **content-addressed** (zero duplicate disk): anything downloading to
    identical bytes lands on the same sha256 path;
  - **cross-account**: a shared `data/blobs/index.db` maps dedup keys to
    blobs, so account B reuses account A's downloads.
  Kinds: image / video / record / file / face (market-face stickers are
  archived and rendered in the webui like images).
- Media lifecycle: `pending -> downloading -> done` (blob stored) or
  `failed` (transient — retried by the maintenance loop). When the protocol
  side reports **HTTP 404/410** (QQ purges old roaming files) the row is
  parked as **`gone`** and never retried; historical failed-404 rows are
  swept to `gone` by the maintenance loop.
- `data/logs/muze.log` — every chat message (recv/backfill/sent/recall) rendered
  one-per-line, plus control, agent, confirm and media events. Never rotated.
- 小程序/卡片链接: light_app 卡片的标题/简介/跳转链接被提取进
  `text_content`（可全文搜索）；外部网页由 agent 工具 `fetch_url` /
  `read_document` 按需抓取解析。

## Versioning

Semver with pre-release + build metadata (`0.1.0-alpha.13+build.g<sha>.<ts>`);
every feature commit bumps the pre-release and is tagged. `muze version`,
`/status`, and the WebUI show the full string.

## Accounts & configuration layout

Config (v2, breaking since 0.1.0-alpha.27) groups protocol logins under
`accounts`, one entry per login; multiple protocol implementations and
multiple instances of the same implementation can be listed side by side
(ob11 / lagrange are reserved slots, milky is wired up today):

```yaml
control:
  controller: [10000]       # controller QQ ids (who may command)
  control_group: 0          # group id used as the control room; 0 = disabled
accounts:
  milky:
    - name: main
      base_url: "http://127.0.0.1:3010"     # LLBot #1
    - name: alt
      base_url: "http://127.0.0.1:3011"     # LLBot #2 (another QQ)
      controller: [20002]                   # extra controllers for this account
      digest: {enabled: false}              # any section may be overridden per account
  ob11: []                  # reserved
  lagrange: []              # reserved
storage: {data_dir: ./data}
web: {port: 8300}           # app-level: ONE webui serves every account
```

- **Auto per-account data dirs**: each account gets `<data_dir>/<QQ号>/`
  (muze.db + media/) derived from its logged-in uin — accounts never mix,
  no per-account storage config needed.
- **Top-level sections are shared defaults**; each account entry
  deep-merges its overrides over them. `web` is application-level and
  cannot be overridden per account.
- **`control.controller`** is the controller list (who: formerly the
  top-level `authority`, and before that `control.admin_users`); **`control.control_group`**
  is the control room (where). An account entry's `controller` unions the global one.
  The legacy top-level `authority` / per-account `authority` keys are still
  accepted and folded in (soft migration).
- One process runs all accounts; any account exiting stops the others and
  the daemon restarts the whole process. The console switches accounts
  with `/use <name>`; `muze cli -i <name>` picks one directly.
- The webui has an account selector (header) — every tab (chats, search,
  AI, pending, settings, logs, status) operates on the selected account.

## Quick configuration

```bash
uv run muze cfg init                    # interactive wizard → minimal config
uv run muze cfg show                    # whole config (secrets masked)
uv run muze cfg show accounts.milky.0.base_url   # one dotted path
uv run muze cfg set web.port 8301       # edit file (rewrites YAML, comments lost)
```

The WebUI has a **Settings** tab (grouped form; secrets stay masked unless
you type a new value) that applies changes at runtime — persist them by
editing `config.yaml` and running `/reload`.

## Remote debugging (relay box)

Debug a running instance from a dev machine that has no route to it — both
sides only make *outbound* connections, so neither needs a public IP. The
webui is the control plane (`POST /api/command` maps 1:1 to the CLI command
surface); an ssh reverse tunnel through any reachable relay carries it.

**One-time, on the relay** (never touched again, no matter how many boxes):

```bash
mkdir -p /root/.ssh && cat >> /root/.ssh/authorized_keys <<'EOF'
restrict,port-forwarding,command="/bin/false",permitopen="127.0.0.1:1",permitlisten="127.0.0.1:*" ssh-ed25519 AAAA... muze-debug-tunnel
EOF
```

Forward-only: no shell, no pty, no `-L` pivoting, and every forwarded
listener binds the relay's *loopback* (nothing new is publicly reachable;
a public probe just times out). Keep the matching private key once and copy
it to every muze box as `data/.ssh/muze_tunnel`.

**Per muze box** — drop the key, claim a port range, enable sshd for shells:

```powershell
# 1. key (box claims the 1830x range; box2 takes 1831x, ...)
mkdir data\.ssh; copy <muze_tunnel> data\.ssh\
# 2. optional, shells into this box (admin PowerShell):
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service sshd -StartupType Automatic
# add your dev pubkey to C:\ProgramData\ssh\administrators_authorized_keys
```

and in `config.yaml`:

```yaml
web:
  tunnel:                     # managed by serve: keeps `ssh -N -R` alive
    enabled: true
    host: relay.example.com
    key_file: data/.ssh/muze_tunnel
    forwards:
      - local_port: 8300      # webui (muze cli --remote)
        remote_port: 18300
      - local_port: 22        # this box's sshd (dev shells)
        remote_port: 18301
    token: <strong random>    # independent remote-access bearer (optional);
                              # the browser keeps web.token, --remote uses this
```

**From the dev machine:**

```powershell
ssh -L 8300:127.0.0.1:18300 -L 2222:127.0.0.1:18301 root@relay.example.com
muze cli --remote http://127.0.0.1:8300 --remote-token <tunnel token>   # REPL
ssh -p 2222 <box-user>@127.0.0.1                                        # shell
```

`--remote` drives the *running* instance (same commands, same `::` agent
turns, live state) instead of embedding a second one; token also via
`MUZE_REMOTE_TOKEN` or the `remote:` section of the dev machine's
`config.yaml`. Layered credentials: the relay sees no new public listener,
the tunnel key can only forward (never a shell on the relay), sshd on the
box wants its own key, and the webui requires its bearer token.

## Tests

```bash
uv run pytest -q
```

## License

CC BY-SA 4.0 — see [LICENSE](LICENSE).
