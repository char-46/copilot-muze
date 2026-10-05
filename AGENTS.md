# AGENTS.md

Working conventions for AI agents contributing to this repository.

## Project

copilot-muze — personal QQ copilot (Milky protocol over LLBot/Lagrange.Milky,
asyncio, per-account SQLite WAL, LLM agent with tools, WebUI/CLI/QQ fronts).
Python 3.11+, dependencies managed with `uv`.

## Environment

- Windows. Shell is PowerShell; interpreter: `A:\copilot-muze\.venv\Scripts\python.exe`;
  package manager: `uv` (`uv sync`, `uv lock`, `uv build`).
- torch on Windows comes from the PyTorch CUDA index (`tool.uv.sources` in
  pyproject.toml, currently cu130) — the PyPI default is CPU-only and GPU
  embedding silently never engages. Swapping torch requires the service to
  be STOPPED first (a running instance locks `.venv/Scripts/muze.exe` and
  `uv sync` dies with os error 5).
- `rg` is NOT available — use the Grep tool, not `rg`.
- `config.yaml` is gitignored and holds REAL secrets (LLM keys, controller QQ).
  Never commit, paste or log its values.
- The user runs a LIVE instance from this checkout on this machine. Never
  stop/kill muze processes unless explicitly asked; code/config flips go
  through the user's own `/restart`.

## Test gate

```
A:\copilot-muze\.venv\Scripts\python.exe -m pytest -q -n 4
```

All tests must pass before any commit. There is no separate lint/typecheck
command configured; pytest is the gate. (Baseline: 341 tests; ~17s with
`-n 4`, ~40s serial. Use plain `-m pytest -q` when debugging a single
worker's output — xdist interleaves logs.)

## Commit / release procedure (every feature batch)

Commit and tag AUTOMATICALLY once a feature batch is complete and the test
gate is green (standing permission from the user — no need to ask each
time; still never commit mid-batch or with a red gate). Follow these steps
in order:

1. **Bump the version in THREE places** (they must stay in sync):
   - `src/muze/version.py` → `BASE_VERSION = "0.1.0-alpha.N"`
   - `pyproject.toml` → `version = "0.1.0aN"`
   - `uv.lock` → the `copilot-muze` package's `version = "0.1.0aN"` line
2. **Run the test gate** (above); fix before proceeding if anything fails.
3. **Commit everything intended** (usually `git add -A`; check
   `git status --short` first — `config.yaml`, `data/`, `dist/` are ignored
   and will not be picked up). Message style follows `git log`:
   a single (long) line like
   `<type>(<scope>): <summary>; alpha.N — <details of the changes>; <N> tests pass`
   where type is `feat` / `fix` / `docs` / `docs+deploy` / `chore` etc.
4. **Tag**: `git tag v0.1.0-alpha.N`.
5. Verify with `git log --oneline -1` and report commit hash + tag to the user.

Build metadata (`+build.g<sha>.<ts>`) is appended automatically by
`get_version()` — never hand-write it.

## Deployment scripts (`scripts/`) — invariants

- `update.ps1` ONLY updates code + deps (git checkout latest tag, or `-Wheel`;
  `uv sync`). It must NEVER stop/start/restart the service — flipping the
  running instance is the user's `/restart` (or `scripts\start.ps1`).
- `start.ps1` is the ONLY script that manages processes: default start (with
  startup verification), `-Stop`, `-Restart`.
- `deploy.ps1` = first install + optional logon task (`muze-autostart`) which
  runs `start.ps1`.
- uv extras are remembered in `data\.update-extras` (`uv sync` is exact and
  would otherwise uninstall them on later plain runs).
- Scripts must stay **pure ASCII**: Windows PowerShell 5.1 reads no-BOM files
  as ANSI and non-ASCII chars corrupt regexes/strings.
- Beware PowerShell function-output unrolling: a function returning a
  one-element array yields a scalar, so `.Count` is empty — wrap call sites
  in `@(...)`.

## Architecture pointers

- `src/muze/main.py` — App assembly, event dispatch, maintenance loops.
- `src/muze/core/service.py` — slash commands (`@slash(...)`), confirm flow.
- `src/muze/ai/agent.py` — concurrent `::` sessions, SYSTEM_PROMPT, tools loop.
- `src/muze/ai/tools.py` — agent tool implementations (incl. WRITE_TOOLS).
- `src/muze/rag.py`, `ai/embeddings.py`, `rag_resource.py` — vector RAG,
  local-first Qwen embeddings, idle/GPU governor (RAG indexing is idle-gated
  and interruptible).
- `src/muze/store/db.py` — schema migrations (PRAGMA user_version; v11 =
  profiles/rag). `store/repo.py` — all SQL.
- `src/muze/daemon.py` — supervisor: rc=100 relaunch, crash backoff, job object.
