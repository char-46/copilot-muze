"""Duration parsing for commands like `/sync 2d` (re-pull the last N time).

Kept tiny and dependency-free; units may be ASCII (s/m/h/d/w) or Chinese
(秒/分/分钟/小时/时/天/周). Examples: ``30m``, ``2h``, ``2d``, ``1w``,
``30分钟``, ``2天``.
"""

from __future__ import annotations

import re

_UNIT_SECONDS = {
    "s": 1, "秒": 1,
    "m": 60, "分": 60, "分钟": 60,
    "h": 3600, "时": 3600, "小时": 3600,
    "d": 86400, "天": 86400,
    "w": 604800, "周": 604800,
}

_DUR_RE = re.compile(r"^(\d+)\s*([smhdw秒分时天周]|分钟|小时)$", re.IGNORECASE)


def parse_duration(text: str) -> int | None:
    """'2d' / '48h' / '30分钟' -> seconds, or None when not a duration."""
    if not text:
        return None
    m = _DUR_RE.match(text.strip())
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    if n > 3650:  # >10 years: almost certainly a typo
        return None
    return n * _UNIT_SECONDS[m.group(2).lower()]


def human_duration(seconds: int) -> str:
    """Seconds -> short Chinese label, e.g. 172800 -> '2 天'."""
    for unit, secs in (("周", 604800), ("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds % secs == 0:
            return f"{seconds // secs} {unit}"
    return f"{seconds} 秒"
