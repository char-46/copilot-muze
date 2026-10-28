"""Reply-image padding (user feedback 2026-10-28: 输出的图片增加总体的
padding -- the trimmed render kept only a few px around content and read
cramped inside a chat bubble)."""

from PIL import Image

from muze.render import _PNG_OUTER_PAD, _pad_white


def test_pad_white_adds_uniform_border():
    im = Image.new("RGB", (10, 20), "white")
    im.putpixel((0, 0), (0, 0, 0))
    out = _pad_white(im, 5)
    assert out.size == (20, 30)
    assert out.getpixel((5, 5)) == (0, 0, 0)      # content shifted inward
    assert out.getpixel((0, 0)) == (255, 255, 255)  # new border is white
    assert out.getpixel((19, 29)) == (255, 255, 255)


def test_pad_white_zero_is_identity():
    im = Image.new("RGB", (4, 4), "white")
    assert _pad_white(im, 0) is im


def test_outer_pad_is_meaningful():
    # a chat-bubble border that is actually visible, not the old 0/2px
    assert _PNG_OUTER_PAD >= 16


def test_chart_keeps_outer_margins(tmp_path):
    from muze.mediaops import make_chart

    p = make_chart(tmp_path, "近七天消息量", ["一", "二", "三"], [3, 1, 2])
    im = Image.open(p)
    assert im.size == (820, 480)
    # content (title text) is inset: the very top-left corner stays background
    assert im.getpixel((1, 1)) == (255, 255, 255)
