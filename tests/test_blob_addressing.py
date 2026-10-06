"""Addressing-root store: the DB holds store-relative paths only; legacy
absolute / CWD-relative forms are normalized once; md5 index gives O(1)
QQNT-cache adoption."""

from __future__ import annotations

from pathlib import Path

from muze.store.blobs import BlobStore, to_store_rel
from muze.store.db import open_db
from muze.store.media import MediaStore
from muze.store.repo import Repository

from test_permissions import Recorder

JPEG = b"\xff\xd8\xff\xe0" + b"m" * 64


def test_to_store_rel_all_legacy_forms(tmp_path):
    root = tmp_path / "store"
    f = lambda s: str(s)  # noqa: E731
    # absolute under the current root
    assert to_store_rel(f(root / "image/ab/x.jpg").replace("/", "\\"), root) == "image/ab/x.jpg"
    # absolute under an OLD root (any ...\blobs\... path)
    assert to_store_rel(r"E:\old\data\blobs\video\cd\y.mp4", root) == "video/cd/y.mp4"
    # CWD-relative legacy
    assert to_store_rel("data\\blobs\\image\\ab\\x.jpg", root) == "image/ab/x.jpg"
    assert to_store_rel("./data/blobs/image/ab/x.jpg", root) == "image/ab/x.jpg"
    # canonical passes through unchanged
    assert to_store_rel("image/ab/x.jpg", root) == "image/ab/x.jpg"
    # unrelated / escaping values are rejected
    assert to_store_rel(r"E:\elsewhere\y.jpg", root) is None
    assert to_store_rel("../escape.jpg", root) is None
    assert to_store_rel("", root) is None


async def test_record_and_lookup_use_relative_paths(tmp_path):
    bs = BlobStore(tmp_path / "store")
    try:
        dest = bs.blob_path("image", "ab" * 32, ".jpg")
        dest.write_bytes(JPEG)
        await bs.record("sha:ab", "ab" * 32, str(dest), len(JPEG), "image", md5="cd" * 16)
        hit = await bs.lookup("sha:ab")
        assert hit["file_path"] == f"image/ab/{'ab' * 32}.jpg"
        assert (await bs.lookup_md5("CD" * 16))["file_path"] == hit["file_path"]
        # resolve() maps the stored rel back to the absolute path
        assert bs.resolve(hit["file_path"]) == dest.resolve() or \
            bs.resolve(hit["file_path"]).samefile(dest)
    finally:
        await bs.close()


async def test_normalize_media_paths_rewrites_account_db(tmp_path):
    conn = await open_db(str(tmp_path / "a.db"))
    repo = Repository(conn)
    bs = BlobStore(tmp_path / "store")
    ms = MediaStore(bs, Recorder(), repo)
    try:
        now = 1758000000
        base = {
            "scene": "group", "peer_id": 1, "seg_index": 0, "kind": "image",
            "resource_id": "", "file_id": "", "file_name": "", "file_hash": "",
            "width": None, "height": None, "duration": None, "summary": "",
            "status": "done", "file_size": None, "content_hash": None,
            "time": now, "created_at": now, "updated_at": now,
        }
        await repo.add_media({**base, "message_seq": 1,
                              "file_path": "data\\blobs\\image\\ab\\x.jpg"})
        await repo.add_media({**base, "message_seq": 2,
                              "file_path": str(tmp_path / "store" / "image" / "ab" / "y.jpg")})
        n = await ms.normalize_media_paths()
        assert n == 2
        rows = {r["message_seq"]: r for r in await repo.list_media()}
        assert rows[1]["file_path"] == "image/ab/x.jpg"
        assert rows[2]["file_path"] == "image/ab/y.jpg"
        # idempotent: second run rewrites nothing
        assert await ms.normalize_media_paths() == 0
        # abs_path resolves relative rows back under the store root
        p = ms.abs_path(rows[1])
        assert p is not None and p == (tmp_path / "store" / "image" / "ab" / "x.jpg")
    finally:
        await conn.close()
        await bs.close()
