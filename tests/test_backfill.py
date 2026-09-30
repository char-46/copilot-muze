"""Backfill stop-condition tests against a scripted fake adapter."""

from __future__ import annotations

import pytest

from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.ingest.backfill import Backfiller
from muze.provider.base import Caps, ProtocolAdapter
from muze.provider.milky.models import HistoryResult, IncomingMessage
from muze.store.db import open_db
from muze.store.repo import Repository


def make_msg(scene, peer, seq, sender=7):
    return IncomingMessage.from_raw(
        {
            "message_scene": scene,
            "peer_id": peer,
            "message_seq": seq,
            "sender_id": sender,
            "time": 1759000000 + seq,
            "segments": [{"type": "text", "data": {"text": f"m{seq}"}}],
        }
    )


class FakeAdapter(ProtocolAdapter):
    def __init__(self, pages_by_conv: dict, friends=None, groups=None):
        self.caps = Caps(
            friend_history=True,
            group_history=True,
            history_start_seq=True,
            incoming_file_segment=True,
            forwarded_messages=True,
            mark_read=True,
            resource_temp_url=True,
            impl_name="fake",
        )
        self.pages = {k: list(v) for k, v in pages_by_conv.items()}
        self.calls: list[tuple[str, int, int | None]] = []
        self.friends = friends or []
        self.groups = groups or []

    async def start(self):
        raise NotImplementedError

    async def close(self):
        pass

    def events(self):
        raise NotImplementedError

    async def get_login_info(self):
        raise NotImplementedError

    async def get_friend_list(self):
        return self.friends

    async def get_group_list(self):
        return self.groups

    async def get_group_member_name(self, group_id, user_id):
        return "member"

    async def send_private_message(self, user_id, segments):
        raise NotImplementedError

    async def send_group_message(self, group_id, segments):
        raise NotImplementedError

    async def get_history_messages(self, scene, peer_id, *, start_message_seq=None, limit=30):
        self.calls.append((scene, peer_id, start_message_seq))
        pages = self.pages.get((scene, peer_id))
        if not pages:
            return HistoryResult(messages=[], next_message_seq=None)
        # Pick the page that starts at start_message_seq (or the newest).
        if start_message_seq is None:
            page = max(pages, key=lambda p: p["start"])  # newest page
        else:
            page = next((p for p in pages if p["start"] == start_message_seq), None)
            if page is None:
                return HistoryResult(messages=[], next_message_seq=None)
        pages.remove(page)
        msgs = [make_msg(scene, peer_id, s) for s in page["seqs"]]
        return HistoryResult(messages=msgs, next_message_seq=page.get("next"))

    async def get_message(self, scene, peer_id, message_seq):
        return None

    async def get_resource_url(self, resource_id):
        return "http://example/" + resource_id

    async def get_private_file_url(self, user_id, file_id, file_hash):
        return "http://example/f"

    async def get_group_file_url(self, group_id, file_id):
        return "http://example/f"


class CountingPipeline:
    def __init__(self, repo=None):
        self.repo = repo
        self.seen: list[int] = []

    async def handle_message(self, msg, source="event"):
        self.seen.append(msg.message_seq)
        if self.repo is not None:
            from muze.chatlog import plain_text

            return await self.repo.upsert_message(msg, plain_text(msg.segments), source)
        return True


@pytest.fixture
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "bf.db"))
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def test_backfill_stops_when_watermark_covered(repo):
    # Pre-existing archive up to seq 50.
    await repo.upsert_message(make_msg("friend", 100, 50), "m50", "event")

    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 61 - 30},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    pipe = CountingPipeline(repo)
    bf = Backfiller(adapter, repo, MuzeConfig(), pipe, ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0

    new = await bf.run_once([("friend", 100)])
    # 61..90 (30) + 51..60 (10) are new; page 31..50 <= watermark not ingested;
    # page 1..30 never fetched (stopped when oldest <= watermark).
    assert new == 40, f"expected 40 new, got {new}"
    assert sorted(pipe.seen) == sorted(list(range(61, 91)) + list(range(51, 61)))
    assert await repo.get_watermark("friend", 100) == 90


async def test_backfill_incremental_after_new_messages(repo):
    for seq in range(1, 91):
        await repo.upsert_message(make_msg("friend", 100, seq), f"m{seq}", "event")

    pages = [{"start": 91, "seqs": [91, 92, 93, 94, 95], "next": 60}]
    adapter = FakeAdapter({("friend", 100): pages})
    pipe = CountingPipeline(repo)
    bf = Backfiller(adapter, repo, MuzeConfig(), pipe, ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0

    new = await bf.run_once([("friend", 100)])
    assert new == 5
    assert await repo.get_watermark("friend", 100) == 95


async def test_backfill_respects_max_pages(repo):
    # Endless pages that never touch the watermark.
    pages = [
        {"start": 200, "seqs": list(range(181, 211)), "next": 180},
        {"start": 180, "seqs": list(range(151, 181)), "next": 150},
        {"start": 150, "seqs": list(range(121, 151)), "next": 120},
        {"start": 120, "seqs": list(range(91, 121)), "next": 90},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    pipe = CountingPipeline(repo)
    bf = Backfiller(adapter, repo, MuzeConfig(), pipe, ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    bf.cfg.ingest.backfill.max_pages_per_conv = 2

    new = await bf.run_once([("friend", 100)])
    assert new == 60  # two pages of 30
    assert len(adapter.calls) == 2


async def test_backfill_force_pulls_everything(repo):
    """force: ignore the watermark, page to the roaming end; already-stored
    messages are upsert-deduped so only genuinely missing ones count."""
    # Pre-existing archive up to seq 50 (same as the watermark test).
    for seq in range(1, 51):
        await repo.upsert_message(make_msg("friend", 100, seq), f"m{seq}", "event")
    # ...but seq 20 went missing historically; force should recover it.
    await repo.conn.execute("DELETE FROM messages WHERE message_seq = 20")
    await repo.conn.commit()

    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    pipe = CountingPipeline(repo)
    bf = Backfiller(adapter, repo, MuzeConfig(), pipe, ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0

    new = await bf.run_once([("friend", 100)], force=True)
    # 61..90 are new (30) + 51..60 (10) + the missing seq 20 recovered;
    # 31..50 & 21..30 deduped by the primary key; all 3 pages walked.
    assert new == 41, f"expected 41 new (30 + 10 + recovered 20), got {new}"
    assert len(adapter.calls) == 3  # walked to the end, no watermark stop
    assert await repo.get_message_row("friend", 100, 20) is not None
    assert await repo.get_watermark("friend", 100) == 90


async def test_probe_coverage(repo):
    # full roaming: pages down to seq 1, end reached
    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    probe = await bf.probe_coverage("friend", 100, pages=10)
    assert probe["oldest_seq"] == 1 and probe["reached_end"] is True
    assert probe["seen"] == 90 and probe["paged"] is True
    # nothing stored by a probe
    assert (await repo.coverage_stats("friend", 100)).get("n") in (None, 0)


async def test_deep_pull_ignores_watermark(repo):
    # watermark already at 90; deep pull still fetches the older pages
    for seq in range(61, 91):
        await repo.upsert_message(make_msg("friend", 100, seq), f"m{seq}", "event")
    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    pipe = CountingPipeline(repo)
    bf = Backfiller(adapter, repo, MuzeConfig(), pipe, ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0

    new = await bf.deep_pull("friend", 100, pages=10)
    assert new == 60  # 31..90 pulled despite watermark at 90
    stats = await repo.coverage_stats("friend", 100)
    assert stats["n"] == 90 and stats["min_seq"] == 1


# ---------------------------------------------- multi-watermark resume (v8)
async def _msg_count(repo, scene, peer):
    async with repo.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE scene=? AND peer_id=?", (scene, peer)
    ) as cur:
        return (await cur.fetchone())[0]


async def test_backfill_resumes_interrupted_pull(repo):
    """A page-capped pull persists cursor/target; the next run continues the
    hole instead of stopping at the raised last_seq (= the old bug)."""
    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    bf.cfg.ingest.backfill.max_pages_per_conv = 1

    await bf.run_once([("friend", 100)])
    assert await repo.get_watermark("friend", 100) == 90  # newest page stored
    assert await _msg_count(repo, "friend", 100) == 30    # only page 1
    state = await repo.get_bf_state("friend", 100)
    assert state.get("cursor") == 31 and state.get("target") == 0
    assert ("friend", 100) in await repo.pending_bf_conversations()

    # "restart": pages are re-served by the fake; the resume must go DOWN
    bf.cfg.ingest.backfill.max_pages_per_conv = 10
    adapter2 = FakeAdapter(
        {("friend", 100): [
            {"start": 31, "seqs": list(range(31, 61)), "next": 1},
            {"start": 1, "seqs": list(range(1, 31)), "next": None},
        ]}
    )
    bf.adapter = adapter2
    await bf.run_once([("friend", 100)])
    assert await _msg_count(repo, "friend", 100) == 90  # hole filled
    assert await repo.get_bf_state("friend", 100) == {}  # state cleared
    assert ("friend", 100) not in await repo.pending_bf_conversations()


async def test_backfill_clears_state_on_completion(repo):
    pages = [{"start": 1, "seqs": [1, 2, 3], "next": None}]
    adapter = FakeAdapter({("friend", 100): pages})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    await bf.run_once([("friend", 100)])
    assert await repo.get_bf_state("friend", 100) == {}
    assert await repo.pending_bf_conversations() == []


async def test_force_cap_leaves_resumable_full_pull(repo):
    """A page-capped force pull records target=0 so the next run continues."""
    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    bf.cfg.ingest.backfill.max_pages_per_conv = 1
    await bf.run_once([("friend", 100)], force=True)
    state = await repo.get_bf_state("friend", 100)
    assert state.get("cursor") == 31 and state.get("target") == 0


async def test_meta_roundtrip(repo):
    assert await repo.get_meta("login_at") is None
    await repo.set_meta("login_at", "1700000000")
    await repo.set_meta("login_at", "1700000001")  # upsert
    assert await repo.get_meta("login_at") == "1700000001"


async def test_bf_only_row_hidden_from_lists(repo):
    """A conversation row existing solely for resume state must not show up
    in /unread or inflate stats — but rescan still sees it as pending."""
    await repo.set_bf_state("friend", 5, cursor=10, target=0)
    assert all(c["peer_id"] != 5 for c in await repo.list_conversations())
    assert (await repo.stats())["conversations"] == 0
    assert ("friend", 5) in await repo.pending_bf_conversations()
    await repo.clear_bf_state("friend", 5)
    assert await repo.pending_bf_conversations() == []


async def test_migration_v7_to_v9_adds_columns(tmp_path):
    import aiosqlite

    path = str(tmp_path / "old.db")
    conn = await aiosqlite.connect(path)
    await conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE conversations (
            scene TEXT NOT NULL, peer_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '', last_seq INTEGER NOT NULL DEFAULT 0,
            last_active_at INTEGER, PRIMARY KEY (scene, peer_id));
        INSERT INTO meta (key, value) VALUES ('schema_version', '7');
        """
    )
    await conn.commit()
    await conn.close()

    from muze.store.db import open_db

    conn = await open_db(path)
    async with conn.execute("PRAGMA table_info(conversations)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert {
        "bf_cursor_seq", "bf_target_seq", "bf_since_ts", "bf_last_run_at", "last_op_at",
    } <= cols
    async with conn.execute("SELECT value FROM meta WHERE key='schema_version'") as cur:
        assert (await cur.fetchone())[0] == "9"
    await conn.close()


# ------------------------------------------- time-window pull (/sync 2d)
async def test_time_window_pull_ignores_watermark(repo):
    """`/sync 2d`-style: re-pull the window regardless of last_seq (dedup)."""
    for seq in range(61, 91):  # already archived -> watermark 90
        await repo.upsert_message(make_msg("friend", 100, seq), f"m{seq}", "event")
    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    # make_msg time = 1759000000 + seq, so cutoff at +41 keeps seqs 41..90
    since = 1759000000 + 41
    await bf.run_once([("friend", 100)], since_ts=since)

    stats = await repo.coverage_stats("friend", 100)
    assert stats["n"] == 50           # window 41..90; 61..90 pre-existing
    assert stats["min_seq"] == 41     # nothing older than the window stored
    assert await repo.get_bf_state("friend", 100) == {}


async def test_time_window_pull_resumes_after_cap(repo):
    pages = [
        {"start": 61, "seqs": list(range(61, 91)), "next": 31},
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]
    adapter = FakeAdapter({("friend", 100): pages})
    bf = Backfiller(adapter, repo, MuzeConfig(), CountingPipeline(repo), ChatLog())
    bf.cfg.ingest.backfill.page_delay = 0
    bf.cfg.ingest.backfill.max_pages_per_conv = 1
    since = 1759000000 + 41
    await bf.run_once([("friend", 100)], since_ts=since)

    state = await repo.get_bf_state("friend", 100)
    assert state.get("since") == since and state.get("cursor") == 31

    bf.cfg.ingest.backfill.max_pages_per_conv = 10
    bf.adapter = FakeAdapter({("friend", 100): [
        {"start": 31, "seqs": list(range(31, 61)), "next": 1},
        {"start": 1, "seqs": list(range(1, 31)), "next": None},
    ]})
    await bf.run_once([("friend", 100)])  # resumes the window pull
    assert (await repo.coverage_stats("friend", 100))["min_seq"] == 41
    assert await repo.get_bf_state("friend", 100) == {}
