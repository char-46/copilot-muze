"""Remote client mode for `muze cli --remote <url>`.

Instead of embedding a full second instance (own Milky websocket + direct
SQLite), the REPL drives the *running* instance through its webui JSON API:
POST /api/command maps 1:1 to ControlService.execute — slash commands and
`::` agent turns share the exact same server-side dispatch. The webui stays
loopback-bound; reach it over an ssh -L tunnel, a VPN, or LAN — anything
that can speak HTTP to the webui port.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp


@dataclass
class RemoteOutcome:
    """CommandOutcome mirror (the fields print_outcome uses)."""

    text: str = ""
    media_paths: list[str] = field(default_factory=list)
    is_error: bool = False


class RemoteService:
    """The `service` object repl_async drives in remote mode."""

    def __init__(self, base_url: str, token: str = "", account: str = "",
                 timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.account = account
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), headers=headers
            )
        return self._session

    def _params(self) -> dict:
        return {"account": self.account} if self.account else {}

    async def ping(self) -> dict:
        """Connect + auth check; raises RuntimeError with a hint on failure."""
        try:
            sess = await self._sess()
            async with sess.get(f"{self.base_url}/api/info",
                                params=self._params()) as r:
                if r.status == 401:
                    raise RuntimeError(
                        "upstream rejected the token (401) — check web.token "
                        "on the server / MUZE_REMOTE_TOKEN"
                    )
                r.raise_for_status()
                return await r.json()
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"cannot reach upstream {self.base_url} ({e.__class__.__name__}: {e}) — "
                "is the tunnel up and the webui enabled?"
            ) from e

    async def execute(self, text: str, ctx=None) -> RemoteOutcome:
        try:
            sess = await self._sess()
            async with sess.post(f"{self.base_url}/api/command",
                                 params=self._params(),
                                 json={"text": text}) as r:
                if r.status == 401:
                    return RemoteOutcome(is_error=True,
                                         text="[remote] token rejected (401)")
                r.raise_for_status()
                data = await r.json()
                return RemoteOutcome(
                    text=str(data.get("text", "")),
                    media_paths=list(data.get("media_paths") or []),
                    is_error=bool(data.get("is_error")),
                )
        except Exception as e:
            return RemoteOutcome(is_error=True,
                                 text=f"[remote] request failed: {e.__class__.__name__}: {e}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
