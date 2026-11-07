"""Protocol adapter abstraction.

The whole application depends only on :class:`ProtocolAdapter`, never on a
concrete protocol stack. Today the only implementation is the Milky adapter
(talking to LLBot / Lagrange.Milky over HTTP+WS). A raw-Lagrange adapter
(LagrangeV2 `Lagrange.Core.NativeAPI` C-ABI bindings, in-process) can be added
later behind the exact same interface; its job would be mapping Lagrange
entities onto the Milky-shaped models used app-wide.

Data shapes are the pydantic models in :mod:`muze.provider.milky.models`
(Milky IR is treated as the neutral canonical schema).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field

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
)

log = logging.getLogger("muze.provider")


class ProtocolError(RuntimeError):
    """A protocol-side API call failed.

    ``kind`` separates the two failure worlds (contract semantics, not string
    sniffing at the call sites):

    - ``"refused"`` -- the protocol side EXPLICITLY rejected the call
      (non-zero retcode, 401/404): the message was NOT delivered, so any
      re-send cannot duplicate it.
    - ``"network"`` -- transport failed / timed out: delivery is UNKNOWN.
      The send may have landed with only the response lost (live 2026-11-07:
      every "failed" digest send had actually arrived, and the re-sends
      duplicated it 11 times overnight).
    - ``"risk"``   -- blocked by the local send policy (budget); nothing hit
      the wire.
    """

    def __init__(self, msg: str, kind: str = "unknown") -> None:
        super().__init__(msg)
        self.kind = kind

    @property
    def refused(self) -> bool:
        return self.kind == "refused"


class NotSupported(ProtocolError):
    """The active adapter lacks this capability (nothing was sent)."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, kind="refused")


@dataclass
class RiskPolicy:
    """Risk-control shape for OUTBOUND traffic, enforced inside the adapter
    contract so every backend (Milky today; OneBot 11/12, raw Lagrange
    tomorrow) inherits the same guards and the app above never has to trust
    its own retry logic (user direction 2026-11-07: 连续发相同消息会被风控)."""

    # an identical send (same conv, same content fingerprint) inside this
    # window is suppressed outright -- the last-resort net under every app
    # layer; a wire attempt that died NETWORK-unknown also leaves its
    # fingerprint behind, so a blind re-send of possibly-delivered content
    # is suppressed too. Explicit refusals clear the fingerprint (a re-send
    # is then safe: the first try provably never landed).
    dupe_window_sec: float = 600.0
    # minimum human-ish gap between consecutive wire sends to the SAME
    # conversation, plus a uniform jitter on top (metronome-exact rates are
    # the classic bot fingerprint)
    min_gap_sec: float = 0.6
    gap_jitter_sec: float = 0.6
    # rolling hourly ceiling on wire sends across all conversations; only a
    # runaway loop can trip it -- which is exactly what it exists to stop
    hourly_budget: int = 160


@dataclass
class _ConvSendTrack:
    last_wire: float = 0.0
    fingerprints: dict[str, float] = field(default_factory=dict)


@dataclass
class Caps:
    """Runtime-probed capabilities of the active protocol side."""

    friend_history: bool = False
    group_history: bool = False
    history_start_seq: bool = False
    incoming_file_segment: bool = False
    forwarded_messages: bool = False
    send_forward: bool = False
    mark_read: bool = False
    resource_temp_url: bool = False
    impl_name: str = ""
    impl_version: str = ""
    notes: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        def yn(b: bool) -> str:
            return "yes" if b else "no"

        return [
            f"impl: {self.impl_name} {self.impl_version}".rstrip(),
            f"friend/group roaming: {yn(self.friend_history)} / {yn(self.group_history)}",
            f"history start_seq paging: {yn(self.history_start_seq)}",
            f"incoming file segment: {yn(self.incoming_file_segment)}",
            f"forwarded messages fetch: {yn(self.forwarded_messages)}",
            f"send forward chain: {yn(self.send_forward)}",
            f"mark as read: {yn(self.mark_read)}",
            f"resource temp url: {yn(self.resource_temp_url)}",
            *self.notes,
        ]


class ProtocolAdapter(ABC):
    """Interface between muze and a QQ protocol implementation."""

    caps: Caps

    @abstractmethod
    async def start(self) -> LoginInfo:
        """Connect, verify login, probe capabilities. Returns login info."""

    @abstractmethod
    async def close(self) -> None: ...

    # ---- event stream -------------------------------------------------
    @abstractmethod
    def events(self) -> "any":
        """Async iterator of :class:`Event`; must reconnect transparently."""

    # ---- identity -----------------------------------------------------
    @abstractmethod
    async def get_login_info(self) -> LoginInfo: ...

    @abstractmethod
    async def get_friend_list(self) -> list[Friend]: ...

    @abstractmethod
    async def get_group_list(self) -> list[Group]: ...

    async def get_group_info(self, group_id: int) -> Group:
        """Single group info (name); used for lazy name resolution."""
        raise NotSupported("get_group_info not supported by this protocol side")

    async def ensure_group_name(self, group_id: int) -> str:
        """Best-effort group display name; cached, lazy network fallback."""
        return ""

    @abstractmethod
    async def get_group_member_name(self, group_id: int, user_id: int) -> str:
        """Best-effort display name for a group member (cached)."""

    async def get_group_member_list(self, group_id: int) -> list[GroupMember]:
        """Full member roster of a group; implementations should cache."""
        raise NotSupported("get_group_member_list not supported by this protocol side")

    async def get_friend_info(self, user_id: int) -> Friend:
        """Single friend info; used for lazy name resolution."""
        raise NotSupported("get_friend_info not supported by this protocol side")

    async def ensure_friend_name(self, user_id: int) -> str:
        """Best-effort friend display name; cached, lazy network fallback."""
        return ""

    async def get_friend_name(self, user_id: int) -> str:
        """Best-effort display name for a friend (cached). May hit network."""
        return ""

    def cached_friend_name(self, user_id: int) -> str:
        """Sync cache-only lookup; no network."""
        return ""

    def cached_group_name(self, group_id: int) -> str:
        """Sync cache-only lookup; no network."""
        return ""

    # ---- messaging ----------------------------------------------------
    # The PUBLIC send surface is the risk-guarded contract entry point;
    # backends implement the _raw pair. Every outbound path (chat replies,
    # digests, notices, forward chains, file messages) funnels through here,
    # so identical-content suppression, per-conversation pacing and the
    # hourly budget apply no matter which backend is plugged in.
    risk: RiskPolicy = RiskPolicy()  # class default; App overrides from config

    def __init__(self) -> None:
        self._send_tracks: dict[tuple[str, int], _ConvSendTrack] = {}
        self._wire_times: deque = deque()

    async def send_private_message(
        self, user_id: int, segments: list[dict]
    ) -> SendMessageResult:
        return await self._risk_guarded(
            "friend", user_id, self._send_private_raw, user_id, segments
        )

    async def send_group_message(
        self, group_id: int, segments: list[dict]
    ) -> SendMessageResult:
        return await self._risk_guarded(
            "group", group_id, self._send_group_raw, group_id, segments
        )

    def __init_subclass__(cls, **kw):
        # Auto-wrap: a backend writes send_private_message / send_group_message
        # the natural way; it is stashed as the _raw wire call and the public
        # name stays the risk-guarded contract entry. The guard can never be
        # accidentally bypassed by an implementation, no matter which protocol
        # stack (Milky / OB11 / OB12 / raw Lagrange) implements it.
        super().__init_subclass__(**kw)
        for public, raw in (
            ("send_private_message", "_send_private_raw"),
            ("send_group_message", "_send_group_raw"),
        ):
            impl = cls.__dict__.get(public)
            if impl is not None:
                setattr(cls, raw, impl)
                setattr(cls, public, getattr(ProtocolAdapter, public))

    async def _send_private_raw(
        self, user_id: int, segments: list[dict]
    ) -> SendMessageResult:
        raise NotSupported("send_private_message not implemented by this adapter")

    async def _send_group_raw(
        self, group_id: int, segments: list[dict]
    ) -> SendMessageResult:
        raise NotSupported("send_group_message not implemented by this adapter")

    async def _risk_guarded(
        self,
        scene: str,
        peer: int,
        raw,  # Callable[[int, list[dict]], Awaitable[SendMessageResult]]
        target_id: int,
        segments: list[dict],
    ) -> SendMessageResult:
        p = self.risk
        now = time.monotonic()
        # lazy: implementors that don't chain __init__ still get the guard
        if not hasattr(self, "_send_tracks"):
            self._send_tracks = {}
            self._wire_times = deque()
        track = self._send_tracks.setdefault((scene, peer), _ConvSendTrack())
        fp = hashlib.sha1(
            json.dumps(segments, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        # prune stale fingerprints
        for k in [k for k, ts in track.fingerprints.items() if now - ts > p.dupe_window_sec]:
            del track.fingerprints[k]
        if fp in track.fingerprints:
            log.warning(
                "risk-guard: suppressed identical send to %s (fingerprint %s "
                "seen %.0fs ago <= window %.0fs)",
                f"{scene}/{peer}", fp, now - track.fingerprints[fp], p.dupe_window_sec,
            )
            return SendMessageResult(message_seq=0, time=int(time.time()))
        # per-conversation pacing with jitter
        wait = track.last_wire + p.min_gap_sec + random.uniform(0.0, p.gap_jitter_sec) - now
        if wait > 0:
            await asyncio.sleep(wait)
        # rolling hourly budget (counts wire attempts)
        now = time.monotonic()
        while self._wire_times and now - self._wire_times[0] > 3600.0:
            self._wire_times.popleft()
        if len(self._wire_times) >= p.hourly_budget:
            raise ProtocolError(
                f"send to {scene}/{peer} blocked: hourly risk budget "
                f"({p.hourly_budget}) exceeded", kind="risk",
            )
        self._wire_times.append(now)
        track.last_wire = now
        # fingerprint BEFORE the wire: a network-unknown death then suppresses
        # blind re-sends of possibly-delivered content within the window
        track.fingerprints[fp] = now
        try:
            res = await raw(target_id, segments)
        except ProtocolError as e:
            if e.refused:
                track.fingerprints.pop(fp, None)  # provably not delivered
            raise
        return res

    async def send_forward_message(
        self,
        scene: str,
        peer_id: int,
        nodes: list[list[dict]],
        *,
        title: str = "",
        preview: list[str] | None = None,
        summary: str = "",
        prompt: str = "",
    ) -> SendMessageResult:
        """Send a merged-forward chain (each node = a segment list)."""
        raise NotSupported("forward send not supported by this protocol side")

    async def send_file(
        self, scene: str, peer_id: int, path: str, name: str = ""
    ) -> str:
        """Send a real FILE (Milky outbound has no file segment; protocol
        sides provide an upload API). Returns a file id."""
        raise NotSupported("file send not supported by this protocol side")

    @abstractmethod
    async def get_history_messages(
        self,
        scene: str,
        peer_id: int,
        *,
        start_message_seq: int | None = None,
        limit: int = 30,
    ) -> HistoryResult:
        """Newest-to-older roaming pull. ``start_message_seq`` is a hint;
        adapters without it fall back to paging from the newest message."""

    @abstractmethod
    async def get_message(self, scene: str, peer_id: int, message_seq: int) -> IncomingMessage | None:
        ...

    # ---- media / files --------------------------------------------------
    @abstractmethod
    async def get_resource_url(self, resource_id: str) -> str: ...

    @abstractmethod
    async def get_private_file_url(self, user_id: int, file_id: str, file_hash: str) -> str: ...

    @abstractmethod
    async def get_group_file_url(self, group_id: int, file_id: str) -> str: ...

    async def get_forwarded_messages(self, forward_id: str) -> list[ForwardedMessage]:
        raise NotSupported("forwarded messages not supported by this protocol side")

    async def mark_read(self, scene: str, peer_id: int, message_seq: int) -> None:
        raise NotSupported("mark-as-read not supported by this protocol side")
