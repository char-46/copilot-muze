"""Media file storage: download resources from the protocol side into the
shared content-addressed blob store (see store/blobs.py).

Rows in each account's `media` table track per-message provenance and
lifecycle (pending -> downloading -> done/failed); the bytes live in
<data_dir>/blobs/<kind>/<sha[:2]>/<sha><ext>, deduped across messages,
conversations AND accounts.

Hook-based protocol sides (LLBot hooking the local QQ client) may answer
the url APIs with the client's OWN cache file; those are adopted locally
(hardlink, else copy) instead of downloaded — no expiring rkey, no network,
and the bytes survive the client's cache cleanup.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from muze.provider.base import ProtocolAdapter, ProtocolError
from muze.provider.milky.models import (
    FileSeg,
    ImageSeg,
    MarketFaceSeg,
    RecordSeg,
    Segment,
    VideoSeg,
)
from muze.store.blobs import BlobStore, dedup_key, safe_name
from muze.store.repo import Repository
from muze.store.sniff import sniff_ext

log = logging.getLogger("muze.media")


class MediaGone(Exception):
    """The resource no longer exists server-side (HTTP 404/410).

    Typical for old roaming media: QQ's multimedia servers purge expired
    files, so retrying is pointless — the row is parked as status='gone'."""


_EXT_BY_KIND = {
    "image": ".jpg",
    "record": ".amr",
    "video": ".mp4",
    "file": ".bin",
    "face": ".gif",
}
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def to_file_uri(path: Path | str) -> str:
    """Absolute filesystem path -> file:// URI (Windows-safe, three slashes)."""
    p = Path(path).resolve().as_posix()
    return f"file:///{p.lstrip('/')}"


def _local_path(url: str) -> Path | None:
    """Best-effort: hook-based protocol sides (LLBot & friends, running
    inside the QQ client on this machine) sometimes answer the url APIs with
    the client's OWN cache file (a local path / file:// URI) instead of an
    expiring rkey HTTPS URL. Recognize exactly those; anything else is None
    and the normal HTTP path applies."""
    u = (url or "").strip()
    if not u:
        return None
    if u.startswith("file://"):
        p = urlparse(u)
        if p.netloc not in ("", "localhost"):
            return None  # UNC / remote host: skip
        path = unquote(p.path or "")
        if re.match(r"^/[A-Za-z]:[/\\]", path):
            path = path[1:]  # file:///C:/... -> C:/...
        return Path(path)
    if u.startswith(("http://", "https://", "ws://", "wss://", "data:", "blob:")):
        return None
    if re.match(r"^[A-Za-z]:[\\/]", u) or u.startswith("\\\\"):
        return Path(u)  # bare absolute Windows path / UNC
    return None


class MediaStore:
    def __init__(
        self,
        blobs: BlobStore,
        adapter: ProtocolAdapter,
        repo: Repository,
        transcode_silk: bool = True,
        qq_cache_dir: str = "",
    ) -> None:
        self.blobs = blobs
        self.adapter = adapter
        self.repo = repo
        self.transcode_silk = transcode_silk
        # best-effort local QQ client cache (QQNT nt_data & co.)
        self.qq_cache = Path(qq_cache_dir).resolve() if qq_cache_dir else None
        self._qq_index: dict[str, Path] = {}
        self._qq_index_at = 0.0
        self._qq_refresh_at = 0.0

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

        # best-effort pre-download adopt: QQ's md5 file_hash may match the
        # local client's cache — no network at all when it hits
        adopted = await self._adopt_from_qq_cache(row)
        if adopted is not None:
            return adopted

        try:
            url = await self.resolve_url(row, hint_url=prefer_url)
        except Exception as e:
            await self.repo.update_media(mid, status="failed", error=f"url: {e}")
            log.warning("MEDIA url-fail #%s %s/%s seq=%s: %s", mid, row["scene"], row["peer_id"], row["message_seq"], e)
            return {**row, "status": "failed", "error": f"url: {e}"}

        orig_name = str(row.get("file_name") or "")
        try:
            content_hash, size, dest = await self._fetch_into_blob(url, row)
        except MediaGone as e:
            await self.repo.update_media(mid, status="gone", error=str(e)[:300])
            log.warning("MEDIA gone  #%s kind=%s: %s (server no longer has it)", mid, row["kind"], e)
            return {**row, "status": "gone", "error": str(e)[:300]}
        except Exception as e:
            await self.repo.update_media(mid, status="failed", error=str(e)[:300])
            log.warning("MEDIA dl-fail #%s %s: %s", mid, url[:120], e)
            return {**row, "status": "failed", "error": str(e)[:300]}
        if not orig_name:
            lp = _local_path(url)
            if lp is not None:
                orig_name = lp.name  # adopted from the client's cache

        # SILK voice: keep the raw clip but expose a playable WAV as the blob
        # consumers download / feed to the agent.
        dest, content_hash, size = await self._maybe_transcode_silk(dest, content_hash, size, row)

        # the same bytes are usually already in the local client's cache:
        # swap our fresh copy for a hardlink to theirs (one disk copy)
        await self._swap_with_qq_cache(dest)

        return await self._finish_row(row, dest, content_hash, size, orig_name)

    async def _finish_row(
        self, row: dict, dest: Path, content_hash: str, size: int, orig_name: str
    ) -> dict:
        key = dedup_key(row)
        if key:
            await self.blobs.record(key, content_hash, str(dest), size, row["kind"])
        await self.blobs.record(f"sha:{content_hash}", content_hash, str(dest), size, row["kind"])
        named = None
        if orig_name:
            named = await asyncio.to_thread(self.blobs.named_link, dest, orig_name)
        await self.repo.update_media(
            row["id"], status="done", file_path=str(dest), file_size=size, content_hash=content_hash
        )
        log.info(
            "MEDIA done #%s kind=%s size=%s blob=%s%s",
            row["id"], row["kind"], size, dest.name,
            f" name={named.name}" if named else "",
        )
        return {**row, "status": "done", "file_path": str(dest), "file_size": size,
                "content_hash": content_hash}

    # ------------------------------------------------ qq client cache
    _QQ_INDEX_MAX = 100_000  # filename cap of the cache index
    _QQ_INDEX_TTL = 60.0  # reuse the index across lookups
    _QQ_REFRESH_COOLDOWN = 15.0  # refresh-on-miss at most this often

    def _qq_cache_index(self, force: bool = False) -> dict[str, Path]:
        now = time.monotonic()
        if force or now - self._qq_index_at > self._QQ_INDEX_TTL:
            idx: dict[str, Path] = {}
            scanned = 0
            if self.qq_cache is not None:
                for root, _dirs, files in os.walk(self.qq_cache):
                    for fn in files:
                        scanned += 1
                        if scanned > self._QQ_INDEX_MAX:
                            log.info("MEDIA qq-cache index capped at %s entries", self._QQ_INDEX_MAX)
                            break
                        idx[fn.lower()] = Path(root) / fn
            self._qq_index = idx
            self._qq_index_at = now
            log.debug("MEDIA qq-cache index: %s entries", len(idx))
        return self._qq_index

    def _qq_cache_find(self, md5_hex: str, expect_size: int | None) -> Path | None:
        """Filename-embedded-md5 lookup over the local QQ client's cache
        (QQNT names many Pic/Video/File blobs that way). Index is reused
        (TTL) and refreshed on miss at a limited rate, so a file the client
        cached seconds ago is still found without walking per lookup."""
        md5_hex = (md5_hex or "").strip().lower()
        if not md5_hex or self.qq_cache is None:
            return None

        def lookup() -> Path | None:
            for fn, p in self._qq_index.items():
                if md5_hex in fn:
                    try:
                        if expect_size is None or p.stat().st_size == expect_size:
                            return p
                    except OSError:
                        continue
            return None

        hit = lookup()
        if hit is not None:
            return hit
        if time.monotonic() - self._qq_refresh_at >= self._QQ_REFRESH_COOLDOWN:
            self._qq_refresh_at = time.monotonic()
            self._qq_cache_index(force=True)
            return lookup()
        return None

    @staticmethod
    def _file_md5(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()

    async def _adopt_from_qq_cache(self, row: dict) -> dict | None:
        """File segments carry QQ's own md5 file_hash: if the local client
        already cached those exact bytes, adopt the file (hardlink) without
        touching the network."""
        fh = str(row.get("file_hash") or "").strip().lower()
        if not fh or self.qq_cache is None or not self.qq_cache.is_dir():
            return None
        cand = await asyncio.to_thread(self._qq_cache_find, fh, None)
        if cand is None:
            return None
        try:
            if await asyncio.to_thread(self._file_md5, cand) != fh:
                return None  # filename coincidence, not the same content
            content_hash, size, dest = await self._link_into_blob(cand, row)
        except Exception as e:
            log.info("MEDIA qq-cache adopt failed (%s): %s", cand.name, e)
            return None
        log.info("MEDIA qq-cache md5-hit %s -> %s (no download)", fh, cand.name)
        return await self._finish_row(row, dest, content_hash, size,
                                      str(row.get("file_name") or cand.name))

    async def _swap_with_qq_cache(self, dest: Path) -> None:
        """Post-download dedup: the QQ client almost always cached the media
        it displayed. When the cache holds the same bytes (found via the
        md5 embedded in its cache filename), drop our fresh copy and hardlink
        theirs into the content-addressed slot — one disk copy, not two."""
        if self.qq_cache is None or not self.qq_cache.is_dir() or not dest.exists():
            return
        try:
            md5 = await asyncio.to_thread(self._file_md5, dest)
            cand = await asyncio.to_thread(
                self._qq_cache_find, md5, dest.stat().st_size
            )
            if cand is None or cand.samefile(dest):
                return
            if await asyncio.to_thread(self._file_md5, cand) != md5:
                return  # name/size coincidence, not the same content
            def _swap() -> None:
                dest.unlink()
                os.link(cand, dest)
            await asyncio.to_thread(_swap)
            log.info("MEDIA qq-cache dedup %s <-> %s (hardlink, single copy)",
                     dest.name, cand.name)
        except Exception as e:
            log.debug("qq-cache swap skipped: %s", e)

    def _ext_for(self, row: dict, url: str, head: bytes = b"") -> str:
        name_ext = ""
        if row.get("file_name"):
            m = _EXT_RE.search(row["file_name"])
            if m:
                name_ext = m.group(0).lower()
        sniffed = sniff_ext(head)
        # a file's own name wins (docx/zip share magic), otherwise trust bytes
        if row.get("kind") == "file" and name_ext:
            return name_ext
        if sniffed:
            return sniffed
        if name_ext:
            return name_ext
        path_ext = Path(urlparse(url).path).suffix
        if path_ext and _EXT_RE.search(path_ext):
            return path_ext.lower()
        return _EXT_BY_KIND.get(row["kind"], ".bin")

    async def _maybe_transcode_silk(
        self, dest: Path, content_hash: str, size: int, row: dict
    ) -> tuple[Path, str, int]:
        """Decode a stored SILK clip into a playable WAV.

        The raw SILK blob is kept (recorded under its content hash); the WAV
        becomes the row's primary file so the webui/agent get something that
        plays anywhere. Falls back to the original on any failure.
        """
        if (
            row.get("kind") != "record"
            or not self.transcode_silk
            or dest.suffix.lower() != ".silk"
        ):
            return dest, content_hash, size
        try:
            import pilk  # pure-python silk codec (optional dependency)
        except Exception:
            log.info("MEDIA silk transcode skipped: pilk unavailable (%s)", dest.name)
            return dest, content_hash, size

        wav_tmp = self.blobs.tmp_path().with_suffix(".wav")
        try:
            await asyncio.to_thread(pilk.silk_to_wav, str(dest), str(wav_tmp), 24000)
            data = wav_tmp.read_bytes()
        except Exception as e:
            log.warning("MEDIA silk transcode failed (%s); keeping %s", e, dest.name)
            try:
                wav_tmp.unlink()
            except OSError:
                pass
            return dest, content_hash, size

        wav_hash = hashlib.sha256(data).hexdigest()
        wav_dest = self.blobs.blob_path("record", wav_hash, ".wav")
        if wav_dest.exists():
            wav_tmp.unlink()
        else:
            wav_tmp.replace(wav_dest)
        # preserve the original silk clip in the shared blob store
        await self.blobs.record(f"sha:{content_hash}", content_hash, str(dest), size, "record")
        log.info("MEDIA silk->wav %s -> %s (%s bytes)", dest.name, wav_dest.name, len(data))
        return wav_dest, wav_hash, len(data)

    async def _fetch_into_blob(self, url: str, row: dict) -> tuple[str, int, Path]:
        """Stream the url into a tmp file while hashing, then move it to its
        content-addressed location (dedup: same bytes -> same path).

        Hook protocol sides may hand us the QQ client's own cache file —
        adopt that locally instead of downloading (see _link_into_blob)."""
        local = _local_path(url)
        if local is not None:
            return await self._link_into_blob(local, row)
        sess = await self.adapter.http.session()  # type: ignore[attr-defined]
        tmp = self.blobs.tmp_path()
        hasher = hashlib.sha256()
        total = 0
        async with sess.get(url) as resp:
            if resp.status in (404, 410):
                raise MediaGone(f"HTTP {resp.status}")
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            with open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    fh.write(chunk)
                    hasher.update(chunk)
                    total += len(chunk)
        with open(tmp, "rb") as fh:
            head = fh.read(512)
        content_hash = hasher.hexdigest()
        dest = self.blobs.blob_path(row["kind"], content_hash, self._ext_for(row, url, head))
        if dest.exists():
            tmp.unlink()  # identical content already stored
            log.info("MEDIA blob-hit %s (disk copy kept)", dest.name)
        else:
            tmp.replace(dest)
        return content_hash, total, dest

    async def _link_into_blob(self, src: Path, row: dict) -> tuple[str, int, Path]:
        """Adopt a hook-side local file (the QQ client's own cache blob) into
        the blob store: HARDLINK when the filesystem allows it — zero bytes
        copied, and the content survives the client's cache eviction (a
        hardlink keeps the data alive; deleting QQ's name doesn't free it).
        Cross-volume / link-denied -> plain copy. We never keep a bare
        pointer into the client's cache: it would break silently on cleanup."""
        if not src.is_file():
            raise RuntimeError(f"local source missing: {src}")

        def _read() -> tuple[str, int, bytes]:
            hasher = hashlib.sha256()
            total = 0
            head = b""
            with open(src, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:512]
                    hasher.update(chunk)
                    total += len(chunk)
            return hasher.hexdigest(), total, head

        content_hash, total, head = await asyncio.to_thread(_read)
        dest = self.blobs.blob_path(row["kind"], content_hash, self._ext_for(row, "", head))
        if dest.exists():
            log.info("MEDIA blob-hit %s (disk copy kept)", dest.name)
            return content_hash, total, dest

        def _place() -> None:
            try:
                os.link(src, dest)
                log.info(
                    "MEDIA qq-cache-link %s -> %s (client blob adopted, no download)",
                    src.name, dest.name,
                )
            except OSError:
                shutil.copyfile(src, dest)
                log.info(
                    "MEDIA qq-cache-copy %s -> %s (hardlink unavailable)",
                    src.name, dest.name,
                )

        await asyncio.to_thread(_place)
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
