# copilot-muze

Personal QQ copilot: archives every message (including already-read ones pulled
via roaming), saves media, and lets you drive your QQ account through commands
and natural language from a control chat, a WebUI, or a CLI.

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
a raw Lagrange (LagrangeV2 NativeAPI) adapter can be added later without
touching the rest of the codebase.

## Status

Work in progress (0.1.0 alpha series). See git log for the build history.

## Versioning

Semver with pre-release and build metadata, e.g.
`0.1.0-alpha.7+build.g1a2b3c4.20260928T120000Z`. Run `muze version`.
