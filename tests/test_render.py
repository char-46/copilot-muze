from muze.chatlog import plain_text, render_message, render_segment, size_fmt, _one_line
from muze.provider.milky.models import parse_segments


def segs(items):
    return parse_segments(items)


def test_render_text_and_media():
    s = segs(
        [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"resource_id": "r", "width": 640, "height": 480, "summary": "猫片"}},
            {"type": "file", "data": {"file_id": "f", "file_name": "report.pdf", "file_size": 2048}},
            {"type": "record", "data": {"resource_id": "r2", "duration": 7}},
        ]
    )
    line = " ".join(render_segment(x) for x in s)
    assert "看这个" in line and "猫片" in line and "report.pdf" in line and "2.0KB" in line and "7s" in line


def test_plain_text_mentions():
    s = segs(
        [
            {"type": "mention", "data": {"user_id": 5, "name": "张三"}},
            {"type": "text", "data": {"text": "你好"}},
            {"type": "mention_all", "data": {}},
        ]
    )
    assert plain_text(s) == "@张三 你好 @全体成员"


def test_render_message_one_line():
    m = parse_and_build()
    line = render_message(m, "李四")
    assert "\n" not in line and "\\n" in line
    assert "李四(10001)" in line and "group/2" in line


def parse_and_build():
    from muze.provider.milky.models import IncomingMessage

    return IncomingMessage.from_raw(
        {
            "message_scene": "group",
            "peer_id": 2,
            "message_seq": 3,
            "sender_id": 10001,
            "time": 1759000000,
            "segments": [{"type": "text", "data": {"text": "第一行\n第二行"}}],
        }
    )


def test_size_fmt():
    assert size_fmt(0) == "?B"
    assert size_fmt(10) == "10B"
    assert size_fmt(2048) == "2.0KB"
    assert size_fmt(5 * 1024 * 1024) == "5.0MB"


def test_one_line_escapes():
    assert _one_line("a\r\nb") == "a\\r\\nb"
    assert _one_line("x\ny") == "x\\ny"
