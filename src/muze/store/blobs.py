"""Application-level content-addressed blob store, shared by ALL accounts.

Layout:
    <data_dir>/blobs/<kind>/<sha[:2]>/<sha256><ext>   deduped by content
    <data_dir>/blobs/index.db                          key -> blob mapping
    <data_dir>/blobs/tmp/                              in-flight downloads

Dedup keys (checked BEFORE any download, across accounts and processes):
    face:<emoji_id>      market-face sticker (stable per sticker)
    fhash:<file_hash>    file segments whose hash the protocol side provides
    sha:<sha256>         recorded after any successful download

Every account's `media` rows keep per-message provenance (scene/peer/seq)
while `file_path` points into this shared blob tree, so identical content —
a sticker spammed across groups, the same file forwarded around, the same
image on two accounts — is stored (and fetched) exactly once.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

import aiosqlite

log = logging.getLogger("muze.blobs")

_IDX_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup (
    key          TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    file_size    INTEGER,
    kind         TEXT,
    created_at   INTEGER NOT NULL
);
"""


class BlobStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._conn: aiosqlite.Connection | None = None

    async def _ensure(self) -> aiosqlite.Connection:
        if self._conn is None:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "tmp").mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self.root / "index.db"))
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.executescript(_IDX_SCHEMA)
            await conn.commit()
            self._conn = conn
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------- lookup
    async def lookup(self, key: str) -> dict | None:
        """Return {content_hash, file_path, file_size, kind} if the key maps
        to a blob that still exists on disk."""
        conn = await self._ensure()
        async with conn.execute("SELECT * FROM dedup WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if not Path(d["file_path"]).exists():
            return None  # blob was removed; caller re-downloads
        return d

    async def record(
        self, key: str, content_hash: str, file_path: str, file_size: int, kind: str
    ) -> None:
        conn = await self._ensure()
        await conn.execute(
            """
            INSERT OR REPLACE INTO dedup (key, content_hash, file_path, file_size, kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, content_hash, file_path, file_size, kind, int(time.time())),
        )
        await conn.commit()

    # -------------------------------------------------------------- paths
    def blob_path(self, kind: str, content_hash: str, ext: str) -> Path:
        d = self.root / kind / content_hash[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / (content_hash + ext)

    def tmp_path(self) -> Path:
        self.root.joinpath("tmp").mkdir(parents=True, exist_ok=True)
        return self.root / "tmp" / f"{uuid.uuid4().hex}.part"


def dedup_key(row: dict) -> str | None:
    """Pre-download dedup key for a planned media row, if any."""
    if row.get("kind") == "face" and row.get("file_id"):
        return row["file_id"]  # already "face:<emoji_id>"
    if row.get("kind") == "file" and row.get("file_hash"):
        return f"fhash:{row['file_hash']}"
    return None
