"""Cross-process single-instance lock for one account's serve process.

A second process writing the same muze.db (a manually launched daemon/serve
while the supervised one already runs, or an overlapping rc=100 relaunch)
produces SQLITE_BUSY bursts on the hot ingest path. SQLite-level guards
(busy_timeout, retries) absorb short contention; this lock removes the
root cause: exactly one serve process per account directory, enforced by
an OS-level exclusive lock on <account>/serve.lock.

The lock is held for the process lifetime (the handle is never released
explicitly) — the OS reclaims it on exit or crash, so a dead process can
never leave a stale lock behind.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("muze.instlock")

_HEARTBEAT = 0.25  # seconds between acquisition attempts


class InstanceLock:
    """Acquired lock handle. Keep a reference alive for the process lifetime."""

    def __init__(self, path: Path, handle) -> None:
        self.path = path
        self._handle = handle  # open file object; closing it releases the lock


# one lock per resolved path per PROCESS: two App instances inside the same
# serve process may legitimately share an account directory (e.g. the same
# QQ exposed through two Milky endpoints); cross-process exclusion is what
# the OS lock provides.
_held: dict[str, InstanceLock] = {}


def acquire(path: Path, timeout: float = 20.0) -> InstanceLock | None:
    """Try to take the exclusive lock, waiting up to `timeout` seconds.

    Returns the lock handle, or None when another PROCESS holds it (the
    wait exists for a dying predecessor — an rc=100 relaunch overlap —
    not for a healthy second instance). Re-acquiring a path this process
    already holds returns the shared handle (same-process sharing is OK).
    """
    import os

    path = Path(path).resolve()
    key = str(path).lower() if os.name == "nt" else str(path)
    existing = _held.get(key)
    if existing is not None and existing._handle.closed:
        _held.pop(key, None)
        existing = None
    if existing is not None:
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        f = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.seek(0)
            f.truncate()
            f.write(str(time.time()).encode())
            f.flush()
            lock = InstanceLock(path, f)
            _held[key] = lock
            return lock
        except OSError:
            f.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(_HEARTBEAT)


def describe_holder(lock: InstanceLock | None, path: Path) -> str:
    """Human hint for the failure message (best effort)."""
    try:
        ts = path.read_text().strip()
        if ts:
            held_for = time.time() - float(ts)
            return f"（该实例约启动于 {held_for/60:.0f} 分钟前）"
    except Exception:
        pass
    return ""
