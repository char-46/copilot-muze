"""HTTP /api + WebSocket /event client for a Milky protocol server."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import aiohttp

from muze.provider.base import ProtocolError

log = logging.getLogger("muze.milky")


class MilkyClient:
    def __init__(
        self,
        base_url: str,
        access_token: str = "",
        request_timeout: float = 30.0,
        reconnect_interval: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.request_timeout = request_timeout
        self.reconnect_interval = reconnect_interval
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------ session
    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _ws_url(self) -> str:
        p = urlparse(self.base_url)
        scheme = "wss" if p.scheme == "https" else "ws"
        url = f"{scheme}://{p.netloc}{p.path}/event"
        if self.access_token:
            url += f"?access_token={self.access_token}"
        return url

    # ---------------------------------------------------------------- api
    async def call(self, api: str, params: dict[str, Any] | None = None) -> Any:
        sess = await self.session()
        url = f"{self.base_url}/api/{api}"
        try:
            async with sess.post(url, json=params or {}, headers=self._headers()) as resp:
                if resp.status == 401:
                    raise ProtocolError(f"{api}: unauthorized (check milky.access_token)")
                if resp.status == 404:
                    raise ProtocolError(f"{api}: not implemented by protocol side (404)")
                body = await resp.json(content_type=None)
        except ProtocolError:
            raise
        except Exception as e:  # network / parse
            raise ProtocolError(f"{api}: {e}") from e

        if not isinstance(body, dict) or body.get("status") != "ok":
            retcode = body.get("retcode") if isinstance(body, dict) else "?"
            message = body.get("message") if isinstance(body, dict) else str(body)[:200]
            raise ProtocolError(f"{api}: retcode={retcode} message={message}")
        return body.get("data")

    # ------------------------------------------------------------- events
    async def ws_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield raw event dicts forever; reconnects with backoff on errors."""
        while True:
            try:
                sess = await self.session()
                headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
                async with sess.ws_connect(self._ws_url(), headers=headers, heartbeat=30) as ws:
                    log.info("event websocket connected: %s", self._ws_url())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                raw = json.loads(msg.data)
                            except (ValueError, TypeError):
                                log.warning("non-JSON event frame dropped: %.200s", msg.data)
                                continue
                            if isinstance(raw, dict):
                                yield raw
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("event websocket error: %s", e)
            log.warning("event websocket disconnected; retrying in %.1fs", self.reconnect_interval)
            await asyncio.sleep(self.reconnect_interval)
