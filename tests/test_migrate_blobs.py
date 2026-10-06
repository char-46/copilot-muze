"""scripts/migrate_blobs.py: cross-volume move + QQNT-cache hardlink dedup
+ stored-path rewrite, verified end to end on a miniature fixture."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "migrate_blobs", Path(__file__).resolve().parents[1] / "scripts" / "migrate_blobs.py"
)
mb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mb)

PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(64)
PNG2 = b"\x89PNG\r\n\x1a\n" + os.urandom(64)


def _mk(old: Path, qq: Path, data: Path) -> tuple[Path, str, str]:
    """old tree with one image blob + one video blob (not in cache);
    QQ cache holds the image under an md5-embedded name."""
    blob = old / "image" / "ab" / ("ab" * 32 + ".jpg")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(PNG)
    other = old / "video" / "cd" / ("cd" * 32 + ".mp4")
    other.parent.mkdir(parents=True)
    other.write_bytes(PNG2)
    md5 = hashlib.md5(PNG).hexdigest()
    (qq / "Pic").mkdir(parents=True)
    (qq / "Pic" / f"thumb_{md5}_0.jpg").write_bytes(PNG)
    # names/ hardlink pointing at the image blob
    names = old / "names"
    names.mkdir()
    os.link(blob, names / "photo.jpg")
    # index.db with one dedup row
    idx = sqlite3.connect(str(old / "index.db"))
    idx.execute(
        "CREATE TABLE dedup (key TEXT PRIMARY KEY, content_hash TEXT,"
        " file_path TEXT NOT NULL, file_size INTEGER, kind TEXT, created_at INTEGER)"
    )
    idx.execute(
        "INSERT INTO dedup VALUES ('sha:1', 'ab', ?, ?, 'image', 0)",
        (str(blob), len(PNG)),
    )
    idx.commit()
    idx.close()
    # account db with one media row
    acct = data / "12345"
    acct.mkdir(parents=True)
    db = sqlite3.connect(str(acct / "muze.db"))
    db.execute("CREATE TABLE media (id INTEGER PRIMARY KEY, file_path TEXT)")
    db.execute("INSERT INTO media VALUES (1, ?)", (str(blob),))
    db.commit()
    db.close()
    return blob, md5, str(acct / "muze.db")


def test_migrate_dedups_and_rewrites(tmp_path):
    old = tmp_path / "blobs"
    new = tmp_path / "newblobs"
    qq = tmp_path / "nt_data"
    data = tmp_path / "data"
    blob, md5, acct_db = _mk(old, qq, data)
    new.mkdir()

    stats = mb.migrate_blobs(old, new, mb.build_cache_index(qq))
    # image deduped to a hardlink of the QQ cache copy, video copied
    assert stats["hardlink"] == 1 and stats["copy"] == 1
    assert stats["names_relinked"] == 1

    img_new = new / "image" / "ab" / blob.name
    assert img_new.read_bytes() == PNG
    cache_copy = qq / "Pic" / f"thumb_{md5}_0.jpg"
    assert img_new.stat().st_ino == cache_copy.stat().st_ino  # ONE physical copy
    assert (new / "video" / "cd" / ("cd" * 32 + ".mp4")).read_bytes() == PNG2
    # names/ relinked to the NEW blob
    nm = new / "names" / "photo.jpg"
    assert nm.is_file() and nm.stat().st_ino == img_new.stat().st_ino

    n = mb.rewrite_index_db(old, new)
    assert n == 1
    conn = sqlite3.connect(str(new / "index.db"))
    row = conn.execute("SELECT file_path FROM dedup").fetchone()
    conn.close()
    assert row[0] == str(img_new)

    per_acct = mb.rewrite_account_dbs(data, old, new)
    assert per_acct == {"12345": 1}
    conn = sqlite3.connect(acct_db)
    row = conn.execute("SELECT file_path FROM media").fetchone()
    conn.close()
    assert row[0] == str(img_new)

    # re-run is idempotent: everything skips
    stats2 = mb.migrate_blobs(old, new, mb.build_cache_index(qq))
    assert stats2["skip"] == 2 and stats2["hardlink"] == 0 and stats2["copy"] == 0
