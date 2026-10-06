"""Application-level content-addressed blob store, shared by ALL accounts.

Layout:
    <data_dir>/blobs/<kind>/<sha[:2]>/<sha256><ext>   deduped by content
    <data_dir>/blobs/names/<original-filename>        hardlink -> content blob
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
import os
import re
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

import aiosqlite

log = logging.getLogger("muze.blobs")

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def to_store_rel(value: str, root: Path) -> str | None:
    """Canonical store-relative form ('image/ab/<sha>.jpg', posix slashes)
    of any legacy stored path, or None when it doesn't address this store.

    Understood legacy forms (the DB once stored whichever was convenient):
      - absolute path under the CURRENT root
      - absolute path under an OLD root (anything ...\\blobs\\...)
      - CWD-relative 'data\\blobs\\...' / './data/blobs/...'
      - already-canonical 'image/ab/x.jpg'  (returned unchanged)
    """
    if not value:
        return None
    v = value.replace("\\", "/")
    root_pos = str(Path(root).resolve()).replace("\\", "/").rstrip("/")
    if v.startswith("/"):  # absolute posix (never ours, but be safe)
        return None
    if len(v) > 2 and v[1] == ":":  # absolute windows path
        if v.lower().startswith(root_pos.lower() + "/"):
            rel = v[len(root_pos) + 1:]
        else:
            i = v.lower().find("/blobs/")
            if i < 0:
                return None
            rel = v[i + len("/blobs/"):]
    else:
        for prefix in ("./data/blobs/", "data/blobs/"):
            if v.lower().startswith(prefix):
                rel = v[len(prefix):]
                break
        else:
            if v.startswith("../"):
                return None
            return v  # already canonical
    rel = rel.lstrip("/")
    if not rel or rel.startswith(".."):
        return None
    return rel


def safe_name(name: str, fallback: str = "file") -> str:
    """Sanitize an arbitrary original filename for the names/ directory
    (CJK and unsafe chars collapse to underscores; leading/trailing dots
    and spaces are dropped, but NOT underscores — an all-CJK stem must
    keep its underscore so the extension survives)."""
    n = _SAFE_RE.sub("_", (name or "").strip())[:80]
    n = n.strip(" .")
    return n or fallback

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
    """The addressing root of the media store.

    MULTI-ROOT: `root` is the primary (writable) store; `read_roots` are
    fallback locations (an old store before a volume migration, an archive
    copy) probed IN ORDER when a relative reference does not exist under the
    primary. Writes only ever land in the primary. The QQNT client cache is
    managed separately by MediaStore (adoption source, not a resolve root —
    its layout is not content-addressed).

    THREADING: async methods are loop-serialized (single aiosqlite conn);
    the synchronous helpers called via asyncio.to_thread (named_link, the
    QQ-cache index, sweep) are guarded with locks where they mutate state.
    """

    def __init__(self, root: Path | str, read_roots: list[Path | str] = ()) -> None:
        self.root = Path(root)
        self.read_roots = [Path(r) for r in read_roots]
        self._conn: aiosqlite.Connection | None = None
        self._link_lock = threading.Lock()

    async def _ensure(self) -> aiosqlite.Connection:
        if self._conn is None:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "tmp").mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self.root / "index.db"))
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.executescript(_IDX_SCHEMA)
            try:
                await conn.execute("ALTER TABLE dedup ADD COLUMN md5 TEXT")
            except aiosqlite.OperationalError:
                pass  # column exists
            await conn.commit()
            self._conn = conn
        return self._conn

    # ---------------------------------------------------------- addressing
    def rel_of(self, path: Path | str) -> str | None:
        """Store-relative posix form of a path inside this store (else None)."""
        return to_store_rel(str(path), self.root)

    def _parts(self, ref: str) -> list[str] | None:
        parts = PurePosixPath(ref.replace("\\", "/"))
        if parts.is_absolute() or ".." in parts.parts:
            return None
        return list(parts.parts)

    def resolve(self, ref: str | None) -> Path | None:
        """Absolute Path for a stored reference.

        Relative refs resolve against the PRIMARY root first, then each
        read-only fallback root (old stores) in order; a reference found
        nowhere returns its primary location (callers decide what a missing
        blob means). Legacy absolute values pass through unchanged.
        """
        if not ref:
            return None
        if len(ref) > 1 and ref[1] == ":":
            return Path(ref)
        parts = self._parts(ref)
        if parts is None:
            return None
        primary = self.root.joinpath(*parts)
        try:
            if primary.exists():
                return primary
            for alt in self.read_roots:
                q = alt.joinpath(*parts)
                if q.exists():
                    return q
        except OSError:
            pass
        return primary

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
        p = self.resolve(d["file_path"])
        if p is None or not p.exists():
            return None  # blob was removed; caller re-downloads
        return d

    async def lookup_md5(self, md5_hex: str) -> dict | None:
        """Content lookup by md5 (QQ's own hash) — O(1) via the index column,
        no QQNT cache filename scan needed."""
        md5_hex = (md5_hex or "").strip().lower()
        if not md5_hex:
            return None
        return await self.lookup(f"md5:{md5_hex}")

    async def record(
        self, key: str, content_hash: str, file_path: str, file_size: int, kind: str,
        md5: str | None = None,
    ) -> None:
        conn = await self._ensure()
        # the addressing root owns path resolution: store the RELATIVE form
        # so the store (and its DB rows) survive relocation between volumes
        rel = self.rel_of(file_path) or str(file_path)
        await conn.execute(
            """
            INSERT OR REPLACE INTO dedup (key, content_hash, file_path, file_size, kind, md5, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (key, content_hash, rel, file_size, kind, (md5 or "").lower() or None, int(time.time())),
        )
        if md5:
            await conn.execute(
                """
                INSERT OR REPLACE INTO dedup (key, content_hash, file_path, file_size, kind, md5, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"md5:{md5.lower()}", content_hash, rel, file_size, kind, md5.lower(), int(time.time())),
            )
        await conn.commit()

    async def normalize_dedup_paths(self) -> int:
        """One-time (idempotent) rewrite of legacy path forms in the index."""
        conn = await self._ensure()
        async with conn.execute("SELECT key, file_path FROM dedup") as cur:
            rows = await cur.fetchall()
        n = 0
        for key, fp in rows:
            rel = to_store_rel(fp or "", self.root)
            if rel is not None and rel != fp:
                await conn.execute(
                    "UPDATE dedup SET file_path=? WHERE key=?", (rel, key)
                )
                n += 1
        if n:
            await conn.commit()
            log.info("blob index: %s legacy path(s) normalized to relative", n)
        return n

    # -------------------------------------------------------------- paths
    def blob_path(self, kind: str, content_hash: str, ext: str) -> Path:
        d = self.root / kind / content_hash[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / (content_hash + ext)

    def named_link(self, blob: Path, original: str) -> Path | None:
        """A human-readable hardlink `names/<original filename>` pointing at
        the content blob (same bytes, two names, one disk copy). A name
        collision with DIFFERENT content gets a short sha suffix; same
        content is a no-op. Returns None when no usable name / link failed.

        Concurrent downloads race on the same original filename — guarded."""
        if not (original or "").strip():
            return None
        name = safe_name(original)
        if not name or name in (".", ".."):
            return None
        d = self.root / "names"
        target = d / name
        try:
            with self._link_lock:
                d.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.samefile(blob):
                        return target
                    stem = Path(name).stem
                    suffix = Path(name).suffix
                    target = d / f"{stem}-{blob.stem[:8]}{suffix}"
                    if target.exists():
                        return target if target.samefile(blob) else None
                os.link(blob, target)
            return target
        except OSError as e:
            log.debug("named_link %s failed: %s", name, e)
            return None

    def tmp_path(self) -> Path:
        self.root.joinpath("tmp").mkdir(parents=True, exist_ok=True)
        return self.root / "tmp" / f"{uuid.uuid4().hex}.part"

    def sweep_tmp(self, max_age_sec: float) -> tuple[int, int]:
        """Delete in-flight download leftovers older than max_age_sec (a
        crashed process can't clean up after itself; live downloads are
        younger than any sane threshold). Returns (files, bytes) reclaimed."""
        return self.sweep_root_tmp(self.root, max_age_sec)

    def sweep_root_tmp(self, root: Path, max_age_sec: float) -> tuple[int, int]:
        """sweep_tmp against an arbitrary root (read-only fallback stores:
        nothing writes there anymore, any leftover .part is abandoned)."""
        cutoff = time.time() - max_age_sec
        files = bytes_ = 0
        tmp = root / "tmp"
        if not tmp.is_dir():
            return 0, 0
        for p in tmp.iterdir():
            try:
                if not p.is_file() or p.stat().st_mtime > cutoff:
                    continue
                size = p.stat().st_size
                p.unlink()
                files += 1
                bytes_ += size
            except OSError:
                pass
        return files, bytes_


def dedup_key(row: dict) -> str | None:
    """Pre-download dedup key for a planned media row, if any."""
    if row.get("kind") == "face" and row.get("file_id"):
        return row["file_id"]  # already "face:<emoji_id>"
    if row.get("kind") == "file" and row.get("file_hash"):
        return f"fhash:{row['file_hash']}"
    return None
