"""Duration parsing for commands like `/sync 2d` (re-pull the last N time).

Kept tiny and dependency-free. Units may be ASCII (s/m/h/d/w/mo/y) or Chinese
(秒/分/分钟/小时/时/天/日/周/星期/月/年). Simple and compound forms are both
accepted: ``30m``, ``2h``, ``2d``, ``1w``, ``1d12h``, ``1个月2周``,
``30分钟``, ``2天``.

Conventions (approximate, matching common usage): 1 月 = 30 天, 1 年 = 365 天.
"""

from __future__ import annotations

import re

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "秒": 1,
    "m": 60, "min": 60, "分": 60, "分钟": 60,
    "h": 3600, "时": 3600, "小时": 3600,
    "d": 86400, "天": 86400, "日": 86400,
    "w": 604800, "周": 604800, "星期": 604800,
    "mo": 2592000, "月": 2592000,
    "y": 31536000, "年": 31536000,
}

# longest alternative first so "mo" wins over "m" and "分钟" over "分"
_UNITS = "|".join(sorted((re.escape(u) for u in _UNIT_SECONDS), key=len, reverse=True))
_TOKEN_RE = re.compile(rf"(\d+)\s*({_UNITS})", re.IGNORECASE)
_FULL_RE = re.compile(rf"^(?:\s*\d+\s*(?:{_UNITS})\s*)+$", re.IGNORECASE)
_MAX_SECONDS = 3650 * 86400  # >10 years: almost certainly a typo


def parse_duration(text: str) -> int | None:
    """'2d' / '48h' / '30分钟' / '1d12h' -> seconds, or None when not a duration."""
    if not text:
        return None
    s = text.strip()
    if not _FULL_RE.match(s):
        return None
    total = 0
    for num, unit in _TOKEN_RE.findall(s):
        total += int(num) * _UNIT_SECONDS[unit.lower()]
    if total <= 0 or total > _MAX_SECONDS:
        return None
    return total


def human_duration(seconds: int) -> str:
    """Seconds -> short Chinese label, e.g. 172800 -> '2 天'."""
    for unit, secs in (
        ("年", 31536000), ("月", 2592000), ("周", 604800), ("天", 86400),
        ("小时", 3600), ("分钟", 60),
    ):
        if seconds % secs == 0:
            return f"{seconds // secs} {unit}"
    return f"{seconds} 秒"
