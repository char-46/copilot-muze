"""Document rendering (markdown -> PDF -> PNG): font resolution, md
detection, byte-level output checks. Downloads are disabled so the suite
never pulls the 24MB LXGW font; system fonts cover the render."""

import pytest

from muze import render as R

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def no_download(monkeypatch):
    monkeypatch.setattr(R, "_download", lambda url, dest: False)


def test_looks_markdown():
    assert R.looks_markdown("# 标题\n正文")
    assert R.looks_markdown("```python\nx=1\n```")
    assert not R.looks_markdown("普通文本一行。\n第二行。")


def test_resolve_fonts_system_fallback(tmp_path):
    fonts = R.resolve_fonts(tmp_path)
    assert "body" in fonts
    assert "mono" in fonts  # mono falls back to body


def test_render_pdf_bytes(tmp_path):
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    pdf = R.render_pdf("# 报告\n\n- 甲项\n- 乙项\n\n正文 **加粗**。", tmp_path)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 10000  # an embedded CJK subset is never tiny
    assert b"FontFile2" in pdf


def test_render_pdf_plain_text(tmp_path):
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    pdf = R.render_pdf("纯文本一段。\n第二行。", tmp_path, markdown=False)
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
