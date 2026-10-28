"""Document rendering (markdown -> PDF -> PNG): markdown detection,
byte-level output checks, user-font drop-in. reportlab subset-embeds the
user/system CJK TTF (STSong-Light CID fallback is not embedded, only
referenced), so PDF sizes vary by font path."""

import shutil
from pathlib import Path

import pytest

from muze import render as R


def test_looks_markdown():
    assert R.looks_markdown("# 标题\n正文")
    assert R.looks_markdown("```python\nx=1\n```")
    assert not R.looks_markdown("普通文本一行。\n第二行。")


def test_find_user_font(tmp_path):
    assert R.find_user_font(None) is None
    assert R.find_user_font(tmp_path) is None  # empty dir
    f = tmp_path / "LxgwWenKai-Regular.ttf"
    shutil.copy(r"C:\Windows\Fonts\Deng.ttf", f)
    assert R.find_user_font(tmp_path) == f
    assert R.find_user_font(None, override=str(f)) == f


def test_render_pdf_bytes(tmp_path):
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    pdf = R.render_pdf("# 报告\n\n- 甲项\n- 乙项\n\n正文 **加粗**。", tmp_path)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 2000  # real content (TTF subset ~tens of KB, CID ~3KB)


def test_render_pdf_plain_text(tmp_path):
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    pdf = R.render_pdf("纯文本一段。\n第二行。", tmp_path, markdown=False)
    assert pdf[:5] == b"%PDF-"


def test_render_pdf_user_font(tmp_path):
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    (tmp_path / "MapleMono-NF-CN-Regular.ttf").write_bytes(
        Path(r"C:\Windows\Fonts\Deng.ttf").read_bytes()
    )
    pdf = R.render_pdf("# 用自定义字体\n\n正文内容。", tmp_path)
    assert pdf[:5] == b"%PDF-"


def test_render_png_stitched(tmp_path):
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    md = "# 长文\n\n" + "\n\n".join(f"## 第{i}节\n\n内容内容内容" for i in range(120))
    png = R.render_png(md, tmp_path)
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_png_none_when_deps_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RENDER_OK", False)
    assert R.render_png("x", tmp_path) is None

def test_render_png_has_no_paper_margins(tmp_path):
    """User feedback 2026-10-24: the rendered image must be sized to the
    content, not to an A4 sheet (which framed it in a wide white border)."""
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    import io as _io

    from PIL import Image

    png = R.render_png("短句", tmp_path)
    assert png is not None
    im = Image.open(_io.BytesIO(png)).convert("RGB")
    # A4 at scale 2 would be ~1190px wide; a content-sized crop is far narrower
    assert im.width < 500
    # content sits inside ONE consistent outer border (user feedback
    # 2026-10-28: 输出的图片增加总体的 padding) -- not a paper sheet, and
    # not flush against the edge either
    bg = Image.new("RGB", im.size, "white")
    from PIL import ImageChops

    bbox = ImageChops.difference(im, bg).getbbox()
    assert bbox is not None
    l, t, r, b = bbox
    assert l >= R._PNG_OUTER_PAD - 2 and t >= R._PNG_OUTER_PAD - 2
    assert im.width - r >= R._PNG_OUTER_PAD - 2
    assert im.height - b >= R._PNG_OUTER_PAD - 2

def test_render_survives_angle_brackets(tmp_path):
    """Regression (2026-10-26): '/done <id|关键词>' used to break the XHTML
    parse and silently degrade the whole document to plain text."""
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    from muze.render import markdown_to_html

    html = markdown_to_html("# 标题\n\n/done <id|关键词> 标记完成\n\n代码 `List<int>` 不受影响")
    assert "&lt;id|关键词&gt;" in html          # stray brackets escaped
    assert "List&lt;int&gt;" in html            # inline code still fine
    pdf = R.render_pdf("# 待办\n\n/done <id|关键词> 标记完成", tmp_path)
    assert pdf[:5] == b"%PDF-"                  # full pipeline, no fallback
