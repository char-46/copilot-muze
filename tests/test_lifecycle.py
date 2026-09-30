"""App lifecycle: restart intent + idempotent, fault-tolerant shutdown."""

import logging

from muze.config import AccountSpec, MilkyEndpoint, MuzeConfig
from muze.main import App


def make_app(name: str = "main") -> App:
    spec = AccountSpec(name, "milky", MilkyEndpoint(), MuzeConfig())
    return App(spec, "config.yaml")


def test_stop_quiet_preserves_restart_intent():
    app = make_app()
    app._request_restart()
    assert app.restart_requested is True
    # propagating shutdown to siblings must NOT wipe the restart flag
    app._stop_quiet()
    assert app.restart_requested is True
    assert app._stop.is_set()
    # an explicit stop does clear it (user ran /shutdown)
    app._request_stop()
    assert app.restart_requested is False


async def test_shutdown_is_idempotent(caplog):
    app = make_app()
    with caplog.at_level(logging.INFO, logger="muze.main"):
        await app.shutdown()
        await app.shutdown()  # second call must be a no-op
    lines = [r for r in caplog.records if "shutting down" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert app._shutdown_done is True


async def test_shutdown_survives_component_failures(caplog):
    class Boom:
        async def close(self):
            raise RuntimeError("boom")

        async def stop(self):
            raise RuntimeError("boom stop")

    app = make_app()
    app.conn = Boom()
    app.adapter = Boom()
    app.backfiller = Boom()
    with caplog.at_level(logging.ERROR, logger="muze.main"):
        await app.shutdown()  # must not raise
    assert app._shutdown_done is True
    assert any("failed during shutdown" in r.getMessage() for r in caplog.records)


async def test_shutdown_bounds_a_wedged_component(caplog):
    """A cleanup step that never finishes is cut off by the timeout, so a
    hung adapter/db can never wedge the whole exit."""
    import asyncio

    class Wedged:
        async def close(self):
            await asyncio.sleep(3600)
            return 0

    app = make_app()
    app.conn = Wedged()
    with caplog.at_level(logging.WARNING, logger="muze.main"):
        await asyncio.wait_for(app.shutdown(), timeout=12)  # returns, not hangs
    assert app._shutdown_done is True
    assert any("timed out" in r.getMessage() for r in caplog.records)


def test_parent_alive_detects_missing():
    import os as _os

    from muze.main import _parent_alive

    assert _parent_alive(_os.getpid()) is True
    assert _parent_alive(999999) is False  # no such process


def test_reexec_uses_same_serve_args(monkeypatch):
    import muze.main as m

    calls = []
    monkeypatch.setattr(m.os, "execv", lambda exe, args: calls.append((exe, args)))
    monkeypatch.setattr(m.sys, "argv", ["muze", "serve", "-c", "x.yaml"])
    m._reexec()
    assert calls, "execv should be called"
    _exe, args = calls[0]
    assert args[1:5] == ["-m", "muze", "serve", "-c"] and args[-1] == "x.yaml"


def test_sigint_handler_graceful_then_force(monkeypatch):
    """1st Ctrl-C stops every account (no exception into the loop);
    2nd Ctrl-C forces process exit."""
    import os

    from muze.main import _make_sigint_handler

    class DummyLoop:
        def call_soon_threadsafe(self, fn):
            fn()

    app = make_app()
    app.restart_requested = True  # must survive the graceful stop
    handler = _make_sigint_handler([app], DummyLoop())

    forced = {}
    monkeypatch.setattr(os, "_exit", lambda code: forced.setdefault("code", code))
    monkeypatch.setattr(
        "muze.main.threading.Thread",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )

    handler(2, None)
    assert app._stop.is_set() and app.restart_requested is True
    assert "code" not in forced  # first press is graceful

    handler(2, None)
    assert forced["code"] == 130  # second press forces exit


# ------------------------------------------------ forced logout / reconnect
class _FakeRepo:
    def __init__(self):
        self.meta: dict[str, str] = {}

    async def set_meta(self, key, value):
        self.meta[key] = value

    async def get_meta(self, key):
        return self.meta.get(key)


class _FakeBackfiller:
    def __init__(self):
        self.running = False
        self.calls: list = []

    async def target_conversations(self):
        return [("friend", 1), ("group", 2)]

    async def run_once(self, targets=None, reporter=None, force=False, since_ts=None):
        self.calls.append(targets)
        return 5


class _FakeService:
    def __init__(self):
        self.notes: list[str] = []

    async def notify_controller(self, text):
        self.notes.append(text)


def _offline_app(gap: float):
    cfg = MuzeConfig()
    cfg.ingest.backfill.reconnect_gap_sec = gap
    app = App(AccountSpec("main", "milky", MilkyEndpoint(), cfg), "config.yaml")
    app.repo = _FakeRepo()
    app.backfiller = _FakeBackfiller()
    app.service = _FakeService()
    return app


async def test_offline_then_reconnect_triggers_catchup():
    app = _offline_app(gap=0)  # any gap triggers immediately
    await app._on_bot_offline("kicked offline")
    assert app._offline_since is not None
    assert "last_offline_at" in app.repo.meta
    assert "kicked" in app.repo.meta.get("last_offline_reason", "")
    assert any("被下线" in n for n in app.service.notes)

    task = await app._on_bot_online()
    assert task is not None
    await task
    assert app.backfiller.calls == [[("friend", 1), ("group", 2)]]
    assert app._offline_since is None
    assert any("补齐完成" in n for n in app.service.notes)


async def test_reconnect_below_gap_threshold_does_not_catchup():
    app = _offline_app(gap=3600)
    await app._on_bot_offline("blip")
    assert await app._on_bot_online() is None
    assert app.backfiller.calls == []


async def test_first_connect_does_not_catchup():
    app = _offline_app(gap=0)
    assert await app._on_bot_online() is None  # no prior offline this process
    assert app.backfiller.calls == []


async def test_dispatch_routes_bot_offline():
    from muze.provider.milky.models import Event

    app = _offline_app(gap=0)
    await app.dispatch(
        Event.from_raw({"event_type": "bot_offline", "data": {"reason": "kicked"}})
    )
    assert app._offline_since is not None
