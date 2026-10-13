"""Remote CLI client (muze cli --remote): RemoteService against a fake
webui exposing /api/info + /api/command with bearer auth."""

from __future__ import annotations

import pytest
from aiohttp import web

from muze.cli.remote import RemoteService


@pytest.fixture
async def fake_webui():
    """Returns (url, token) once the app listens on an ephemeral port."""
    token = "test-token-123"

    async def info(request):
        if request.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"version": "0.1.0-test", "account": "main"})

    async def command(request):
        if request.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        text = (await request.json()).get("text", "")
        acct = request.query.get("account", "")
        if text == "/boom":
            return web.json_response(
                {"text": "命令失败", "media_paths": [], "is_error": True}
            )
        return web.json_response(
            {"text": f"echo:{text}@{acct}", "media_paths": [], "is_error": False}
        )

    app = web.Application()
    app.router.add_get("/api/info", info)
    app.router.add_post("/api/command", command)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", token
    await runner.cleanup()


async def test_ping_ok(fake_webui):
    url, token = fake_webui
    svc = RemoteService(url, token=token)
    info = await svc.ping()
    assert info["version"] == "0.1.0-test"
    await svc.close()


async def test_ping_bad_token(fake_webui):
    url, _ = fake_webui
    svc = RemoteService(url, token="wrong")
    with pytest.raises(RuntimeError, match="401"):
        await svc.ping()
    await svc.close()


async def test_ping_unreachable():
    svc = RemoteService("http://127.0.0.1:1", token="x", timeout=3)
    with pytest.raises(RuntimeError, match="cannot reach upstream"):
        await svc.ping()
    await svc.close()


async def test_execute_roundtrip(fake_webui):
    url, token = fake_webui
    svc = RemoteService(url, token=token, account="main")
    out = await svc.execute("/status")
    assert out.is_error is False
    assert out.text == "echo:/status@main"
    assert out.media_paths == []
    await svc.close()


async def test_execute_error_outcome(fake_webui):
    url, token = fake_webui
    svc = RemoteService(url, token=token)
    out = await svc.execute("/boom")
    assert out.is_error is True and out.text == "命令失败"
    await svc.close()


async def test_execute_token_rejected(fake_webui):
    url, _ = fake_webui
    svc = RemoteService(url, token="nope")
    out = await svc.execute("/status")
    assert out.is_error and "401" in out.text
    await svc.close()
