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
