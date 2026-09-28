"""Milky implementation of the ProtocolAdapter seam."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from muze.config import MilkyConfig
from muze.provider.base import Caps, NotSupported, ProtocolAdapter, ProtocolError
from muze.provider.milky.client import MilkyClient
from muze.provider.milky.models import (
    Event,
    ForwardedMessage,
    Friend,
    Group,
    GroupMember,
    HistoryResult,
    IncomingMessage,
    LoginInfo,
    SendMessageResult,
    parse_segments,
)

log = logging.getLogger("muze.milky")


def _first_list(data: Any, *keys: str) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


class MilkyAdapter(ProtocolAdapter):
    """Talks to LLBot / Lagrange.Milky over the Milky standard interface."""

    def __init__(self, cfg: MilkyConfig) -> None:
        self.cfg = cfg
        self.client = MilkyClient(
            cfg.base_url,
            access_token=cfg.access_token,
            request_timeout=cfg.request_timeout,
            reconnect_interval=cfg.reconnect_interval,
        )
        self.caps = Caps()
        self.self_id = 0
        self._friend_names: dict[int, str] = {}
        self._member_names: dict[tuple[int, int], str] = {}
        self._member_lookups: dict[tuple[int, int], asyncio.Future[str]] = {}
        self._group_names: dict[int, str] = {}
        self._connected = asyncio.Event()

    # ------------------------------------------------------------ session
    async def start(self) -> LoginInfo:
        info = await self.get_login_info()
        self.self_id = info.user_id
        await self._probe_caps()
        return info

    async def close(self) -> None:
        await self.client.close()

    @property
    def http(self) -> MilkyClient:
        """Shared HTTP client (also used for media downloads)."""
        return self.client

    # ---------------------------------------------------------- capability
    async def _probe_caps(self) -> None:
        caps = self.caps
        caps.notes.clear()
        try:
            impl = await self.client.call("get_impl_info", {})
            caps.impl_name = str(impl.get("impl_name", "")) if isinstance(impl, dict) else ""
            caps.impl_version = str(impl.get("app_version", "")) if isinstance(impl, dict) else ""
        except Exception as e:
            caps.impl_name = "unknown"
            caps.notes.append(f"impl info probe failed: {e}")

        name = (caps.impl_name or "").lower()
        if "lagrange" in name and "milky" in name:
            # Known gaps in Lagrange.Milky at time of writing.
            caps.incoming_file_segment = False
            caps.forwarded_messages = False
            caps.mark_read = False
            caps.resource_temp_url = True
            caps.notes.append("Lagrange.Milky detected: file segment / forwarded / mark-read assumed missing")
        else:
            # LLBot & friends implement the full spec; verified lazily at use.
            caps.incoming_file_segment = True
            caps.forwarded_messages = True
            caps.mark_read = True
            caps.resource_temp_url = True
            caps.notes.append("full-spec implementation assumed; degraded lazily on first failure")

        # Probe friend / group roaming with a harmless 1-message pull.
        friends = await self.get_friend_list()
        groups = await self.get_group_list()
        if friends:
            try:
                await self.get_history_messages("friend", friends[0].user_id, limit=1)
                caps.friend_history = True
            except ProtocolError as e:
                caps.notes.append(f"friend roaming probe failed: {e}")
        if groups:
            try:
                await self.get_history_messages("group", groups[0].group_id, limit=1)
                caps.group_history = True
            except ProtocolError as e:
                caps.notes.append(f"group roaming probe failed: {e}")

        # Probe start_message_seq support (only meaningful if history works).
        caps.history_start_seq = False
        if caps.friend_history or caps.group_history:
            peer = friends[0].user_id if friends and caps.friend_history else groups[0].group_id
            scene = "friend" if friends and caps.friend_history else "group"
            try:
                res = await self.get_history_messages(scene, peer, limit=1)
                newest = res.messages[-1].message_seq if res.messages else None
                if newest is not None:
                    await self.get_history_messages(scene, peer, start_message_seq=newest, limit=1)
                    caps.history_start_seq = True
            except ProtocolError:
                caps.history_start_seq = False
        log.info(
            "capabilities probed: friend_history=%s group_history=%s start_seq=%s file=%s",
            caps.friend_history,
            caps.group_history,
            caps.history_start_seq,
            caps.incoming_file_segment,
        )

    def _degrade(self, cap_note: str) -> None:
        self.caps.notes.append(cap_note)
        log.warning("capability degraded: %s", cap_note)

    # ------------------------------------------------------------- events
    async def events(self) -> AsyncIterator[Event]:
        async for raw in self.client.ws_events():
            ev = Event.from_raw(raw)
            if ev.event_type == "message_receive":
                # Assume file segments actually arrive if one shows up.
                seg_types = {getattr(s, "type", "") for s in ev.data.segments}
                if "file" in seg_types:
                    self.caps.incoming_file_segment = True
            yield ev

    # ------------------------------------------------------------ identity
    async def get_login_info(self) -> LoginInfo:
        data = await self.client.call("get_login_info", {})
        log.debug("get_login_info raw: %r", data)
        return LoginInfo.model_validate(data or {})

    async def get_friend_list(self) -> list[Friend]:
        data = await self.client.call("get_friend_list", {})
        friends = [Friend.model_validate(x) for x in _first_list(data, "friends")]
        for f in friends:
            self._friend_names[f.user_id] = f.display
        return friends

    async def get_friend_info(self, user_id: int) -> Friend:
        data = await self.client.call("get_friend_info", {"user_id": user_id})
        f = Friend.model_validate(data or {})
        if f.display:
            self._friend_names[f.user_id] = f.display
        return f

    async def ensure_friend_name(self, user_id: int) -> str:
        name = self._friend_names.get(user_id)
        if name:
            return name
        try:
            f = await self.get_friend_info(user_id)
            return f.display
        except (ProtocolError, Exception) as e:
            log.debug("friend name lookup failed u=%s: %s", user_id, e)
            return ""

    async def get_group_list(self) -> list[Group]:
        data = await self.client.call("get_group_list", {})
        groups = [Group.model_validate(x) for x in _first_list(data, "groups")]
        for g in groups:
            self._group_names[g.group_id] = g.group_name
        return groups

    async def get_group_info(self, group_id: int) -> Group:
        data = await self.client.call("get_group_info", {"group_id": group_id})
        g = Group.model_validate(data or {})
        if g.group_name:
            self._group_names[g.group_id] = g.group_name
        return g

    async def ensure_group_name(self, group_id: int) -> str:
        name = self._group_names.get(group_id)
        if name:
            return name
        try:
            g = await self.get_group_info(group_id)
            return g.group_name
        except (ProtocolError, Exception) as e:
            log.debug("group name lookup failed g=%s: %s", group_id, e)
            return ""

    async def get_group_member_name(self, group_id: int, user_id: int) -> str:
        key = (group_id, user_id)
        cached = self._member_names.get(key)
        if cached is not None:
            return cached
        fut = self._member_lookups.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._member_lookups[key] = fut
        try:
            data = await self.client.call(
                "get_group_member_info", {"group_id": group_id, "user_id": user_id}
            )
            member = GroupMember.model_validate(data or {})
            name = member.display
            self._member_names[key] = name
            if not fut.done():
                fut.set_result(name)
            return name
        except Exception as e:
            name = ""
            if not fut.done():
                fut.set_result(name)
            log.debug("member name lookup failed g=%s u=%s: %s", group_id, user_id, e)
            return name
        finally:
            self._member_lookups.pop(key, None)

    async def get_friend_name(self, user_id: int) -> str:
        return self._friend_names.get(user_id, "")

    def cached_group_name(self, group_id: int) -> str:
        return self._group_names.get(group_id, "")

    def cached_friend_name(self, user_id: int) -> str:
        return self._friend_names.get(user_id, "")

    # ----------------------------------------------------------- messaging
    async def send_private_message(
        self, user_id: int, segments: list[dict]
    ) -> SendMessageResult:
        data = await self.client.call(
            "send_private_message", {"user_id": user_id, "message": segments}
        )
        return SendMessageResult.model_validate(data or {})

    async def send_group_message(
        self, group_id: int, segments: list[dict]
    ) -> SendMessageResult:
        data = await self.client.call(
            "send_group_message", {"group_id": group_id, "message": segments}
        )
        return SendMessageResult.model_validate(data or {})

    async def get_history_messages(
        self,
        scene: str,
        peer_id: int,
        *,
        start_message_seq: int | None = None,
        limit: int = 30,
    ) -> HistoryResult:
        params: dict[str, Any] = {
            "message_scene": scene,
            "peer_id": peer_id,
            "limit": max(1, min(int(limit), 30)),
        }
        if start_message_seq is not None:
            params["start_message_seq"] = start_message_seq
        data = await self.client.call("get_history_messages", params)
        messages = [
            IncomingMessage.from_raw(m) for m in _first_list(data, "messages") if isinstance(m, dict)
        ]
        nxt = (data or {}).get("next_message_seq") if isinstance(data, dict) else None
        return HistoryResult(
            messages=messages,
            next_message_seq=int(nxt) if nxt is not None else None,
        )

    async def get_message(
        self, scene: str, peer_id: int, message_seq: int
    ) -> IncomingMessage | None:
        try:
            data = await self.client.call(
                "get_message",
                {"message_scene": scene, "peer_id": peer_id, "message_seq": message_seq},
            )
        except ProtocolError:
            return None
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        if not isinstance(msg, dict):
            return None
        m = IncomingMessage.from_raw(msg)
        if not m.message_seq:
            m.message_seq = message_seq
        if not m.message_scene or m.message_scene == "friend":
            m.message_scene = scene  # type: ignore[assignment]
        if not m.peer_id:
            m.peer_id = peer_id
        return m

    # --------------------------------------------------------- media/files
    async def get_resource_url(self, resource_id: str) -> str:
        if not self.caps.resource_temp_url and "temp_url degraded" in "".join(self.caps.notes):
            raise NotSupported("get_resource_temp_url previously failed on this protocol side")
        try:
            data = await self.client.call("get_resource_temp_url", {"resource_id": resource_id})
            return str((data or {}).get("url", ""))
        except ProtocolError as e:
            if "not implemented" in str(e) or "404" in str(e):
                self._degrade("temp_url degraded: API missing on protocol side")
            raise

    async def get_private_file_url(self, user_id: int, file_id: str, file_hash: str) -> str:
        data = await self.client.call(
            "get_private_file_download_url",
            {
                "user_id": user_id,
                "file_id": file_id,
                "file_hash": file_hash,
            },
        )
        return str((data or {}).get("download_url", ""))

    async def get_group_file_url(self, group_id: int, file_id: str) -> str:
        data = await self.client.call(
            "get_group_file_download_url", {"group_id": group_id, "file_id": file_id}
        )
        return str((data or {}).get("download_url", ""))

    async def get_forwarded_messages(self, forward_id: str) -> list[ForwardedMessage]:
        if not self.caps.forwarded_messages:
            raise NotSupported("forwarded messages marked unavailable on this protocol side")
        try:
            data = await self.client.call("get_forwarded_messages", {"forward_id": forward_id})
        except ProtocolError as e:
            self.caps.forwarded_messages = False
            self._degrade(f"forwarded degraded: {e}")
            raise NotSupported(str(e)) from e
        out: list[ForwardedMessage] = []
        for m in _first_list(data, "messages"):
            if not isinstance(m, dict):
                continue
            m = dict(m)
            if isinstance(m.get("segments"), list):
                m["segments"] = parse_segments(m["segments"])
            out.append(ForwardedMessage.model_validate(m))
        return out

    async def mark_read(self, scene: str, peer_id: int, message_seq: int) -> None:
        if not self.caps.mark_read:
            raise NotSupported("mark-as-read unavailable on this protocol side")
        try:
            await self.client.call(
                "mark_message_as_read",
                {"message_scene": scene, "peer_id": peer_id, "message_seq": message_seq},
            )
        except ProtocolError as e:
            self.caps.mark_read = False
            self._degrade(f"mark-read degraded: {e}")
            raise
