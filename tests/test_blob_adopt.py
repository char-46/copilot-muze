"""Hook protocol sides (LLBot & co.) sometimes answer the media url APIs
with the QQ client's OWN cache file (local path / file:// URI) instead of an
expiring rkey URL. Adopt those into the blob store: hardlink preferred,
copy fallback, never a bare pointer into the client's cache."""

import pytest

from muze.store.blobs import BlobStore
from muze.store.db import open_db
from muze.store.media import MediaStore, _local_path, to_file_uri
from muze.store.repo import Repository

from test_permissions import Recorder

JPEG = b"\xff\xd8\xff\xe0" + b"q" * 100


def test_local_path_detection():
    from pathlib import Path

    assert _local_path("https://example.com/a.jpg?sig=1") is None
    assert _local_path("http://x/y") is None
    assert _local_path("") is None
    assert _local_path("plainrelative.jpg") is None
    assert _local_path("file:///C:/QQ/nt_cache/abc.jpg") == Path("C:/QQ/nt_cache/abc.jpg")
    assert _local_path("file://server/share/a.jpg") is None  # remote host: skip
    assert _local_path("C:\\QQ\\nt\\abc.png") == Path("C:\\QQ\\nt\\abc.png")
    assert _local_path("file:///C:/dir%20name/x%20y.mp4") == Path("C:/dir name/x y.mp4")


@pytest.fixture
async def ms(tmp_path):
    conn = await open_db(str(tmp_path / "b.db"))
    repo = Repository(conn)
    blobs = BlobStore(tmp_path / "blobs")
    store = MediaStore(blobs, Recorder(), repo)
    yield store, tmp_path
    await conn.close()


async def test_fetch_adopts_local_blob_hardlink(ms):
    store, tmp_path = ms
    src = tmp_path / "qqcache" / "abc.jpg"
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(JPEG)
    row = {"id": 1, "kind": "image", "file_name": "", "file_id": "", "file_hash": ""}

    h, size, dest = await store._fetch_into_blob(to_file_uri(src), row)
    assert size == len(JPEG) and dest.exists()
    assert dest.read_bytes() == JPEG
    assert dest.suffix == ".jpg"  # sniffed from magic bytes
    import os

    assert os.path.samefile(src, dest)  # hardlink (same volume)
    # the adopted blob survives the client "cleaning" its cache copy
    src.unlink()
    assert dest.exists() and dest.read_bytes() == JPEG


async def test_fetch_adopts_bare_windows_path_and_copies_when_link_denied(ms, monkeypatch):
    store, tmp_path = ms
    src = tmp_path / "cache2" / "b.jpg"
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(JPEG)
    row = {"id": 2, "kind": "image", "file_name": "", "file_id": "", "file_hash": ""}

    import os

    def deny(*a, **k):
        raise OSError("cross-volume")

    monkeypatch.setattr(os, "link", deny)
    _h, size, dest = await store._fetch_into_blob(str(src), row)  # bare path, no file://
    assert dest.exists() and dest.read_bytes() == JPEG and size == len(JPEG)


async def test_fetch_missing_local_source_raises(ms):
    store, tmp_path = ms
    row = {"id": 3, "kind": "image", "file_name": "", "file_id": "", "file_hash": ""}
    with pytest.raises(RuntimeError):
        await store._fetch_into_blob(str(tmp_path / "nope.jpg"), row)


async def test_download_row_full_path_with_local_url(tmp_path):
    """get_resource_temp_url answering a client-cache path end to end."""

    class LocalUrlAdapter(Recorder):
        def __init__(self, src):
            super().__init__()
            self.src = src

        async def get_resource_url(self, resource_id):
            return to_file_uri(self.src)

    conn = await open_db(str(tmp_path / "c.db"))
    repo = Repository(conn)
    blobs = BlobStore(tmp_path / "blobs")
    src = tmp_path / "nt" / "pic.jpg"
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(JPEG)
    store = MediaStore(blobs, LocalUrlAdapter(src), repo)
    now = 1758000000
    mid = await repo.add_media(
        {
            "scene": "group", "peer_id": 1, "message_seq": 9, "seg_index": 0,
            "kind": "image", "resource_id": "res9", "file_id": "", "file_name": "",
            "file_hash": "", "width": None, "height": None, "duration": None,
            "summary": "", "status": "pending", "time": now,
            "created_at": now, "updated_at": now,
        }
    )
    row = await repo.media_by_id(mid)
    out = await store.download_row(row)
    assert out["status"] == "done"
    assert out["file_path"].startswith(str(tmp_path / "blobs"))
    from pathlib import Path as _P

    assert _P(out["file_path"]).read_bytes() == JPEG
    await conn.close()
