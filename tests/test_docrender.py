"""Document rendering (markdown -> PDF -> PNG): markdown detection,
byte-level output checks, user-font drop-in. The PyMuPDF Story engine
bundles CJK fallback fonts, so no font setup is required."""

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
    assert len(pdf) > 10000  # CJK font subset embedded


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
