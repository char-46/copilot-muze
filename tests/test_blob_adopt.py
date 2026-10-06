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

    h, size, dest, _md5 = await store._fetch_into_blob(to_file_uri(src), row)
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
    _h, size, dest, _md5 = await store._fetch_into_blob(str(src), row)  # bare path, no file://
    assert dest.exists() and dest.read_bytes() == JPEG and size == len(JPEG)


async def test_fetch_missing_local_source_raises(ms):
    store, tmp_path = ms
    row = {"id": 3, "kind": "image", "file_name": "", "file_id": "", "file_hash": ""}
    with pytest.raises(RuntimeError):
        await store._fetch_into_blob(str(tmp_path / "nope.jpg"), row)


async def test_failed_download_leaves_no_partial_behind(ms):
    """A download that dies mid-stream must clean its .part up — orphaned
    partials of huge group files accumulated to 118GB before this rule."""
    store, tmp_path = ms
    row = {"id": 4, "kind": "video", "file_name": "", "file_id": "", "file_hash": ""}

    class _Body:
        @staticmethod
        async def iter_chunked(_n):
            yield b"x" * 4096
            raise RuntimeError("connection dropped")

    class _Resp:
        status = 200
        content = _Body()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Sess:
        @staticmethod
        def get(_url):
            return _Resp()

    class _Http:
        @staticmethod
        async def session():
            return _Sess()

    store.adapter.http = _Http()
    with pytest.raises(RuntimeError):
        await store._fetch_into_blob("https://example.com/v.mp4", row)
    leftovers = list((tmp_path / "blobs" / "tmp").glob("*"))
    assert leftovers == []


def test_sweep_tmp_removes_only_stale_partials(tmp_path):
    import os

    blobs = BlobStore(tmp_path / "b")
    old = blobs.tmp_path()
    old.write_bytes(b"o" * 100)
    fresh = blobs.tmp_path()
    fresh.write_bytes(b"n" * 5)
    os.utime(old, (0, 0))  # epoch: ancient

    files, nbytes = blobs.sweep_tmp(3600)
    assert (files, nbytes) == (1, 100)
    assert not old.exists() and fresh.exists()

    # second sweep is a no-op (fresh file under the age threshold)
    assert blobs.sweep_tmp(3600) == (0, 0)


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
    # stored path is store-RELATIVE now; the addressing root resolves it
    assert out["file_path"].startswith("image/")
    assert blobs.resolve(out["file_path"]).read_bytes() == JPEG
    await conn.close()


# ------------------------------------------------- named links + qq cache
def test_named_link_hardlink_and_collision(tmp_path):
    from muze.store.blobs import safe_name

    assert safe_name("报表 2026.xlsx", "f") != ""
    blobs = BlobStore(tmp_path / "b")
    a = tmp_path / "a.bin"
    a.write_bytes(b"AAA")
    n1 = blobs.named_link(a, "Report Final!.docx")
    assert n1 is not None and n1.name == "Report_Final_.docx"
    import os

    assert os.path.samefile(a, n1)  # hardlink -> blob
    assert blobs.named_link(a, "Report Final!.docx") == n1  # idempotent
    b = tmp_path / "b.bin"
    b.write_bytes(b"BBB-different")
    n2 = blobs.named_link(b, "Report Final!.docx")  # collision -> sha suffix
    assert n2 is not None and n2 != n1 and os.path.samefile(b, n2)
    assert blobs.named_link(b, "") is None  # no name -> skip


async def test_file_hash_adopts_from_qq_cache_no_network(tmp_path):
    """File rows carry QQ's md5: a matching client-cache file is adopted
    without any url resolution (adapter would fail if asked)."""
    import hashlib

    class NoUrlAdapter(Recorder):
        async def get_group_file_url(self, group_id, file_id):
            raise AssertionError("network must not be touched")

        async def get_resource_url(self, resource_id):
            raise AssertionError("network must not be touched")

    data = "季度报表内容".encode("utf-8")
    md5 = hashlib.md5(data).hexdigest()
    qq = tmp_path / "nt_data"
    (qq / "File").mkdir(parents=True)
    cached = qq / "File" / f"{md5}.xlsx"
    cached.write_bytes(data)

    conn = await open_db(str(tmp_path / "d.db"))
    repo = Repository(conn)
    blobs = BlobStore(tmp_path / "blobs")
    store = MediaStore(blobs, NoUrlAdapter(), repo, qq_cache_dir=str(qq))
    now = 1758000000
    mid = await repo.add_media(
        {
            "scene": "group", "peer_id": 2, "message_seq": 1, "seg_index": 0,
            "kind": "file", "resource_id": "", "file_id": "fid1",
            "file_name": "季度报表.xlsx", "file_hash": md5,
            "width": None, "height": None, "duration": None, "summary": "",
            "status": "pending", "time": now, "created_at": now, "updated_at": now,
        }
    )
    out = await store.download_row(await repo.media_by_id(mid))
    assert out["status"] == "done" and out["file_size"] == len(data)
    blob = blobs.resolve(out["file_path"])
    assert blob is not None and blob.read_bytes() == data and blob.suffix == ".xlsx"
    found = list((tmp_path / "blobs" / "names").glob("*.xlsx"))
    assert len(found) == 1 and found[0].read_bytes() == data  # original name kept
    await conn.close()


async def test_post_download_swap_dedups_with_qq_cache(tmp_path):
    """After an http download, an identical client-cache copy replaces ours:
    one disk copy, content-addressed slot unchanged."""
    import hashlib
    import os

    data = b"\xff\xd8\xff\xe0" + b"z" * 64
    md5 = hashlib.md5(data).hexdigest()
    qq = tmp_path / "nt_cache"
    (qq / "Pic" / "2026-10").mkdir(parents=True)
    cached = qq / "Pic" / "2026-10" / f"{md5}_Ori.jpg"
    cached.write_bytes(data)

    conn = await open_db(str(tmp_path / "e.db"))
    repo = Repository(conn)
    blobs = BlobStore(tmp_path / "blobs")
    store = MediaStore(blobs, Recorder(), repo, qq_cache_dir=str(qq))

    # simulate the already-downloaded blob sitting at its content path
    sha = hashlib.sha256(data).hexdigest()
    dest = blobs.blob_path("image", sha, ".jpg")
    dest.write_bytes(data)
    await store._swap_with_qq_cache(dest)
    assert dest.read_bytes() == data
    assert os.path.samefile(cached, dest)  # ours was replaced by a link to theirs
    await conn.close()


async def test_qq_cache_disabled_by_default(tmp_path):
    store = MediaStore(BlobStore(tmp_path / "b"), Recorder(), _noop_repo())
    assert store.qq_cache is None
    row = {"id": 1, "kind": "file", "file_name": "x", "file_hash": "deadbeef" * 4}
    assert await store._adopt_from_qq_cache(row) is None


class _noop_repo:
    def __getattr__(self, name):
        raise AssertionError(f"repo must not be touched, got {name}")
