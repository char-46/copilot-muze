"""Document rendering: markdown text -> styled PDF -> PNG image.

Used for two outbound reply forms:
- long QQ replies get their markdown formatting preserved as a rendered image
  (QQ plain text carries no formatting);
- /topdf & /sess getpdf export documents as PDF files.

Pipeline: markdown -> HTML (python-markdown) -> flowables (reportlab
Platypus; a user TTF from data/fonts/ wins, else a system CJK TTF is
subset-embedded on Windows, else the non-embedded STSong-Light CID face) ->
PDF -> PNG (pypdfium2 raster, pages stitched vertically with PIL).

Every rendering dependency here is permissively licensed (BSD-3 /
Apache-2.0); the former PyMuPDF (AGPL) pipeline was retired during the
CC BY-SA 4.0 relicensing.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger("muze.render")

try:  # pragma: no cover - trivial import guard
    import markdown as _md
    import pypdfium2 as pdfium
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Table,
        TableStyle,
        XPreformatted,
    )

    RENDER_OK = True
except Exception:  # pragma: no cover
    _md = pdfium = None
    RENDER_OK = False

# user-dropped font files win (first pattern match is used, any role)
_LOCAL_PATTERNS = [
    "maplemono*cn*regular*.ttf", "maplemono*-regular*.ttf", "maplemono*.ttf",
    "lxgwwenkai*.ttf", "lxgw*.ttf", "wenkai*.ttf",
]


def find_user_font(font_dir: Path | None, override: str = "") -> Path | None:
    """Explicit override file, else a dropped-in font under font_dir."""
    if override:
        p = Path(override)
        if p.is_file():
            return p
        log.warning("render_font %s not found, using built-in fonts", override)
    d = Path(font_dir) if font_dir else None
    if not d or not d.is_dir():
        return None
    files = {p.name.lower(): p for p in d.glob("*.ttf")}
    for pat in _LOCAL_PATTERNS:
        for name, p in files.items():
            if re.fullmatch(pat.replace(".", r"\.").replace("*", ".*"), name):
                return p
    return None


# ------------------------------------------------------------------- html
_MD_EXTENSIONS = ["fenced_code", "tables", "nl2br", "sane_lists"]


def markdown_to_html(text: str) -> str:
    return _md.markdown(text or "", extensions=_MD_EXTENSIONS)


_MD_HINTS = [
    re.compile(r"```"),                          # fenced code
    re.compile(r"^#{1,6}\s+\S", re.M),           # headings
    re.compile(r"\*\*[^*\n]+\*\*"),              # bold
    re.compile(r"`[^`\n]+`"),                    # inline code
    re.compile(r"\[[^\]\n]+\]\(https?://[^)\s]+\)"),  # links
    re.compile(r"^>\s?\S", re.M),                # blockquote
    re.compile(r"^\s*[-*]\s+\S", re.M),          # bullet list
    re.compile(r"^\|.*\|", re.M),                # table row
    re.compile(r"^(-{3,}|\*{3,})$", re.M),       # hr
]


def looks_markdown(text: str) -> bool:
    """Heuristic: does this text carry markdown worth rendering?"""
    if not text:
        return False
    return any(rx.search(text) for rx in _MD_HINTS)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ fonts
# pick order: user TTF -> system CJK TTF (Windows) -> STSong-Light CID face
# (non-embedded; rasterizers substitute a local CJK font) -> Helvetica.
_SYS_TTF = [
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simkai.ttf",
    r"C:\Windows\Fonts\simfang.ttf",
]
_font_cache: dict[str, str] = {}  # key (font path or "") -> registered name


def _pick_font(font: Path | None) -> str:
    key = str(font.resolve()) if font is not None else ""
    if key in _font_cache:
        return _font_cache[key]
    src = font
    if src is None and sys.platform == "win32":
        src = next((Path(c) for c in _SYS_TTF if os.path.isfile(c)), None)
    name = None
    if src is not None:
        try:
            pdfmetrics.registerFont(TTFont("muzefont", str(src)))
            name = "muzefont"
        except Exception as e:
            log.warning("font %s unusable (%s); trying CID fallback", src.name, e)
    if name is None:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            name = "STSong-Light"
        except Exception:  # pragma: no cover - reportlab always has CID fonts
            name = "Helvetica"
    _font_cache[key] = name
    return name


def _styles(f: str) -> dict[str, ParagraphStyle]:
    kw = dict(fontName=f, fontSize=12, leading=18, wordWrap="CJK")
    body = ParagraphStyle("body", spaceAfter=6, **kw)
    return {
        "body": body,
        "h1": ParagraphStyle("h1", parent=body, fontSize=19, leading=28,
                             spaceBefore=16, spaceAfter=8),
        "h2": ParagraphStyle("h2", parent=body, fontSize=16, leading=24,
                             spaceBefore=12, spaceAfter=6),
        "h3": ParagraphStyle("h3", parent=body, fontSize=14, leading=21,
                             spaceBefore=10, spaceAfter=5),
        "quote": ParagraphStyle("quote", parent=body, leftIndent=16,
                                textColor=HexColor("#444444")),
        "code": ParagraphStyle("code", parent=body, fontSize=11, leading=16,
                               backColor=HexColor("#f0f0f0"), borderPadding=6,
                               leftIndent=6, spaceBefore=4, spaceAfter=8),
        "li0": ParagraphStyle("li0", parent=body, leftIndent=20, bulletIndent=4),
        "cell": ParagraphStyle("cell", parent=body, fontSize=11, leading=16,
                               spaceAfter=0),
    }


# ------------------------------------------------- html -> reportlab markup
def _esc_attr(text: str) -> str:
    return _esc(text).replace('"', "&quot;")


def _inline(el) -> str:
    """Element -> reportlab Paragraph inline markup (b/i/font/a/br)."""
    tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
    text = _esc(el.text or "")
    kids = "".join(_inline(c) for c in el)
    if tag in ("strong", "b"):
        body = f"<b>{text}{kids}</b>"
    elif tag in ("em", "i"):
        body = f"<i>{text}{kids}</i>"
    elif tag == "code":
        body = f"<font>{text}{kids}</font>"
    elif tag == "br":
        body = "<br/>"
    elif tag == "a":
        href = _esc_attr(el.get("href", ""))
        body = f'<a href="{href}" color="#1155cc">{text}{kids}</a>'
    elif tag in ("img",):  # images are not supported in replies
        body = ""
    else:
        body = f"{text}{kids}"
    return body + _esc(el.tail or "")


def _table_flowable(el, st: dict) -> Table | None:
    rows: list[list[Paragraph]] = []
    for tr in el.iter("tr"):
        row = [Paragraph(_inline(c), st["cell"])
               for c in tr if c.tag in ("td", "th")]
        if row:
            rows.append(row)
    if not rows:
        return None
    usable = A4[0] - 80 - 12
    colw = [usable / len(rows[0])] * len(rows[0])
    t = Table(rows, colWidths=colw, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#eeeeee")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _list_items(el, st: dict, depth: int, ordered: bool) -> list:
    out: list = []
    idx = 0
    for li in el:
        if not isinstance(li.tag, str) or li.tag.split("}")[-1] != "li":
            continue
        idx += 1
        parts = [_esc(li.text or "")]
        nested: list = []
        for c in li:
            ctag = c.tag.split("}")[-1] if isinstance(c.tag, str) else ""
            if ctag in ("ul", "ol"):
                nested.extend(_list_items(c, st, depth + 1, ctag == "ol"))
            else:
                parts.append(_inline(c))
        style = ParagraphStyle(
            f"li{depth}", parent=st["li0"],
            leftIndent=st["li0"].leftIndent + depth * 16,
            bulletIndent=st["li0"].bulletIndent + depth * 16,
        )
        out.append(Paragraph("".join(parts), style,
                             bulletText=f"{idx}." if ordered else "\u2022"))
        out.extend(nested)
    return out


def _flowables(el, st: dict) -> list:
    out: list = []
    for c in el:
        tag = c.tag.split("}")[-1] if isinstance(c.tag, str) else ""
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append(Paragraph(_inline(c), st.get(tag, st["h3"])))
        elif tag == "p":
            out.append(Paragraph(_inline(c), st["body"]))
        elif tag in ("ul", "ol"):
            out.extend(_list_items(c, st, 0, tag == "ol"))
        elif tag == "pre":
            out.append(XPreformatted("".join(c.itertext()) or " ", st["code"]))
        elif tag == "blockquote":
            for sub in c:
                stag = sub.tag.split("}")[-1] if isinstance(sub.tag, str) else ""
                if stag == "p":
                    out.append(Paragraph(_inline(sub), st["quote"]))
                elif stag in ("ul", "ol"):
                    out.extend(_list_items(sub, st, 0, stag == "ol"))
                else:
                    txt = _inline(sub) if stag else _esc(sub.text or "")
                    if txt:
                        out.append(Paragraph(txt, st["quote"]))
        elif tag == "table":
            t = _table_flowable(c, st)
            if t is not None:
                out.append(t)
        elif tag == "hr":
            out.append(HRFlowable(width="100%", thickness=0.6,
                                  color=HexColor("#bbbbbb"),
                                  spaceBefore=8, spaceAfter=8))
        elif tag == "div":
            out.extend(_flowables(c, st))
        else:
            txt = _inline(c)
            if txt:
                out.append(Paragraph(txt, st["body"]))
    return out


def _flow_pdf(html_body: str, font: Path | None) -> bytes:
    f = _pick_font(font)
    st = _styles(f)
    try:
        root = ET.fromstring(f"<muzebody>{html_body}</muzebody>")
        story = _flowables(root, st)
    except ET.ParseError as e:  # raw non-XHTML html slipped through
        log.warning("html parse failed (%s); rendering as plain text", e)
        story = [Paragraph(_esc(html_body), st["body"])]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=40, rightMargin=40,
                            topMargin=40, bottomMargin=40,
                            title="muze", author="muze")
    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------- document
def render_pdf(
    text: str,
    font_dir: Path | None = None,
    *,
    markdown: bool | None = None,
    font_override: str = "",
) -> bytes:
    """Markdown/plain text -> PDF bytes. ``markdown=None`` auto-detects."""
    if not RENDER_OK:
        raise RuntimeError("rendering dependencies unavailable")
    if markdown is None:
        markdown = looks_markdown(text)
    if markdown:
        html_body = markdown_to_html(text)
    else:
        html_body = "<p>" + _esc(text or "").replace("\n", "<br />") + "</p>"
    font = find_user_font(font_dir, font_override)
    return _flow_pdf(html_body, font)


def render_png(
    text: str,
    font_dir: Path | None = None,
    *,
    markdown: bool | None = None,
    scale: float = 2.0,
    max_pages: int = 8,
    font_override: str = "",
) -> bytes | None:
    """Text -> single tall PNG (A4 pages stitched vertically). None = failed."""
    if not RENDER_OK:
        return None
    try:
        pdf = render_pdf(text, font_dir, markdown=markdown, font_override=font_override)
        doc = pdfium.PdfDocument(pdf)
        n = min(len(doc), max_pages)
        if n <= 0:
            return None
        s = max(1.0, scale)
        imgs = [doc[i].render(scale=s).to_pil().convert("RGB") for i in range(n)]
        doc.close()
        if len(imgs) == 1:
            out = imgs[0]
        else:
            from PIL import Image

            w = max(im.width for im in imgs)
            h = sum(im.height for im in imgs)
            out = Image.new("RGB", (w, h), "white")
            y = 0
            for im in imgs:
                out.paste(im, (0, y))
                y += im.height
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.warning("png render failed: %s", e)
        return None


def png_cache_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]
