"""read_document format coverage: office docs, pdf, spreadsheets, archives."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from muze.docs import read_document


@pytest.fixture
def d(tmp_path):
    return tmp_path


def _make_xlsx(p: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "报价"
    ws.append(["名称", "单价"])
    ws.append(["苹果", 3])
    wb.save(p)


def _make_pptx(p: Path):
    from pptx import Presentation

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "项目汇报"
    s.placeholders[1].text = "进度过半"
    prs.save(p)


def _make_pdf(p: Path):
    from reportlab.pdfgen.canvas import Canvas

    c = Canvas(str(p))
    c.setFont("Helvetica", 12)
    c.drawString(72, 720, "invoice no 42")
    c.save()


def _make_7z(p: Path, d: Path):
    import shutil
    import subprocess

    backend = shutil.which("7z") or next(
        (c for c in (r"C:\Program Files\7-Zip\7z.exe",
                     r"C:\Program Files (x86)\7-Zip\7z.exe")
         if Path(c).is_file()), None)
    if not backend:
        pytest.skip("7-Zip not installed (7z/rar archive backend)")
    (d / "a.txt").write_text("七压缩正文", encoding="utf-8")
    r = subprocess.run([backend, "a", "-bso0", "-bse0", str(p), "a.txt"],
                       cwd=d, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[:200]


def test_xlsx(d):
    _make_xlsx(d / "t.xlsx")
    t = read_document(d / "t.xlsx")
    assert "报价" in t and "苹果" in t and "3" in t


def test_pdf(d):
    _make_pdf(d / "t.pdf")
    assert "invoice no 42" in read_document(d / "t.pdf")


def test_pptx(d):
    _make_pptx(d / "t.pptx")
    t = read_document(d / "t.pptx")
    assert "项目汇报" in t and "进度过半" in t


def test_rtf_sniffed_doc(d):
    (d / "t.doc").write_bytes(b"{\\rtf1\\par RTF body line one")
    assert "RTF body" in read_document(d / "t.doc")


def test_ole_doc_crude_extract(d):
    (d / "t.doc").write_bytes(
        b"\xd0\xcf\x11\xe0" + ("旧Word文档正文测试".encode("utf-16-le") * 8)
    )
    assert "旧Word文档正文测试" in read_document(d / "t.doc")


def test_zip_listing_entry_and_binary_member(d):
    with zipfile.ZipFile(d / "a.zip", "w") as z:
        z.writestr("readme.txt", "压缩包说明文本")
        z.writestr("docs/note.md", "# 笔记\n内容")
    listing = read_document(d / "a.zip")
    assert "readme.txt" in listing and "压缩包说明文本" in listing  # preview shown
    entry = read_document(d / "a.zip", entry="readme.txt")
    assert entry.strip() == "压缩包说明文本"
    # suffix match for nested paths
    nested = read_document(d / "a.zip", entry="note.md")
    assert "笔记" in nested
    # a binary member renders through the document pipeline
    _make_pdf(d / "in.pdf")
    with zipfile.ZipFile(d / "b.zip", "w") as z:
        z.write(d / "in.pdf", "report.pdf")
    out = read_document(d / "b.zip", entry="report.pdf")
    assert "invoice" in out


def test_sevenzip_entry(d):
    _make_7z(d / "t.7z", d)
    assert "七压缩正文" in read_document(d / "t.7z", entry="a.txt")
    assert "a.txt" in read_document(d / "t.7z")  # listing


def test_archive_missing_entry_error(d):
    with zipfile.ZipFile(d / "a.zip", "w") as z:
        z.writestr("x.txt", "内容")
    with pytest.raises(ValueError):
        read_document(d / "a.zip", entry="不存在.txt")


def test_read_document_tool_entry_passthrough(d):
    """The agent tool forwards `entry` (tool schema regression)."""
    with zipfile.ZipFile(d / "a.zip", "w") as z:
        z.writestr("k.txt", "通过工具读取")
    from muze.docs import read_document as rd

    assert "通过工具读取" in rd(str(d / "a.zip"), "k.txt")
