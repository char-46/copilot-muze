"""WebUI: aiohttp JSON API + single-page frontend.

Application-level: one web frontend serves every account in the process.
API calls pick an account with `?account=<name>` (default: the first one).
Bound to localhost by default; every /api/* request needs the bearer token
(generated and logged at startup when not configured).
"""

from __future__ import annotations

import copy
import json
import logging
import secrets
from pathlib import Path
from typing import NamedTuple

from aiohttp import web

from muze.chatlog import ChatLog
from muze.config import MilkyEndpoint, MuzeConfig, WebConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.store.repo import Repository
from muze.version import get_version, uptime_since

log = logging.getLogger("muze.web")

STATIC_DIR = Path(__file__).parent / "static"
SECRET_KEYS = {"access_token", "api_key", "token"}
SET_MASK = "***set***"


class WebAccount(NamedTuple):
    service: ControlService
    repo: Repository
    runtime: Runtime


def make_token(cfg: WebConfig) -> str:
    return cfg.token or secrets.token_urlsafe(18)


def _mask(node, key: str = "") -> object:
    """Deep-copy with any secret-ish leaf masked (by key name)."""
    if isinstance(node, dict):
        return {k: _mask(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask(v) for v in node]
    if key in SECRET_KEYS and isinstance(node, str) and node:
        return SET_MASK
    return node


def _coerce_like(value: object, like: object) -> object:
    if isinstance(like, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int) and str(value).strip().lstrip("-").isdigit():
        return int(value)
    if isinstance(like, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if isinstance(like, list):
        if isinstance(value, str):
            return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
        return value
    return value


def create_app(accounts: dict[str, WebAccount], token: str) -> web.Application:
    default_name = next(iter(accounts))

    def ctx(user: str = "web") -> ControlContext:
        return ControlContext(
            source="web",
            chat_key="web:default",
            scene="",
            peer_id=0,
            user_label=user,
            auto_confirm=False,
        )

    def pick(request: web.Request) -> WebAccount:
        name = request.query.get("account") or default_name
        acct = accounts.get(name)
        if acct is None:
            raise web.HTTPBadRequest(
                text=f"unknown account {name!r}; available: {', '.join(accounts)}"
            )
        return acct

    # ------------------------------------------------------------------
    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path.startswith("/api/"):
            hdr = request.headers.get("Authorization", "")
            supplied = hdr[7:].strip() if hdr.startswith("Bearer ") else request.query.get("token", "")
            if not secrets.compare_digest(supplied, token):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ------------------------------------------------------------------
    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def api_accounts(_request: web.Request) -> web.Response:
        out = [
            {"name": n, "self_id": a.runtime.self_id, "instance": a.runtime.instance_name}
            for n, a in accounts.items()
        ]
        return web.json_response({"accounts": out, "default": default_name})

    async def api_info(_request: web.Request) -> web.Response:
        per = []
        for name, (service, repo, runtime) in accounts.items():
            stats = await repo.stats()
            bf = runtime.backfiller.status() if runtime.backfiller else {}
            per.append(
                {
                    "name": name,
                    "self_id": runtime.self_id,
                    "stats": stats,
                    "backfill": bf,
                    "agent": service.agent is not None,
                    "capabilities": service.adapter.caps.summary_lines(),
                }
            )
        return web.json_response(
            {
                "version": get_version(),
                "uptime": uptime_since(accounts[default_name].runtime.started_at),
                "accounts": per,
            }
        )

    async def api_conversations(request: web.Request) -> web.Response:
        _, repo, _ = pick(request)
        active_h = float(request.query.get("active_h", 0)) or None
        limit = int(request.query.get("limit", 100))
        rows = await repo.list_conversations(active_within_h=active_h, limit=limit)
        return web.json_response({"conversations": rows})

    async def api_messages(request: web.Request) -> web.Response:
        _, repo, _ = pick(request)
        scene = request.match_info["scene"]
        peer = int(request.match_info["peer"])
        before_seq = request.query.get("before_seq")
        limit = int(request.query.get("limit", 50))
        rows = await repo.get_messages(
            scene, peer, before_seq=int(before_seq) if before_seq else None, limit=limit
        )
        return web.json_response({"messages": list(reversed(rows))})

    async def api_message_detail(request: web.Request) -> web.Response:
        _, repo, _ = pick(request)
        scene = request.match_info["scene"]
        peer = int(request.match_info["peer"])
        seq = int(request.match_info["seq"])
        row = await repo.get_message_row(scene, peer, seq)
        if not row:
            return web.json_response({"error": "not found"}, status=404)
        media = await repo.media_for_message(scene, peer, seq)
        return web.json_response({"message": row, "media": media})

    async def api_search(request: web.Request) -> web.Response:
        _, repo, _ = pick(request)
        q = request.query.get("q", "").strip()
        if not q:
            return web.json_response({"error": "missing q"}, status=400)
        scene = request.query.get("scene") or None
        peer = request.query.get("peer")
        limit = int(request.query.get("limit", 30))
        rows = await repo.search_messages(
            q, scene=scene, peer_id=int(peer) if peer else None, limit=limit
        )
        return web.json_response({"matches": rows})

    async def api_mediafile(request: web.Request) -> web.Response:
        _, repo, _ = pick(request)
        media_id = int(request.match_info["media_id"])
        row = await repo.media_by_id(media_id)
        if not row or not row["file_path"] or row["status"] != "done":
            return web.json_response({"error": "media unavailable"}, status=404)
        p = Path(row["file_path"])
        if not p.exists():
            return web.json_response({"error": "file missing"}, status=404)
        return web.FileResponse(p)

    async def api_command(request: web.Request) -> web.Response:
        service, _, _ = pick(request)
        data = await request.json()
        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "missing text"}, status=400)
        outcome = await service.execute(text, ctx())
        return web.json_response(
            {
                "text": outcome.text,
                "media_paths": outcome.media_paths,
                "is_error": outcome.is_error,
            }
        )

    async def api_agent(request: web.Request) -> web.Response:
        service, _, _ = pick(request)
        data = await request.json()
        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "missing text"}, status=400)
        if service.agent is None:
            return web.json_response({"error": "LLM 未配置"}, status=503)
        outcome = await service.agent.converse(text, ctx())
        return web.json_response(
            {"text": outcome.text, "media_paths": outcome.media_paths, "is_error": outcome.is_error}
        )

    async def api_agent_reset(request: web.Request) -> web.Response:
        service, _, _ = pick(request)
        if service.agent is not None:
            service.agent.reset("web:default")
        return web.json_response({"ok": True})

    async def api_pending(request: web.Request) -> web.Response:
        _, repo, _ = pick(request)
        rows = await repo.list_pending_actions()
        out = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            out.append(
                {
                    "id": r["id"],
                    "scene": r["scene"],
                    "peer_id": r["peer_id"],
                    "preview": payload.get("preview", ""),
                    "segments": payload.get("segments", []),
                    "created_by": r["created_by"],
                    "control": f"{r['control_scene']}/{r['control_peer_id']}",
                }
            )
        return web.json_response({"pending": out})

    async def api_pending_op(request: web.Request) -> web.Response:
        service, _, _ = pick(request)
        action_id = int(request.match_info["action_id"])
        op = request.match_info["op"]
        if op not in ("confirm", "cancel"):
            return web.json_response({"error": "bad op"}, status=400)
        outcome = (
            await service.confirm_action(action_id, ctx())
            if op == "confirm"
            else await service.cancel_action(action_id, ctx())
        )
        return web.json_response(
            {"text": outcome.text, "is_error": outcome.is_error}
        )

    # ------------------------------------------------------------ config
    async def api_config_get(request: web.Request) -> web.Response:
        service, _, runtime = pick(request)
        return web.json_response(
            {
                "instance": runtime.instance_name,
                "endpoint": _mask(runtime.endpoint.model_dump()),
                "masked": _mask(service.cfg.model_dump(mode="json")),
                "hint": "secrets 显示为 ***set***；留空/保持掩码提交则不修改。运行时生效，持久化请编辑 config.yaml 后 /reload",
            }
        )

    async def api_config_post(request: web.Request) -> web.Response:
        service, _, runtime = pick(request)
        data = await request.json()
        updates = data.get("updates") or {}
        if not isinstance(updates, dict) or not updates:
            return web.json_response({"error": "missing updates"}, status=400)

        cfg_dict = service.cfg.model_dump(mode="json")
        endpoint_dict = runtime.endpoint.model_dump()
        applied: list[str] = []
        for dotted, value in updates.items():
            keys = [k for k in str(dotted).split(".") if k]
            if not keys:
                continue
            if keys[0] == "milky":  # endpoint fields, e.g. milky.base_url
                leaf = keys[-1]
                if len(keys) != 2 or leaf not in endpoint_dict:
                    continue
                if leaf in SECRET_KEYS and (value == SET_MASK or value == ""):
                    continue
                endpoint_dict[leaf] = _coerce_like(value, endpoint_dict.get(leaf))
                applied.append(dotted + " (重启后生效)")
                continue
            node = cfg_dict
            ok = True
            for k in keys[:-1]:
                if not isinstance(node.get(k), dict):
                    ok = False
                    break
                node = node[k]
            if not ok:
                continue
            leaf = keys[-1]
            if leaf in SECRET_KEYS and (value == SET_MASK or value == ""):
                continue
            node[leaf] = _coerce_like(value, node.get(leaf))
            applied.append(dotted)

        try:
            fresh = MuzeConfig.model_validate(cfg_dict)
        except Exception as e:
            return web.json_response({"error": f"invalid config: {e}"}, status=400)
        for name in MuzeConfig.model_fields:
            setattr(service.cfg, name, getattr(fresh, name))
        try:
            runtime.endpoint = MilkyEndpoint.model_validate(endpoint_dict)
            if runtime.reloader is not None:
                runtime.reloader.endpoint = runtime.endpoint
        except Exception as e:
            return web.json_response({"error": f"invalid endpoint: {e}"}, status=400)
        llm = getattr(runtime, "llm", None)
        if llm is not None:
            llm.apply_config(service.cfg.llm)
        return web.json_response(
            {
                "applied": applied,
                "hint": "已运行时生效（milky.* 需重启）；持久化请编辑 config.yaml 后执行 /reload",
            }
        )

    async def api_logs_tail(request: web.Request) -> web.Response:
        _, _, runtime = pick(request)
        lines = max(10, min(int(request.query.get("lines", 300)), 3000))
        path = Path(runtime.log_path)
        if not path.exists():
            return web.json_response({"lines": []})
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 2_000_000))
                chunk = fh.read().decode("utf-8", errors="replace")
            tail = chunk.splitlines()[-lines:]
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"lines": tail})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    app.router.add_get("/api/accounts", api_accounts)
    app.router.add_get("/api/info", api_info)
    app.router.add_get("/api/conversations", api_conversations)
    app.router.add_get("/api/messages/{scene}/{peer}", api_messages)
    app.router.add_get("/api/message/{scene}/{peer}/{seq}", api_message_detail)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/mediafile/{media_id}", api_mediafile)
    app.router.add_post("/api/command", api_command)
    app.router.add_post("/api/agent", api_agent)
    app.router.add_post("/api/agent/reset", api_agent_reset)
    app.router.add_get("/api/pending", api_pending)
    app.router.add_post("/api/pending/{action_id}/{op}", api_pending_op)
    app.router.add_get("/api/logs/tail", api_logs_tail)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_post)
    return app


async def start_web(
    cfg: WebConfig,
    accounts: dict[str, tuple | WebAccount],
    chatlog: ChatLog,
) -> tuple[web.AppRunner, str]:
    token = make_token(cfg)
    norm = {
        n: (a if isinstance(a, WebAccount) else WebAccount(*a))
        for n, a in accounts.items()
    }
    app = create_app(norm, token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        raise RuntimeError(
            f"webui 端口 {cfg.host}:{cfg.port} 无法绑定（很可能已有另一个实例在运行，"
            f"或上一个进程尚未退出并占着端口）: {e}"
        ) from e
    url = f"http://{cfg.host}:{cfg.port}/"
    log.info("webui listening on %s (%s account(s))", url, len(norm))
    chatlog.log.info("WEBUI      | %s (token: %s)", url, token)
    return runner, token
