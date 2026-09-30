"""Media file storage: download resources from the protocol side into the
shared content-addressed blob store (see store/blobs.py).

Rows in each account's `media` table track per-message provenance and
lifecycle (pending -> downloading -> done/failed); the bytes live in
<data_dir>/blobs/<kind>/<sha[:2]>/<sha><ext>, deduped across messages,
conversations AND accounts.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.provider.milky.models import (
    FileSeg,
    ImageSeg,
    MarketFaceSeg,
    RecordSeg,
    Segment,
    VideoSeg,
)
from muze.store.blobs import BlobStore, dedup_key
from muze.store.repo import Repository

log = logging.getLogger("muze.media")

_EXT_BY_KIND = {
    "image": ".jpg",
    "record": ".amr",
    "video": ".mp4",
    "file": ".bin",
    "face": ".gif",
}
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str, fallback: str) -> str:
    name = _SAFE_RE.sub("_", (name or "").strip())[:80]
    return name or fallback


def to_file_uri(path: Path | str) -> str:
    """Absolute filesystem path -> file:// URI (Windows-safe, three slashes)."""
    p = Path(path).resolve().as_posix()
    return f"file:///{p.lstrip('/')}"


class MediaStore:
    def __init__(self, blobs: BlobStore, adapter: ProtocolAdapter, repo: Repository) -> None:
        self.blobs = blobs
        self.adapter = adapter
        self.repo = repo

    # ------------------------------------------------------------- planning
    @staticmethod
    def segment_kind(seg: Segment) -> str | None:
        if isinstance(seg, (ImageSeg, RecordSeg, VideoSeg, FileSeg, MarketFaceSeg)):
            return {
                ImageSeg: "image",
                RecordSeg: "record",
                VideoSeg: "video",
                FileSeg: "file",
                MarketFaceSeg: "face",
            }[type(seg)]
        return None

    @staticmethod
    def segment_meta(seg: Segment) -> dict:
        meta: dict = {}
        if isinstance(seg, ImageSeg):
            meta.update(
                resource_id=seg.resource_id,
                file_name="",
                width=seg.width,
                height=seg.height,
                summary=seg.summary,
            )
        elif isinstance(seg, RecordSeg):
            meta.update(resource_id=seg.resource_id, duration=seg.duration)
        elif isinstance(seg, VideoSeg):
            meta.update(
                resource_id=seg.resource_id, width=seg.width, height=seg.height, duration=seg.duration
            )
        elif isinstance(seg, FileSeg):
            meta.update(
                file_id=seg.file_id,
                file_name=seg.file_name,
                file_hash=seg.file_hash,
            )
        elif isinstance(seg, MarketFaceSeg):
            meta.update(
                file_id=f"face:{seg.emoji_id}" if seg.emoji_id else "",
                summary=seg.summary,
                file_name="",
            )
        return meta

    def plan_row(
        self, scene: str, peer_id: int, seq: int, seg_index: int, seg: Segment, msg_time: int = 0
    ) -> dict | None:
        kind = self.segment_kind(seg)
        if kind is None:
            return None
        if kind == "file" and not self.adapter.caps.incoming_file_segment:
            log.warning(
                "file segment seen but capability off; recording metadata only (%s/%s #%s)",
                scene, peer_id, seq,
            )
        meta = self.segment_meta(seg)
        return {
            "scene": scene,
            "peer_id": peer_id,
            "message_seq": seq,
            "seg_index": seg_index,
            "kind": kind,
            "resource_id": meta.get("resource_id", ""),
            "file_id": meta.get("file_id", ""),
            "file_name": meta.get("file_name", "") or "",
            "file_hash": meta.get("file_hash", ""),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "duration": meta.get("duration"),
            "summary": meta.get("summary", "") or "",
            "status": "pending",
            "time": msg_time or None,  # message timestamp, unix sec
        }

    async def register(
        self, scene: str, peer_id: int, seq: int, seg_index: int, seg: Segment, msg_time: int = 0
    ) -> int | None:
        """Plan + insert a media row. Returns the id when a DOWNLOAD is still
        needed, or None when nothing to do (not media / duplicate message /
        dedup hit: the blob already exists and the row is inserted done)."""
        row = self.plan_row(scene, peer_id, seq, seg_index, seg, msg_time=msg_time)
        if row is None:
            return None
        if row["kind"] == "file" and not self.adapter.caps.incoming_file_segment:
            row["status"] = "skipped"
            return await self.repo.add_media(row)

        # pre-download dedup: sticker / file-hash seen before (any account)
        key = dedup_key(row)
        if key:
            hit = await self.blobs.lookup(key)
            if hit:
                row.update(
                    status="done",
                    file_path=hit["file_path"],
                    file_size=hit["file_size"],
                    content_hash=hit["content_hash"],
                )
                mid = await self.repo.add_media(row)
                if mid is not None:
                    log.info(
                        "MEDIA dedup-hit %s kind=%s -> %s (no download)",
                        key, row["kind"], Path(hit["file_path"]).name,
                    )
                return None
        return await self.repo.add_media(row)

    # ------------------------------------------------------------ downloads
    async def resolve_url(self, row: dict, hint_url: str | None = None) -> str:
        """Find a usable download URL for a media row.

        Order: caller-provided fresh temp_url -> get_resource_temp_url ->
        file download APIs. Temp URLs expire, so anything older than minutes
        must be re-resolved via resource_id.
        """
        if hint_url:
            return hint_url
        if row.get("resource_id"):
            return await self.adapter.get_resource_url(row["resource_id"])
        if row.get("file_id") and row["kind"] != "face":
            if row["scene"] == "group":
                return await self.adapter.get_group_file_url(row["peer_id"], row["file_id"])
            return await self.adapter.get_private_file_url(
                row["peer_id"], row["file_id"], row.get("file_hash", "") or ""
            )
        if row["kind"] == "face":
            # no protocol API for stickers: only a fresh event temp_url works
            raise ProtocolError(
                "market-face url expired; will reuse the blob next time the sticker appears"
            )
        raise ProtocolError(f"no way to resolve url for media #{row['id']}")

    async def download_row(self, row: dict, *, prefer_url: str | None = None) -> dict:
        """Download one media row into the blob store; updates status."""
        mid = row["id"]
        if row.get("status") == "done" and row.get("file_path") and Path(row["file_path"]).exists():
            return row
        await self.repo.update_media(mid, status="downloading")
        try:
            url = await self.resolve_url(row, hint_url=prefer_url)
        except Exception as e:
            await self.repo.update_media(mid, status="failed", error=f"url: {e}")
            log.warning("MEDIA url-fail #%s %s/%s seq=%s: %s", mid, row["scene"], row["peer_id"], row["message_seq"], e)
            return {**row, "status": "failed", "error": f"url: {e}"}

        try:
            content_hash, size, dest = await self._fetch_into_blob(url, row)
        except Exception as e:
            await self.repo.update_media(mid, status="failed", error=str(e)[:300])
            log.warning("MEDIA dl-fail #%s %s: %s", mid, url[:120], e)
            return {**row, "status": "failed", "error": str(e)[:300]}

        key = dedup_key(row)
        if key:
            await self.blobs.record(key, content_hash, str(dest), size, row["kind"])
        await self.blobs.record(f"sha:{content_hash}", content_hash, str(dest), size, row["kind"])

        await self.repo.update_media(
            mid, status="done", file_path=str(dest), file_size=size, content_hash=content_hash
        )
        log.info("MEDIA done #%s kind=%s size=%s blob=%s", mid, row["kind"], size, dest.name)
        return {**row, "status": "done", "file_path": str(dest), "file_size": size,
                "content_hash": content_hash}

    def _ext_for(self, row: dict, url: str) -> str:
        if row.get("file_name") and _EXT_RE.search(row["file_name"]):
            return _EXT_RE.search(row["file_name"]).group(0).lower()
        path_ext = Path(urlparse(url).path).suffix
        if path_ext and _EXT_RE.search(path_ext):
            return path_ext.lower()
        return _EXT_BY_KIND.get(row["kind"], ".bin")

    async def _fetch_into_blob(self, url: str, row: dict) -> tuple[str, int, Path]:
        """Stream the url into a tmp file while hashing, then move it to its
        content-addressed location (dedup: same bytes -> same path)."""
        sess = await self.adapter.http.session()  # type: ignore[attr-defined]
        tmp = self.blobs.tmp_path()
        hasher = hashlib.sha256()
        total = 0
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            with open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    fh.write(chunk)
                    hasher.update(chunk)
                    total += len(chunk)
        content_hash = hasher.hexdigest()
        dest = self.blobs.blob_path(row["kind"], content_hash, self._ext_for(row, url))
        if dest.exists():
            tmp.unlink()  # identical content already stored
            log.info("MEDIA blob-hit %s (disk copy kept)", dest.name)
        else:
            tmp.replace(dest)
        return content_hash, total, dest

    # --------------------------------------------------------------- retry
    async def retry_failed(self, limit: int = 50) -> int:
        rows = await self.repo.media_pending(limit)
        ok = 0
        for row in rows:
            updated = await self.download_row(row)
            if updated.get("status") == "done":
                ok += 1
        return ok
