"""ProgressReporter cadence + integration with backfill jobs."""

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.ingest.backfill import Backfiller
from muze.progress import ProgressReporter
from muze.store.db import open_db
from muze.store.repo import Repository

from test_backfill import CountingPipeline, FakeAdapter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "pg.db"))
    yield Repository(conn)
    await conn.close()


async def test_cadence_first_minutes_then_every_five():
    clock = FakeClock()
    sent: list[str] = []

    async def emit(text):
        sent.append(text)

    rep = ProgressReporter("任务", emit, now=clock)
    await rep.report_start("测试")
    assert sent[-1].startswith("▶") and "开始" in sent[-1]

    # before any milestone: no ticks
    clock.advance(30)
    assert await rep.maybe_tick() is False
    clock.advance(31)  # t=61
    assert await rep.maybe_tick() is True
    assert "1 分钟" in sent[-1] and "⏳" in sent[-1]

    clock.advance(60)  # t=121
    assert await rep.maybe_tick() is True
    clock.advance(60)  # t=181
    assert await rep.maybe_tick() is True

    # after the 3-minute mark: only every 5 minutes
    clock.advance(60)  # t=241
    assert await rep.maybe_tick() is False
    clock.advance(59)  # t=300 -> 5-minute point
    assert await rep.maybe_tick() is True
    assert await rep.maybe_tick() is False  # consumed
    clock.advance(299)  # t=599
    assert await rep.maybe_tick() is False
    clock.advance(1)  # t=600
    assert await rep.maybe_tick() is True

    rep.bump(new=42, pages=7)
    await rep.finish("完成")
    assert sent[-1].startswith("✔") and "42" in sent[-1] and "7 页" in sent[-1]


async def test_no_double_tick_between_points():
    clock = FakeClock()
    sent: list[str] = []

    async def emit(text):
        sent.append(text)

    rep = ProgressReporter("任务", emit, now=clock)
    clock.advance(120)  # two milestones passed -> only one tick per call
    assert await rep.maybe_tick() is True
    assert await rep.maybe_tick() is True  # second milestone still pending
    assert await rep.maybe_tick() is False
    assert len([s for s in sent if "⏳" in s]) == 2


async def test_deep_pull_reports_progress(repo):
    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    pipe = CountingPipeline(repo)
    bf = Backfiller(adapter, repo, MuzeConfig(), pipe, ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0

    clock = FakeClock()
    sent: list[str] = []

    async def emit(text):
        sent.append(text)

    rep = ProgressReporter("深拉测试", emit, now=clock)
    await rep.report_start("3 页")
    new = await bf.deep_pull("friend", 100, pages=10, reporter=rep)
    assert new == 90
    await rep.finish("完成")
    assert any("开始" in s for s in sent)
    assert sent[-1].startswith("✔") and "90" in sent[-1]
    assert rep.pages == 3 and rep.new_msgs == 90


async def test_run_once_reports_conversations(repo):
    pages1 = [{"start": 1, "seqs": [1, 2, 3], "next": None}]
    adapter = FakeAdapter({("friend", 100): pages1, ("friend", 200): pages1})
    pipe = CountingPipeline(repo)
    bf = Backfiller(adapter, repo, MuzeConfig(), pipe, ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    bf.cfg.ingest.backfill.conv_delay = 0
    bf.cfg.ingest.scope = "watchlist"
    bf.cfg.ingest.watchlist.friends = [100, 200]

    sent: list[str] = []

    async def emit(text):
        sent.append(text)

    rep = ProgressReporter("全量测试", emit)
    new = await bf.run_once([("friend", 100), ("friend", 200)], reporter=rep)
    await rep.finish("完成")
    assert new == 6 and rep.convs_done == 2
    assert sent[-1].startswith("✔") and "会话 2 个" in sent[-1]


async def test_run_once_skipped_returns_none(repo):
    """A concurrent call must signal 'skipped', not a fake 0-new success."""
    adapter = FakeAdapter({("friend", 100): [{"start": 1, "seqs": [1], "next": None}]})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.running = True
    assert await bf.run_once([("friend", 100)]) is None


async def test_reporter_abort_is_not_a_completion():
    sent: list[str] = []

    async def emit(text):
        sent.append(text)

    rep = ProgressReporter("全量补拉（强制全量）", emit)
    await rep.abort("已有补拉在运行，本次跳过")
    line = sent[-1]
    assert line.startswith("⚠") and "未执行" in line and "已有补拉" in line
    assert "✔" not in line and "共" not in line


async def test_reporter_broadcasts_to_all_subscribers():
    a: list[str] = []
    b: list[str] = []

    async def emit_a(text):
        a.append(text)

    async def emit_b(text):
        b.append(text)

    rep = ProgressReporter("任务", emit_a)
    rep.add_subscriber(emit_b)
    rep.add_subscriber(emit_b)  # dedupe
    await rep.report_start("x")
    await rep.finish("完成")
    assert len(rep._subs) == 2
    assert a == b and a and a[-1].startswith("✔")


async def test_reporter_keyed_subscriber_replaces_same_destination():
    a: list[str] = []
    b: list[str] = []
    c: list[str] = []

    async def emit_a(text):
        a.append(text)

    async def emit_b(text):
        b.append(text)

    async def emit_c(text):
        c.append(text)

    rep = ProgressReporter("任务", emit_a, key="conv:1")
    rep.add_subscriber(emit_b, key="conv:1")  # same conversation -> replace
    rep.add_subscriber(emit_c, key="conv:2")  # different -> keep both
    await rep.report_start("x")
    assert len(rep._subs) == 2
    assert not a and b and c  # conv:1 now goes to the newer emit only


async def test_backfiller_subscribe_false_when_idle(repo):
    bf = Backfiller(FakeAdapter({}), repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    assert bf.subscribe(lambda text: None) is False


async def test_run_once_owns_reporter_and_reports_finish(repo):
    """A background pull (no reporter passed) still owns a reporter, so a
    later /sync can subscribe and will receive the completion summary."""
    pages = [{"start": 1, "seqs": [1, 2, 3], "next": None}]
    adapter = FakeAdapter({("friend", 100): pages})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    bf.cfg.ingest.backfill.conv_delay = 0

    seen: list[str] = []
    captured: dict = {}
    orig = bf._backfill_conversation

    async def collect(text):
        seen.append(text)

    async def spy(scene, peer, reporter=None, force=False, since_ts=None):
        captured["subscribed"] = bf.subscribe(collect)
        return await orig(scene, peer, reporter=reporter, force=force)

    bf._backfill_conversation = spy
    new = await bf.run_once([("friend", 100)])
    assert new == 3
    assert captured["subscribed"] is True  # attachable while running
    assert bf.subscribe(lambda t: None) is False  # gone after finish
    assert any(s.startswith("✔") and "3" in s for s in seen)


async def test_sync_while_backfill_running_forwards_progress(repo):
    from muze.core.service import ControlContext, ControlService, Runtime

    class RunningBF:
        running = True

        def __init__(self):
            self.subs: list = []

        def subscribe(self, emit, key=None):
            self.subs.append((key, emit))
            return True

        def status(self):
            return {"running": True, "last_run_at": 0.0, "last_new_messages": 0}

    bf = RunningBF()
    runtime = Runtime()
    runtime.backfiller = bf
    svc = ControlService(MuzeConfig(), FakeAdapter({}), repo, ChatLog(), runtime)
    ctx = ControlContext(source="qq", chat_key="qq:friend:1", scene="friend", peer_id=1)
    out = await svc.run_command("/sync force", ctx)
    out2 = await svc.run_command("/sync force", ctx)  # repeated trigger
    assert not out.is_error and not out2.is_error and "转发" in out.text
    assert len(bf.subs) == 2
    # every subscription carries the conversation key, so the reporter can
    # dedupe instead of delivering twice to the same chat
    assert {k for k, _ in bf.subs} == {"qq:friend:1"}


async def test_sync_when_running_without_reporter_reports_error(repo):
    from muze.core.service import ControlContext, ControlService, Runtime

    class RunningBF:
        running = True

        def subscribe(self, emit, key=None):
            return False

        def status(self):
            return {"running": True, "last_run_at": 0.0, "last_new_messages": 0}

    runtime = Runtime()
    runtime.backfiller = RunningBF()
    svc = ControlService(MuzeConfig(), FakeAdapter({}), repo, ChatLog(), runtime)
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)
    out = await svc.run_command("/sync force", ctx)
    assert out.is_error and "已有补拉" in out.text


async def test_sync_duration_pull_ignores_watermark(repo):
    import asyncio

    from muze.core.service import ControlContext, ControlService, Runtime

    class FakeBF:
        running = False

        def __init__(self):
            self.calls: list[dict] = []

        async def run_once(self, targets=None, reporter=None, force=False, since_ts=None):
            self.calls.append({"targets": targets, "force": force, "since_ts": since_ts})
            return 3

        def status(self):
            return {}

    bf = FakeBF()
    runtime = Runtime()
    runtime.backfiller = bf
    svc = ControlService(MuzeConfig(), FakeAdapter({}), repo, ChatLog(), runtime)
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)

    out = await svc.run_command("sync 2d", ctx)
    assert not out.is_error and "最近 2 天" in out.text and "忽略水位线" in out.text
    await asyncio.sleep(0.05)  # let the background job run
    assert bf.calls and bf.calls[0]["since_ts"] is not None
    assert bf.calls[0]["force"] is False and bf.calls[0]["targets"] is None


async def test_sync_duration_targeted(repo):
    import asyncio

    from muze.core.service import ControlContext, ControlService, Runtime

    class FakeBF:
        running = False

        def __init__(self):
            self.calls: list[dict] = []

        async def run_once(self, targets=None, reporter=None, force=False, since_ts=None):
            self.calls.append({"targets": targets, "since_ts": since_ts})
            return 1

        def status(self):
            return {}

    bf = FakeBF()
    runtime = Runtime()
    runtime.backfiller = bf
    svc = ControlService(MuzeConfig(), FakeAdapter({}), repo, ChatLog(), runtime)
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)

    out = await svc.run_command("sync 12h group/9", ctx)
    assert not out.is_error and "最近 12 小时" in out.text
    await asyncio.sleep(0.05)
    assert bf.calls[0]["targets"] == [("group", 9)]
    assert bf.calls[0]["since_ts"] is not None
