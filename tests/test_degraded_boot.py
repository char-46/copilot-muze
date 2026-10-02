"""Degraded boot: protocol side down at startup no longer kills the process
(rc=3 + daemon backoff left NO console for a minute). A remembered uin lets
the account assemble its full local stack; a reconnect loop brings QQ back."""

import asyncio
from types import SimpleNamespace

import pytest

from muze.config import MuzeConfig
from muze.main import App
from muze.provider.base import ProtocolError
from muze.provider.milky.models import LoginInfo


class FlakyAdapter:
    """start() raises `fail_times` times, then logs in."""

    def __init__(self, uin=123456, fail_times=0):
        self.uin = uin
        self.fails_left = fail_times
        self.start_calls = 0
        self.caps = SimpleNamespace(summary_lines=lambda: [])

    async def start(self):
        self.start_calls += 1
        if self.fails_left > 0:
            self.fails_left -= 1
            raise ProtocolError("get_login_info: Cannot connect to host 127.0.0.1:3010")
        return LoginInfo(user_id=self.uin, user_name="test")

    async def close(self):
        pass


def make_app(tmp_path, name="t1") -> App:
    cfg = MuzeConfig()
    cfg.storage.data_dir = tmp_path / "data"
    spec = SimpleNamespace(name=name, config=cfg, endpoint=None)
    return App(spec, "config.yaml")


def test_remembered_uin_roundtrip(tmp_path):
    app = make_app(tmp_path)
    assert app._remembered_uin() is None  # nothing yet
    app._remember_uin(1000000001)
    app._remember_uin(1000000001)  # idempotent, no rewrite
    assert app._remembered_uin() == 1000000001
    # a second account has its own slot
    app2 = make_app(tmp_path, name="t2")
    assert app2._remembered_uin() is None
    app2._remember_uin(42)
    assert app._remembered_uin() == 1000000001 and app2._remembered_uin() == 42


async def test_setup_fatals_without_remembered_uin(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "muze.main.MilkyAdapter", lambda endpoint: FlakyAdapter(fail_times=99)
    )
    with pytest.raises(ProtocolError):
        await app.setup()  # first boot: genuinely fatal


async def test_setup_degraded_from_remembered_uin(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    app._remember_uin(1000000001)
    monkeypatch.setattr(
        "muze.main.MilkyAdapter", lambda endpoint: FlakyAdapter(fail_times=99)
    )
    await app.setup()
    assert app.degraded is True
    assert app.runtime.self_id == 1000000001
    assert app.account_dir.name == "1000000001"
    # the full local stack assembled: repo bound to the remembered owner
    assert await app.repo.get_meta("login_at") is None  # login not recorded (offline)
    await app.conn.close()


async def test_reconnect_loop_recovers_and_starts_backfill(tmp_path):
    app = make_app(tmp_path)
    app.RECONNECT_DELAY_START = 0.01
    app.degraded = True
    adapter = FlakyAdapter(uin=999, fail_times=2)
    app.adapter = adapter
    started = []

    async def fake_record_login():
        started.append("login")

    app._record_login = fake_record_login
    notified = []

    async def fake_notify(text):
        notified.append(text)

    app._notify = fake_notify

    async def fake_backfill_start():
        started.append("backfill")

    app.backfiller = SimpleNamespace(start_background=fake_backfill_start)

    await app._reconnect_loop()
    assert adapter.start_calls == 3  # 2 failures + 1 success
    assert app.degraded is False and app.runtime.self_id == 999
    assert app._remembered_uin() == 999  # persisted on recovery
    assert started == ["login", "backfill"]
    assert notified  # controller told the protocol side is back
