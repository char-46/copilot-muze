from muze.provider.milky.models import Event, IncomingMessage, parse_segments


def test_parse_known_segments():
    segs = parse_segments(
        [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "mention", "data": {"user_id": 123, "name": "foo"}},
            {"type": "image", "data": {"resource_id": "r1", "temp_url": "http://x", "width": 10, "height": 20, "summary": "一只猫"}},
            {"type": "reply", "data": {"message_seq": 9, "segments": [{"type": "text", "data": {"text": "quoted"}}]}},
        ]
    )
    assert segs[0].type == "text" and segs[0].text == "hello"
    assert segs[1].user_id == 123
    assert segs[2].width == 10
    assert segs[3].message_seq == 9
    assert segs[3].segments[0].text == "quoted"


def test_parse_unknown_segment_degrades():
    segs = parse_segments([{"type": "future_thing", "data": {"x": 1}}])
    assert segs[0].type == "future_thing"
    assert getattr(segs[0], "raw", {}).get("data") == {"x": 1}


def test_event_message_receive_roundtrip():
    ev = Event.from_raw(
        {
            "time": 1700000000,
            "self_id": 42,
            "event_type": "message_receive",
            "data": {
                "message_scene": "group",
                "peer_id": 777,
                "message_seq": 5,
                "sender_id": 9,
                "time": 1700000000,
                "segments": [{"type": "text", "data": {"text": "hi"}}],
            },
        }
    )
    assert ev.event_type == "message_receive"
    msg: IncomingMessage = ev.data
    assert msg.peer_id == 777 and msg.segments[0].text == "hi"


def test_event_unknown_type_tolerated():
    ev = Event.from_raw({"time": 1, "self_id": 2, "event_type": "brand_new_event", "data": {"z": 1}})
    assert ev.event_type == "brand_new_event"


def test_message_from_raw_keeps_unknown_segments():
    m = IncomingMessage.from_raw(
        {
            "message_scene": "friend",
            "peer_id": 1,
            "message_seq": 2,
            "sender_id": 3,
            "time": 4,
            "segments": [{"type": "text", "data": {"text": "a"}}, {"type": "wat", "data": {}}],
        }
    )
    assert len(m.segments) == 2 and m.segments[1].type == "wat"


def test_llbot_field_aliases():
    """LLBot returns uin/nickname/remark/card instead of the spec names."""
    from muze.provider.milky.models import Friend, GroupMember, LoginInfo

    li = LoginInfo.model_validate({"uin": 1000000002, "nickname": "我"})
    assert li.user_id == 1000000002 and li.user_name == "我"

    f = Friend.model_validate({"user_id": 5, "nickname": "雪貂", "remark": "雪宝"})
    assert f.user_name == "雪貂" and f.user_remark == "雪宝" and f.display == "雪宝"

    m = GroupMember.model_validate({"user_id": 7, "nickname": "小明", "card": "群名片"})
    assert m.user_name == "小明" and m.card_name == "群名片" and m.display == "群名片"

    spec = LoginInfo.model_validate({"user_id": 9, "user_name": "x"})
    assert spec.user_id == 9 and spec.user_name == "x"
