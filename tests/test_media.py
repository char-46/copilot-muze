"""Media blob dedup: stickers, cross-account reuse, content-addressing."""

from pathlib import Path

import pytest

from muze.chatlog import plain_text
from muze.provider.milky.models import ImageSeg, LightAppSeg, MarketFaceSeg, TextSeg
from muze.store.blobs import BlobStore
from muze.store.db import open_db
from muze.store.media import MediaStore
from muze.store.repo import Repository

PAYLOAD = b"sticker-bytes-123"


class FakeResp:
    status = 200

    def __init__(self, data: bytes) -> None:
        self._data = data

    class _Content:
        def __init__(self, data: bytes) -> None:
            self._data = data

        async def iter_chunked(self, _n: int):
            yield self._data

    @property
    def content(self):
        return FakeResp._Content(self._data)


class FakeCtx:
    def __init__(self, data: bytes) -> None:
        self._resp = FakeResp(data)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeHttp:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.count = 0

    async def session(self):
        return self

    def get(self, url: str):
        self.count += 1
        return FakeCtx(self.data)


class FakeAdapter:
    def __init__(self, data: bytes = PAYLOAD) -> None:
        self.caps = type("C", (), {"incoming_file_segment": True})()
        self.http = FakeHttp(data)

    async def get_resource_url(self, resource_id: str) -> str:
        return f"http://fake/{resource_id}"


async def make_store(root: Path, data: bytes = PAYLOAD):
    blobs = BlobStore(root / "blobs")
    conn = await open_db(":memory:")
    repo = Repository(conn)
    store = MediaStore(blobs, FakeAdapter(data), repo)
    return store, repo, store.adapter


async def close(store: MediaStore) -> None:
    await store.repo.conn.close()
    await store.blobs.close()


async def test_face_dedup_same_account(tmp_path):
    store, repo, adapter = await make_store(tmp_path)
    face = MarketFaceSeg(emoji_id="EMO1", summary="开心", url="http://fake/face1")

    mid1 = await store.register("group", 1, 100, 0, face, msg_time=1700000100)
    assert mid1 is not None  # first sighting needs a download
    row = await repo.media_by_id(mid1)
    assert row["time"] == 1700000100  # message timestamp stored on the media row
    updated = await store.download_row(row, prefer_url=face.url)
    assert updated["status"] == "done"
    assert adapter.http.count == 1

    # same sticker again in another message: zero downloads, row arrives done
    mid2 = await store.register("group", 2, 200, 0, face, msg_time=1700000200)
    assert mid2 is None
    rows = await repo.media_for_message("group", 2, 200)
    assert len(rows) == 1 and rows[0]["status"] == "done"
    assert rows[0]["file_path"] == updated["file_path"]
    assert rows[0]["time"] == 1700000200  # per-message timestamp kept
    assert adapter.http.count == 1  # no extra fetch
    await close(store)


async def test_face_dedup_cross_account(tmp_path):
    store_a, repo_a, adapter_a = await make_store(tmp_path)
    face = MarketFaceSeg(emoji_id="EMO9", summary="笑", url="http://fake/face9")
    mid = await store_a.register("friend", 10, 1, 0, face)
    row = await repo_a.media_by_id(mid)
    done_a = await store_a.download_row(row, prefer_url=face.url)

    # account B: different db, SAME shared blob store
    blobs = BlobStore(tmp_path / "blobs")
    conn_b = await open_db(":memory:")
    store_b = MediaStore(blobs, FakeAdapter(), Repository(conn_b))
    assert await store_b.register("group", 99, 5, 0, face) is None
    rows = await store_b.repo.media_for_message("group", 99, 5)
    assert rows[0]["status"] == "done"
    assert rows[0]["file_path"] == done_a["file_path"]
    assert store_b.adapter.http.count == 0  # nothing fetched for account B
    await close(store_a)
    await close(store_b)


async def test_same_content_image_single_blob(tmp_path):
    store, repo, adapter = await make_store(tmp_path)
    img1 = ImageSeg(resource_id="r1", summary="same pic")
    img2 = ImageSeg(resource_id="r2", summary="same pic again")

    for seq, img in ((1, img1), (2, img2)):
        mid = await store.register("friend", 5, seq, 0, img)
        row = await repo.media_by_id(mid)
        updated = await store.download_row(row)
        assert updated["status"] == "done"

    # two fetches (unknown hash beforehand) but ONE blob on disk
    assert adapter.http.count == 2
    blobs_root = tmp_path / "blobs" / "image"
    files = [p for p in blobs_root.rglob("*") if p.is_file()]
    assert len(files) == 1
    rows1 = await repo.media_for_message("friend", 5, 1)
    rows2 = await repo.media_for_message("friend", 5, 2)
    assert rows1[0]["content_hash"] == rows2[0]["content_hash"]
    assert rows1[0]["file_path"] == rows2[0]["file_path"]
    await close(store)


def test_light_app_text_extraction():
    seg = LightAppSeg(
        app_name="哔哩哔哩",
        json_payload=(
            '{"meta_data":{"title":"【视频】标题在此","desc":"简介文字","jump_url":"https://b23.tv/xyz"}}'
        ),
    )
    text = plain_text([seg])
    assert "哔哩哔哩" in text and "标题在此" in text and "b23.tv" in text

    bad = LightAppSeg(app_name="x", json_payload="not json")
    assert plain_text([bad]) == "[小程序:x]"
    assert plain_text([TextSeg(text="hi"), bad]) == "hi [小程序:x]"


async def test_blob_lookup_invalidated_by_deleted_file(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    await blobs.record("face:GONE", "ab" * 32, str(tmp_path / "nope.gif"), 5, "face")
    assert await blobs.lookup("face:GONE") is None  # file missing -> miss
    p = tmp_path / "yes.gif"
    p.write_bytes(b"z")
    await blobs.record("face:HERE", "cd" * 32, str(p), 1, "face")
    hit = await blobs.lookup("face:HERE")
    assert hit and hit["content_hash"] == "cd" * 32
    await blobs.close()
