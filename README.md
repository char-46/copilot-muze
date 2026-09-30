# copilot-muze

Personal QQ copilot: archives every message (including already-read ones pulled
via roaming), saves media, and lets you drive your QQ account through commands
and natural language — from a QQ control chat, a WebUI, or a CLI.

```
muze 0.1.0-alpha.13+build.g<sha>.<utc-time>
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
- `get_resource_temp_url` + file download APIs let muze persist every image /
  voice / video / file to disk before temp URLs expire.
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

## Commands (QQ / Web / CLI share one implementation)

| command | effect |
|---|---|
| `/status` | version+build, uptime, archive stats, capabilities |
| `/sync [force] [target]` | roaming backfill now; `force` ignores the watermark and pulls EVERYTHING to the roaming end (duplicates are upsert-deduped — recovers holes) |
| `/unread` | recently active conversations with previews |
| `/search <kw> [n]` | full-text search over the archive |
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
images (vision model), expand forwarded messages, and draft sends — all sends
respect the confirmation policy (`control.confirm_all_sends`, default on;
CLI bypasses since it is local).

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
authority: [10000]          # global controller QQ ids (who may command)
accounts:
  milky:
    - name: main
      base_url: "http://127.0.0.1:3010"     # LLBot #1
    - name: alt
      base_url: "http://127.0.0.1:3011"     # LLBot #2 (another QQ)
      authority: [20002]                    # extra controllers for this account
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
- **authority** is the controller list (formerly `control.admin_users`);
  an account entry's `authority` unions the global one.
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

## Tests

```bash
uv run pytest -q
```
