"""Media blob dedup: stickers, cross-account reuse, content-addressing."""

from pathlib import Path

import pytest

from muze.chatlog import plain_text
from muze.provider.milky.models import ImageSeg, LightAppSeg, MarketFaceSeg, RecordSeg, TextSeg
from muze.store.blobs import BlobStore
from muze.store.db import open_db
from muze.store.media import MediaStore
from muze.store.repo import Repository
from muze.store.sniff import sniff_container, sniff_ext

PAYLOAD = b"sticker-bytes-123"


class FakeResp:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._data = data
        self.status = status

    class _Content:
        def __init__(self, data: bytes) -> None:
            self._data = data

        async def iter_chunked(self, _n: int):
            yield self._data

    @property
    def content(self):
        return FakeResp._Content(self._data)


class FakeCtx:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._resp = FakeResp(data, status)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeHttp:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self.data = data
        self.status = status
        self.count = 0

    async def session(self):
        return self

    def get(self, url: str):
        self.count += 1
        return FakeCtx(self.data, self.status)


class FakeAdapter:
    def __init__(self, data: bytes = PAYLOAD, status: int = 200) -> None:
        self.caps = type("C", (), {"incoming_file_segment": True})()
        self.http = FakeHttp(data, status)

    async def get_resource_url(self, resource_id: str) -> str:
        return f"http://fake/{resource_id}"


async def make_store(root: Path, data: bytes = PAYLOAD, status: int = 200):
    blobs = BlobStore(root / "blobs")
    conn = await open_db(":memory:")
    repo = Repository(conn)
    store = MediaStore(blobs, FakeAdapter(data, status), repo)
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


async def test_media_404_marks_gone(tmp_path):
    """Expired roaming media (HTTP 404) is parked as 'gone', not retried."""
    store, repo, adapter = await make_store(tmp_path, status=404)
    img = ImageSeg(resource_id="r404", summary="old")
    mid = await store.register("group", 7, 1, 0, img, msg_time=1700000000)
    row = await repo.media_by_id(mid)
    upd = await store.download_row(row)
    assert upd["status"] == "gone"
    # not offered for retry, but counted in stats
    assert await repo.media_pending(10) == []
    stats = await repo.media_stats()
    assert stats["gone"] == 1 and stats["failed"] == 0
    await close(store)


async def test_retire_gone_media_retires_historical_failures(tmp_path):
    """Rows already failed with a 404 error are swept to 'gone' once."""
    store, repo, adapter = await make_store(tmp_path)
    img = ImageSeg(resource_id="r1", summary="x")
    mid = await store.register("friend", 3, 1, 0, img)
    await repo.update_media(mid, status="failed", error="HTTP 404")
    assert len(await repo.media_pending(10)) == 1  # would be retried before
    retired = await repo.retire_gone_media()
    assert retired == 1
    assert await repo.media_pending(10) == []  # gone rows are never retried
    row = await repo.media_by_id(mid)
    assert row["status"] == "gone"
    # idempotent: nothing left to retire
    assert await repo.retire_gone_media() == 0
    await close(store)


# --------------------------------------------------------------- sniffing
def test_sniff_container_and_ext():
    assert sniff_container(b"#!SILK_V3\x0c\x00") == "silk"
    assert sniff_container(b"\x02#!SILK_V3\x00") == "silk"  # Tencent prefix
    assert sniff_container(b"#!AMR\n...") == "amr"
    assert sniff_container(b"ID3\x04\x00") == "mp3"
    assert sniff_container(b"\xff\xfb\x90\x00") == "mp3"
    assert sniff_container(b"OggS\x00\x02") == "ogg"
    assert sniff_container(b"RIFF\x00\x00\x00\x00WAVE") == "wav"
    assert sniff_container(b"\x89PNG\r\n\x1a\n") == "png"
    assert sniff_container(b"\xff\xd8\xff\xe0") == "jpg"
    assert sniff_container(b"GIF89a") == "gif"
    assert sniff_container(b"\x00\x00\x00\x18ftypmp42") == "m4a"
    assert sniff_container(b"%PDF-1.7") == "pdf"
    assert sniff_container(b"PK\x03\x04") == "zip"
    assert sniff_container(b"not media at all") is None
    assert sniff_ext(b"#!SILK_V3") == ".silk"
    assert sniff_ext(b"\xff\xd8\xff") == ".jpg"
    assert sniff_ext(b"plain text") is None


def test_ext_priority_file_name_wins_for_files(tmp_path):
    store = MediaStore(BlobStore(tmp_path / "blobs"), FakeAdapter(), None)
    # a docx is really a zip: the user-facing name must win for kind=file
    row = {"kind": "file", "file_name": "report.docx"}
    assert store._ext_for(row, "http://x/y", b"PK\x03\x04") == ".docx"
    # an image named .jpg but actually PNG: bytes win
    row = {"kind": "image", "file_name": "photo.jpg"}
    assert store._ext_for(row, "http://x/y", b"\x89PNG\r\n\x1a\n") == ".png"
    # nothing known: fall back to the kind default
    row = {"kind": "record", "file_name": ""}
    assert store._ext_for(row, "http://x/y", b"random") == ".amr"


async def test_voice_silk_stored_as_playable_wav(tmp_path):
    pilk = pytest.importorskip("pilk")
    # build a tiny (silent) SILK clip the way the protocol side would deliver it
    pcm = (b"\x00\x00") * 4800  # 0.2s @ 24kHz mono s16
    (tmp_path / "t.pcm").write_bytes(pcm)
    pilk.encode(str(tmp_path / "t.pcm"), str(tmp_path / "t.silk"), pcm_rate=24000)
    silk_bytes = (tmp_path / "t.silk").read_bytes()
    assert silk_bytes[:9] == b"#!SILK_V3"

    store, repo, adapter = await make_store(tmp_path, data=silk_bytes)
    rec = RecordSeg(resource_id="v1", duration=1)
    mid = await store.register("friend", 5, 1, 0, rec)
    row = await repo.media_by_id(mid)
    upd = await store.download_row(row, prefer_url="http://fake/v1")

    assert upd["status"] == "done"
    playable = store.abs_path(upd)
    assert playable is not None and playable.suffix == ".wav" and playable.exists()
    assert playable.read_bytes()[:4] == b"RIFF"  # real WAV container
    # the original silk is preserved alongside the playable wav
    silk_files = list((tmp_path / "blobs" / "record").rglob("*.silk"))
    assert len(silk_files) == 1
    await close(store)


async def test_voice_amr_stays_amr(tmp_path):
    store, repo, adapter = await make_store(tmp_path, data=b"#!AMR\n\x00\x01\x02")
    rec = RecordSeg(resource_id="a1", duration=2)
    mid = await store.register("friend", 6, 1, 0, rec)
    row = await repo.media_by_id(mid)
    upd = await store.download_row(row, prefer_url="http://fake/a1")
    assert upd["status"] == "done"
    amr = store.abs_path(upd)
    assert amr is not None and amr.suffix == ".amr"
    await close(store)
