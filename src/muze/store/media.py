"""Media file storage: download resources from the protocol side onto disk.

Layout: <data_dir>/media/<YYYY-MM>/<scene>_<peer>_<seq>_<segidx><ext>
Rows in the `media` table track lifecycle: pending -> downloading -> done/failed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.provider.milky.models import (
    FileSeg,
    ImageSeg,
    RecordSeg,
    Segment,
    VideoSeg,
)
from muze.store.repo import Repository

log = logging.getLogger("muze.media")

_EXT_BY_KIND = {
    "image": ".jpg",
    "record": ".amr",
    "video": ".mp4",
    "file": ".bin",
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
    def __init__(self, root: Path, adapter: ProtocolAdapter, repo: Repository) -> None:
        self.root = Path(root)
        self.adapter = adapter
        self.repo = repo

    # ------------------------------------------------------------- planning
    @staticmethod
    def segment_kind(seg: Segment) -> str | None:
        if isinstance(seg, (ImageSeg, RecordSeg, VideoSeg, FileSeg)):
            return {
                ImageSeg: "image",
                RecordSeg: "record",
                VideoSeg: "video",
                FileSeg: "file",
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
        return meta

    def plan_row(self, scene: str, peer_id: int, seq: int, seg_index: int, seg: Segment) -> dict | None:
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
        }

    async def register(self, scene: str, peer_id: int, seq: int, seg_index: int, seg: Segment) -> int | None:
        row = self.plan_row(scene, peer_id, seq, seg_index, seg)
        if row is None:
            return None
        if row["kind"] == "file" and not self.adapter.caps.incoming_file_segment:
            row["status"] = "skipped"
            row["file_path"] = ""
            mid = await self.repo.add_media(row)
            return mid
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
        if row.get("file_id"):
            if row["scene"] == "group":
                return await self.adapter.get_group_file_url(row["peer_id"], row["file_id"])
            return await self.adapter.get_private_file_url(
                row["peer_id"], row["file_id"], row.get("file_hash", "") or ""
            )
        raise ProtocolError(f"no way to resolve url for media #{row['id']}")

    def _dest_path(self, row: dict) -> Path:
        month = datetime.now().strftime("%Y-%m")
        base = f"{row['scene']}_{row['peer_id']}_{row['message_seq']}_{row['seg_index']}"
        if row.get("file_name"):
            stem_ext = safe_name(row["file_name"], base)
            name = f"{base}_{stem_ext}"
        else:
            name = base + _EXT_BY_KIND.get(row["kind"], ".bin")
        if not _EXT_RE.search(name):
            name += _EXT_BY_KIND.get(row["kind"], ".bin")
        d = self.root / month
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    async def download_row(self, row: dict, *, prefer_url: str | None = None) -> dict:
        """Download one media row to disk; updates status. Returns updated row."""
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

        dest = self._dest_path(row)
        try:
            size = await self._fetch(url, dest)
        except Exception as e:
            await self.repo.update_media(mid, status="failed", error=str(e)[:300])
            log.warning("MEDIA dl-fail #%s %s: %s", mid, url[:120], e)
            return {**row, "status": "failed", "error": str(e)[:300]}

        await self.repo.update_media(mid, status="done", file_path=str(dest), file_size=size)
        log.info("MEDIA done #%s kind=%s size=%s path=%s", mid, row["kind"], size, dest)
        return {**row, "status": "done", "file_path": str(dest), "file_size": size}

    async def _fetch(self, url: str, dest: Path) -> int:
        sess = await self.adapter.http.session()  # type: ignore[attr-defined]
        tmp = dest.with_suffix(dest.suffix + ".part")
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            total = 0
            with open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    fh.write(chunk)
                    total += len(chunk)
        tmp.replace(dest)
        return total

    # --------------------------------------------------------------- retry
    async def retry_failed(self, limit: int = 50) -> int:
        rows = await self.repo.media_pending(limit)
        ok = 0
        for row in rows:
            updated = await self.download_row(row)
            if updated.get("status") == "done":
                ok += 1
        return ok
