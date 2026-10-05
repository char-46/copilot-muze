"""Semantic versioning with pre-release and build metadata.

The base version follows semver with a pre-release segment (e.g. 0.1.0-alpha.7).
Build metadata is resolved at runtime from git (commit sha, dirty flag) plus the
process start time, e.g.::

    0.1.0-alpha.7+build.g1a2b3c4.dirty.20260928T120000Z

Keep BASE_VERSION in sync with `version` in pyproject.toml on every feature
commit; each such commit is tagged `v<BASE_VERSION>`.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from functools import lru_cache

BASE_VERSION = "0.1.0-alpha.97"


def _git(args: list[str]) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _build_metadata() -> str:
    sha = _git(["rev-parse", "--short", "HEAD"]) or "unknown"
    dirty = _git(["status", "--porcelain"])
    parts = [f"g{sha}"]
    if dirty:
        parts.append("dirty")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parts.append(stamp)
    return "build." + ".".join(parts)


@lru_cache(maxsize=1)
def get_version() -> str:
    return f"{BASE_VERSION}+{_build_metadata()}"


def build_time_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def epoch_iso(epoch: int | float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def uptime_since(start: float) -> str:
    secs = max(0, int(time.time() - start))
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d{h}h{m}m"
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"
