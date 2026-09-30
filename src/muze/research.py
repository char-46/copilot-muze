"""Web research: search providers + multi-page aggregation.

The agent gets a closed research loop: `web_search` finds candidate pages and
`research` fetches the top ones and returns their extracts for synthesis.

Backends (research.provider): tavily | brave | serper | searxng | duckduckgo.
Only configured backends are used; without config the agent is told to say so.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote_plus, urlencode

import aiohttp

from muze.config import ResearchConfig

log = logging.getLogger("muze.research")


class SearchError(Exception):
    pass


def _norm(title, url, snippet) -> dict:
    return {
        "title": html.unescape(str(title or "").strip()),
        "url": str(url or "").strip(),
        "snippet": html.unescape(str(snippet or "").strip())[:400],
    }


class SearchClient:
    def __init__(self, cfg: ResearchConfig, session: aiohttp.ClientSession | None = None) -> None:
        self.cfg = cfg
        self._session = session

    @property
    def configured(self) -> bool:
        p = (self.cfg.provider or "").lower()
        if p in ("brave", "serper", "tavily"):
            return bool(self.cfg.api_key)
        if p == "searxng":
            return bool(self.cfg.base_url)
        if p == "duckduckgo":
            return True
        return False

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.cfg.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------- search
    async def search(self, query: str, n: int | None = None) -> list[dict]:
        n = max(1, min(int(n or self.cfg.max_results), 20))
        p = (self.cfg.provider or "").lower()
        if p == "tavily":
            return await self._tavily(query, n)
        if p == "brave":
            return await self._brave(query, n)
        if p == "serper":
            return await self._serper(query, n)
        if p == "searxng":
            return await self._searxng(query, n)
        if p in ("", "duckduckgo"):
            return await self._duckduckgo(query, n)
        raise SearchError(f"未知 search provider: {p}")

    async def _tavily(self, query: str, n: int) -> list[dict]:
        sess = await self._sess()
        async with sess.post(
            self.cfg.base_url or "https://api.tavily.com/search",
            json={"api_key": self.cfg.api_key, "query": query, "max_results": n},
        ) as r:
            data = await r.json()
        return [_norm(x.get("title"), x.get("url"), x.get("content")) for x in data.get("results", [])][:n]

    async def _brave(self, query: str, n: int) -> list[dict]:
        sess = await self._sess()
        url = (
            self.cfg.base_url or "https://api.search.brave.com/res/v1/web/search"
        ) + "?" + urlencode({"q": query, "count": n})
        async with sess.get(
            url, headers={"X-Subscription-Token": self.cfg.api_key, "Accept": "application/json"}
        ) as r:
            data = await r.json()
        items = (data.get("web") or {}).get("results", [])
        return [_norm(x.get("title"), x.get("url"), x.get("description")) for x in items][:n]

    async def _serper(self, query: str, n: int) -> list[dict]:
        sess = await self._sess()
        async with sess.post(
            self.cfg.base_url or "https://google.serper.dev/search",
            json={"q": query, "num": n},
            headers={"X-API-KEY": self.cfg.api_key, "Content-Type": "application/json"},
        ) as r:
            data = await r.json()
        return [
            _norm(x.get("title"), x.get("link"), x.get("snippet"))
            for x in data.get("organic", [])
        ][:n]

    async def _searxng(self, query: str, n: int) -> list[dict]:
        sess = await self._sess()
        base = self.cfg.base_url.rstrip("/")
        async with sess.get(
            f"{base}/search?" + urlencode({"q": query, "format": "json"})
        ) as r:
            data = await r.json()
        return [_norm(x.get("title"), x.get("url"), x.get("content")) for x in data.get("results", [])][:n]

    async def _duckduckgo(self, query: str, n: int) -> list[dict]:
        sess = await self._sess()
        async with sess.post(
            self.cfg.base_url or "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            page = await r.text()
        out: list[dict] = []
        for m in re.finditer(
            r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S
        ):
            out.append(_norm(re.sub("<[^>]+>", "", m.group(2)), m.group(1), ""))
            if len(out) >= n:
                break
        return out

    # ----------------------------------------------------------- research
    async def research(self, query: str, n: int | None = None, pages: int = 3) -> dict:
        """Search then fetch the top pages; returns combined extracts."""
        from muze.docs import fetch_url

        hits = await self.search(query, n)
        limit = max(1, min(int(pages), len(hits)))
        out = {"query": query, "results": []}
        for hit in hits[:limit]:
            item = dict(hit)
            try:
                text = await fetch_url(hit["url"])
                item["extract"] = text[: self.cfg.fetch_chars]
            except Exception as e:
                item["fetch_error"] = str(e)
            out["results"].append(item)
        out["other_hits"] = hits[limit:]
        return out


def search_hint(cfg: ResearchConfig) -> str:
    return (
        "未配置网络搜索（config.yaml 的 research.provider: tavily|brave|serper|"
        "searxng|duckduckgo + api_key/base_url），无法联网检索"
    )
