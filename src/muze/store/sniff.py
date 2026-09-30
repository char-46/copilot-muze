"""Magic-byte detection for downloaded media.

The protocol side gives no trustworthy content type, and voice in particular
arrives either as Tencent SILK (``#!SILK_V3``, sometimes prefixed with 0x02)
or AMR. Sniffing the first bytes lets us store each blob under the correct
extension instead of a blind per-kind guess (which used to force every voice
clip to ``.amr``).

This module is pure-python and dependency-free so it can be unit-tested
without touching the network or the audio codec.
"""

from __future__ import annotations

_CONTAINER_EXT = {
    "silk": ".silk",
    "amr": ".amr",
    "mp3": ".mp3",
    "aac": ".aac",
    "ogg": ".ogg",
    "wav": ".wav",
    "flac": ".flac",
    "m4a": ".m4a",
    "png": ".png",
    "jpg": ".jpg",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
    "tiff": ".tiff",
    "pdf": ".pdf",
    "zip": ".zip",
    "gzip": ".gz",
}

# containers we can turn into a broadly playable file (see media.py)
TRANSCODE_TO_WAV = {"silk"}
PLAYABLE_AUDIO = {"silk", "amr", "mp3", "aac", "ogg", "wav", "flac", "m4a"}


def sniff_container(head: bytes) -> str | None:
    """Best-effort container name from the leading bytes, or None."""
    h = head or b""
    if len(h) < 2:
        return None

    # ---- audio -----------------------------------------------------------
    # Tencent SILK: "#!SILK_V3" / "#!SILK" (occasionally preceded by 0x02)
    if b"#!SILK" in h[:16]:
        return "silk"
    if h.startswith(b"#!AMR"):  # AMR-NB / AMR-WB
        return "amr"
    if h.startswith(b"ID3"):
        return "mp3"
    if h[0] == 0xFF and (h[1] & 0xF6) == 0xF0:  # ADTS AAC
        return "aac"
    if h[0] == 0xFF and (h[1] & 0xE0) == 0xE0:  # MPEG audio frame sync
        return "mp3"
    if h.startswith(b"OggS"):
        return "ogg"
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return "wav"
    if h.startswith(b"fLaC"):
        return "flac"
    if h[4:8] == b"ftyp":  # ISO-BMFF (m4a/mp4) — checked before images
        return "m4a"

    # ---- images ----------------------------------------------------------
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if h.startswith(b"GIF87a") or h.startswith(b"GIF89a"):
        return "gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "webp"
    if h.startswith(b"BM"):
        return "bmp"
    if h.startswith(b"II*\x00") or h.startswith(b"MM\x00*"):
        return "tiff"

    # ---- documents / archives -------------------------------------------
    if h.startswith(b"%PDF"):
        return "pdf"
    if h.startswith(b"PK\x03\x04"):
        return "zip"
    if h.startswith(b"\x1f\x8b"):
        return "gzip"
    return None


def sniff_ext(head: bytes) -> str | None:
    """Suggested file extension (with dot) for the leading bytes, or None."""
    container = sniff_container(head)
    return _CONTAINER_EXT.get(container) if container else None
