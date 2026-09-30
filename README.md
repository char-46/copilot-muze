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
| `/sync [target]` | roaming backfill now (one conversation or all) |
| `/unread` | recently active conversations with previews |
| `/search <kw> [n]` | full-text search over the archive |
| `/msg <scene/peer> <seq>` | single message detail + media status |
| `/media <scene/peer> <seq>` | extract downloaded media (sends images back) |
| `/summary <scene/peer> [n]` | LLM summary incl. image descriptions |
| `/send` / `/reply <target> [seq] <text>` | send as the controlled account |
| `/pending` `/confirm [id]` `/cancel [id]` | manage deferred sends |
| `/markread <target> [seq]` | mark conversation read (if supported) |

Non-slash input goes to the AI agent, which can query the archive, describe
images (vision model), expand forwarded messages, and draft sends — all sends
respect the confirmation policy (`control.confirm_all_sends`, default on;
CLI bypasses since it is local).

## Storage & logs

- `data/muze.db` — SQLite WAL: messages (dedup by scene/peer/seq), media index,
  pending actions. Safe for concurrent `serve` + `cli`.
- `data/media/YYYY-MM/` — downloaded images / voices / videos / files.
- `data/logs/muze.log` — every chat message (recv/backfill/sent/recall) rendered
  one-per-line, plus control, agent, confirm and media events. Never rotated.

## Versioning

Semver with pre-release + build metadata (`0.1.0-alpha.13+build.g<sha>.<ts>`);
every feature commit bumps the pre-release and is tagged. `muze version`,
`/status`, and the WebUI show the full string.

## Multiple accounts

Each QQ account runs as its own isolated stack — one LLBot login, one
config, one data dir, one daemon:

```bash
# account A
muze daemon -c muze-a.yaml    # storage.data_dir: ./data-a, web.port: 8300
# account B
muze daemon -c muze-b.yaml    # storage.data_dir: ./data-b, web.port: 8301
```

Everything (messages, media, logs, dreams, memories, scheduled tasks,
console history) lives under the account's `storage.data_dir`. The
database binds itself to the first account that opens it and **refuses to
start (exit code 4) if a different account points at the same data dir**,
so accounts can never silently mix.

## Tests

```bash
uv run pytest -q
```
