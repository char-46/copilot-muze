"""Link-title resolution for archived messages.

Chat links show up as bare URLs, which carries zero context ("不会读链接
的标题" -- user feedback 2026-10-31): a task about a 推送 or a doc can't be
matched to its activity without the page title. This module fetches the
<title> of http(s) links cheaply (no JS, size/time capped, cached per URL in
the link_titles table); the enrichment pass appends "[链接] <title>" to the
message text so search, digests and the agent all see it.
"""

from __future__ import annotations

import html
import logging
import re

log = logging.getLogger("muze.linkmeta")

URL_RE = re.compile(r"https?://[^\s<>\"')\]，。；！？]+")

_TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL
)

MAX_TITLE = 100
FETCH_TIMEOUT = 6.0
MAX_BYTES = 65536


def extract_urls(text: str) -> list[str]:
    """Distinct http(s) URLs in a message, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.findall(text or ""):
        u = m.rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def title_from_html(body: bytes) -> str:
    """Best-effort <title> extraction from raw bytes."""
    # try declared charset, fall back to utf-8 then gbk (QQ links are often
    # legacy Chinese pages)
    head = body[:MAX_BYTES]
    m = re.search(rb"charset=[\"']?([\w-]+)", head[:2048], re.IGNORECASE)
    text = None
    if m:
        enc = m.group(1).decode("ascii", "ignore")
        try:
            text = head.decode(enc, "ignore")
        except LookupError:
            text = None
    if text is None:
        # undeclared charset: utf-8 STRICT first -- decoding with ignore
        # would "succeed" as mojibake and hide the gb18030 case (legacy
        # Chinese pages are common in QQ links)
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            text = head.decode("gb18030", "ignore")
    t = _TITLE_RE.search(text)
    if not t:
        return ""
    title = html.unescape(re.sub(r"\s+", " ", t.group(1))).strip()
    return title[:MAX_TITLE]


async def fetch_title(url: str) -> str:
    """Fetch one URL and return its <title> ('' on any failure/non-HTML).

    Deliberately tiny: no JS, no retries -- this is enrichment, not the
    fetch_url tool; a page that needs a browser is simply untitled here
    (and is exactly what capability_wish is for)."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; muze/1.0)"},
        ) as hc:
            r = await hc.get(url)
        if r.status_code != 200:
            return ""
        ctype = r.headers.get("content-type", "").lower()
        if ctype and "html" not in ctype:
            return ""
        return title_from_html(r.content)
    except Exception as e:  # network noise is normal; never propagate
        log.debug("link title fetch failed %s: %s", url[:80], e)
        return ""
