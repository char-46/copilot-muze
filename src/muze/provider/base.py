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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from muze.provider.milky.models import (
    Event,
    ForwardedMessage,
    Friend,
    Group,
    HistoryResult,
    IncomingMessage,
    LoginInfo,
    SendMessageResult,
)


class ProtocolError(RuntimeError):
    """A protocol-side API call failed."""


class NotSupported(ProtocolError):
    """The active adapter lacks this capability."""


@dataclass
class Caps:
    """Runtime-probed capabilities of the active protocol side."""

    friend_history: bool = False
    group_history: bool = False
    history_start_seq: bool = False
    incoming_file_segment: bool = False
    forwarded_messages: bool = False
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

    @abstractmethod
    async def get_group_member_name(self, group_id: int, user_id: int) -> str:
        """Best-effort display name for a group member (cached)."""

    async def get_friend_name(self, user_id: int) -> str:
        """Best-effort display name for a friend (cached). May hit network."""
        return ""

    # ---- messaging ----------------------------------------------------
    @abstractmethod
    async def send_private_message(
        self, user_id: int, segments: list[dict]
    ) -> SendMessageResult: ...

    @abstractmethod
    async def send_group_message(
        self, group_id: int, segments: list[dict]
    ) -> SendMessageResult: ...

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
