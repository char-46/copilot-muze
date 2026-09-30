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
