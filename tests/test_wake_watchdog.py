"""Wake watchdog: the machine may SUSPEND while muze keeps running (frozen
mid-air, never exits). On resume the websocket reconnects within seconds so
the bot_online gap accounting sees nothing worth catching up, and the 30-min
rescan only targets conversations active within 24h PER THE LOCAL DB — a
long sleep ages those out entirely. Live incident 2026-10-18..21 (box slept
2.5 days). The watchdog detects the wall-clock jump and pulls missed
messages itself."""

import asyncio
import types

from muze.main import App


class FakeClock:
    def __init__(self, ticks, step=0.05):
        self.ticks = list(ticks)
        self.last = ticks[-1]
        self.step = step

    def __call__(self):
        if self.ticks:
            self.last = self.ticks.pop(0)
        else:
            self.last += self.step
        return self.last


class Stub:
    """Borrow App._wake_watchdog unbound; only needs the attributes it touches."""

    _wake_watchdog = App._wake_watchdog
    WAKE_MIN_GAP_SEC = 0.5

    def __init__(self):
        self.spec = types.SimpleNamespace(name="test")
        self.check_sec = 0.05
        self.catchups: list[float] = []
        self.notifications: list[str] = []

    @property
    def WAKE_CHECK_SEC(self):
        return self.check_sec

    async def _notify(self, text):
        self.notifications.append(text)

    async def _catch_up(self, gap):
        self.catchups.append(gap)


async def test_suspend_jump_triggers_catchup(monkeypatch):
    s = Stub()
    # ticks: init; normal tick; then a tick 600s "late" (suspend); then quiet
    clock = FakeClock([1000.0, 1000.05, 1600.10, 1600.15, 1600.20])
    monkeypatch.setattr("muze.main.time.time", clock)
    task = asyncio.create_task(s._wake_watchdog())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(s.catchups) == 1
    assert 599 < s.catchups[0] < 601
    assert any("休眠" in n for n in s.notifications)


async def test_normal_ticks_do_not_trigger(monkeypatch):
    s = Stub()
    clock = FakeClock([2000.0 + i * 0.05 for i in range(12)])
    monkeypatch.setattr("muze.main.time.time", clock)
    task = asyncio.create_task(s._wake_watchdog())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert s.catchups == [] and s.notifications == []


async def test_catchup_error_does_not_kill_watchdog(monkeypatch):
    s = Stub()

    async def boom(gap):
        s.catchups.append(gap)
        raise RuntimeError("roaming down")

    s._catch_up = boom
    clock = FakeClock([3000.0, 3000.05, 3700.10, 3700.15, 4400.20, 4400.25])
    monkeypatch.setattr("muze.main.time.time", clock)
    task = asyncio.create_task(s._wake_watchdog())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # survived the first failure and caught the second jump
    assert len(s.catchups) == 2
