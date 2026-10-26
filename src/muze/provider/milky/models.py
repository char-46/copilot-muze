"""Milky protocol models (per https://milky.ntqqrev.org/, v1.3 subset).

These double as the canonical app-side data shapes. Unknown segment and event
types degrade gracefully per the spec's compatibility guidance.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SkipValidation, TypeAdapter

Scene = Literal["friend", "group", "temp"]


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- segments
class TextSeg(_M):
    type: Literal["text"] = "text"
    text: str = ""


class MentionSeg(_M):
    type: Literal["mention"] = "mention"
    user_id: int = 0
    name: str = ""


class MentionAllSeg(_M):
    type: Literal["mention_all"] = "mention_all"


class FaceSeg(_M):
    type: Literal["face"] = "face"
    face_id: str = ""
    is_large: bool = False


class ReplySeg(_M):
    type: Literal["reply"] = "reply"
    message_seq: int = 0
    sender_id: int | None = None
    sender_name: str | None = None
    time: int | None = None
    segments: SkipValidation[list[Segment]] = Field(default_factory=list)


class ImageSeg(_M):
    type: Literal["image"] = "image"
    resource_id: str = ""
    temp_url: str = ""
    width: int = 0
    height: int = 0
    summary: str = ""
    sub_type: str = "normal"


class RecordSeg(_M):
    type: Literal["record"] = "record"
    resource_id: str = ""
    temp_url: str = ""
    duration: int = 0


class VideoSeg(_M):
    type: Literal["video"] = "video"
    resource_id: str = ""
    temp_url: str = ""
    width: int = 0
    height: int = 0
    duration: int = 0


class FileSeg(_M):
    type: Literal["file"] = "file"
    file_id: str = ""
    file_name: str = ""
    file_size: int = 0
    file_hash: str | None = None


class ForwardSeg(_M):
    type: Literal["forward"] = "forward"
    forward_id: str = ""
    title: str = ""
    preview: list[str] = Field(default_factory=list)
    summary: str = ""


class MarketFaceSeg(_M):
    type: Literal["market_face"] = "market_face"
    emoji_package_id: int = 0
    emoji_id: str = ""
    key: str = ""
    summary: str = ""
    url: str = ""


class LightAppSeg(_M):
    type: Literal["light_app"] = "light_app"
    app_name: str = ""
    json_payload: str = ""


class XmlSeg(_M):
    type: Literal["xml"] = "xml"
    service_id: int = 0
    xml_payload: str = ""


class MarkdownSeg(_M):
    type: Literal["markdown"] = "markdown"
    content: str = ""


class UnknownSeg(_M):
    """Per spec: unknown segment types degrade to text with a notice."""

    type: str
    raw: dict[str, Any] = Field(default_factory=dict)


# Discriminated union over the known segment types (spec v1.3).
KnownSegment = Annotated[
    TextSeg
    | MentionSeg
    | MentionAllSeg
    | FaceSeg
    | ReplySeg
    | ImageSeg
    | RecordSeg
    | VideoSeg
    | FileSeg
    | ForwardSeg
    | MarketFaceSeg
    | LightAppSeg
    | XmlSeg
    | MarkdownSeg,
    Field(discriminator="type"),
]

# App-side segment value: a known segment or an UnknownSeg fallback.
# Fields typed with Segment must use SkipValidation; parsing goes through
# parse_segments(), which degrades unknown types per the spec.
Segment = Union[
    TextSeg,
    MentionSeg,
    MentionAllSeg,
    FaceSeg,
    ReplySeg,
    ImageSeg,
    RecordSeg,
    VideoSeg,
    FileSeg,
    ForwardSeg,
    MarketFaceSeg,
    LightAppSeg,
    XmlSeg,
    MarkdownSeg,
    UnknownSeg,
]

_segment_item = TypeAdapter(KnownSegment)


def parse_segments(raw: Any) -> list[Segment]:
    """Accept both wire shape ({type, data:{...}}) and flat shape (model_dump)."""
    if not isinstance(raw, list):
        return []
    out: list[Segment] = []
    for item in raw:
        if not isinstance(item, dict):
            out.append(UnknownSeg(type="unknown", raw={}))
            continue
        candidate = item
        if isinstance(item.get("data"), dict) and "type" in item:
            candidate = {"type": item["type"], **item["data"]}
        try:
            parsed = _segment_item.validate_python(candidate)
            if isinstance(parsed, ReplySeg) and parsed.segments and isinstance(parsed.segments[0], dict):
                parsed.segments = parse_segments(parsed.segments)
            out.append(parsed)
        except Exception:
            t = item.get("type", "unknown")
            out.append(UnknownSeg(type=str(t), raw=item))
    return out


# ---------------------------------------------------------------- message
class IncomingMessage(_M):
    message_scene: Scene = "friend"
    peer_id: int = 0
    message_seq: int = 0
    sender_id: int = 0
    time: int = 0
    is_self: bool = False  # outgoing (sent by this account from another client)
    segments: SkipValidation[list[Segment]] = Field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "IncomingMessage":
        data = dict(raw)
        if isinstance(data.get("segments"), list):
            data["segments"] = parse_segments(data["segments"])
        return cls.model_validate(data)


class ForwardedMessage(_M):
    sender_id: int = 0
    sender_name: str = ""
    time: int = 0
    segments: SkipValidation[list[Segment]] = Field(default_factory=list)


# ---------------------------------------------------------------- entities
# Field aliases: the spec says user_name/user_remark/card_name, but real
# implementations (LLBot) return nickname/remark/card — accept both.
class Friend(_M):
    user_id: int = 0
    user_name: str = Field(
        default="", validation_alias=AliasChoices("user_name", "nickname")
    )
    user_remark: str = Field(
        default="", validation_alias=AliasChoices("user_remark", "remark")
    )

    @property
    def display(self) -> str:
        return self.user_remark or self.user_name or str(self.user_id)


class FriendCategory(_M):
    category_name: str = ""
    friends: list[Friend] = Field(default_factory=list)


class Group(_M):
    group_id: int = 0
    group_name: str = ""


class GroupMember(_M):
    user_id: int = 0
    user_name: str = Field(
        default="", validation_alias=AliasChoices("user_name", "nickname")
    )
    card_name: str = Field(
        default="", validation_alias=AliasChoices("card_name", "card")
    )

    @property
    def display(self) -> str:
        return self.card_name or self.user_name or str(self.user_id)


# ---------------------------------------------------------------- results
class SendMessageResult(_M):
    message_seq: int = 0
    time: int = 0


class HistoryResult(_M):
    messages: list[IncomingMessage] = Field(default_factory=list)
    next_message_seq: int | None = None


class LoginInfo(_M):
    user_id: int = Field(
        default=0, validation_alias=AliasChoices("user_id", "uin")
    )
    user_name: str = Field(
        default="", validation_alias=AliasChoices("user_name", "nickname")
    )
    user_remark: str = ""


# ---------------------------------------------------------------- events
class MessageEventData(IncomingMessage):
    pass


class BotOfflineData(_M):
    reason: str = ""


class MessageRecallData(_M):
    message_scene: Scene = "friend"
    peer_id: int = 0
    message_seq: int = 0
    sender_id: int = 0
    operator_id: int = 0
    display_suffix: str = ""


class FriendFileUploadData(_M):
    user_id: int = 0
    file_id: str = ""
    file_name: str = ""
    file_size: int = 0
    file_hash: str | None = None
    is_self: bool = False


class GroupFileUploadData(_M):
    group_id: int = 0
    user_id: int = 0
    file_id: str = ""
    file_name: str = ""
    file_size: int = 0


class FriendRequestData(_M):
    initiator_id: int = 0
    comment: str = ""
    via: str = ""


class GenericEventData(_M):
    model_config = ConfigDict(extra="allow")
    data: dict[str, Any] = Field(default_factory=dict)


class Event(_M):
    time: int = 0
    self_id: int = 0
    event_type: str = "unknown"
    data: Any = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Event":
        et = str(raw.get("event_type", "unknown"))
        data = raw.get("data") or {}
        typed: Any
        if et == "message_receive":
            typed = MessageEventData.from_raw(data)
        elif et == "message_send":
            # Some Milky implementations report the account's own outgoing
            # messages (typed on another client, e.g. the owner's phone) as
            # this separate event type. Archive them exactly like receive
            # events -- is_self=True keeps them off the control-trigger path
            # (LLBot currently reuses message_receive for these, which is why
            # self replies already land in the archive in real time).
            typed = MessageEventData.from_raw({**data, "is_self": True})
        elif et == "bot_offline":
            typed = BotOfflineData.model_validate(data)
        elif et == "message_recall":
            typed = MessageRecallData.model_validate(data)
        elif et == "friend_file_upload":
            typed = FriendFileUploadData.model_validate(data)
        elif et == "group_file_upload":
            typed = GroupFileUploadData.model_validate(data)
        elif et == "friend_request":
            typed = FriendRequestData.model_validate(data)
        else:
            typed = GenericEventData(data=data)
        return cls(
            time=int(raw.get("time") or 0),
            self_id=int(raw.get("self_id") or 0),
            event_type=et,
            data=typed,
        )


# -------------------------------------------------------------- outgoing
def out_text(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def out_mention(user_id: int) -> dict:
    return {"type": "mention", "data": {"user_id": user_id}}


def out_reply(message_seq: int) -> dict:
    return {"type": "reply", "data": {"message_seq": message_seq}}


def out_image(file_uri: str) -> dict:
    # Milky outbound media field is "uri" (spec + LLBot-verified); the old
    # "file_uri" key made LLBot reject the WHOLE message (retcode -400)
    return {"type": "image", "data": {"uri": file_uri}}


def out_record(file_uri: str) -> dict:
    return {"type": "record", "data": {"uri": file_uri}}


def out_video(file_uri: str) -> dict:
    return {"type": "video", "data": {"uri": file_uri}}


def out_file(file_uri: str, file_name: str = "") -> dict:
    """Internal shape only — Milky outbound has NO file segment; real file
    delivery goes through upload_private_file / upload_group_file (see
    MilkyAdapter.send_file)."""
    data: dict = {"file_uri": file_uri}
    if file_name:
        data["file_name"] = file_name
    return {"type": "file", "data": data}


def out_forward(
    nodes: list[list[dict]],
    *,
    user_id: int = 0,
    sender_name: str = "muze",
    title: str = "",
    preview: list[str] | None = None,
    summary: str = "",
    prompt: str = "",
) -> dict:
    """Merged-forward segment (Milky v1.2+). ``nodes`` = segment lists, one
    per chain message; optional title/preview/summary/prompt shape the chat
    bubble (preview 1..4 lines)."""
    import time as _time

    messages = [
        {
            "user_id": user_id,
            "sender_name": sender_name,
            "time": int(_time.time()),
            "segments": segs,
        }
        for segs in nodes
    ]
    data: dict = {"messages": messages}
    if title:
        data["title"] = title
    if preview:
        data["preview"] = [p for p in preview if p][:4]
    if summary:
        data["summary"] = summary
    if prompt:
        data["prompt"] = prompt
    return {"type": "forward", "data": data}
