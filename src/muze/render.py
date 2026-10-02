"""Document rendering: markdown text -> styled PDF -> PNG image.

Used for two outbound reply forms:
- long QQ replies get their markdown formatting preserved as a rendered image
  (QQ plain text carries no formatting);
- /topdf & /sess getpdf export documents as PDF files.

Pipeline: markdown -> HTML (python-markdown) -> PDF (PyMuPDF Story, the C
MuPDF layout engine with bundled CJK fallback fonts — no font setup needed,
~30ms per page) -> PNG (PyMuPDF raster, pages stitched vertically with PIL).

A user font (Maple Mono / LXGW WenKai / any .ttf dropped into data/fonts/)
still wins: it is passed to Story via an Archive + @font-face.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from pathlib import Path

log = logging.getLogger("muze.render")

try:  # pragma: no cover - trivial import guard
    import markdown as _md
    import pymupdf

    RENDER_OK = True
except Exception:  # pragma: no cover
    _md = pymupdf = None
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


_BASE_CSS = """
body { font-size: 12px; }
h1 { font-size: 1.6em; font-weight: bold; }
h2 { font-size: 1.35em; font-weight: bold; }
h3 { font-size: 1.15em; font-weight: bold; }
pre { background-color: #f0f0f0; font-family: sans-serif; }
code { font-family: sans-serif; }
blockquote { color: #444444; margin-left: 1em; }
"""


def _story_pdf(html_body: str, font: Path | None) -> bytes:
    css = _BASE_CSS
    arch = None
    if font is not None:
        css = (
            f"@font-face {{ font-family: muzefont; src: url({font.name}); }}\n"
            "body { font-family: muzefont; font-size: 12px; }\n"
            "h1 { font-size: 1.6em; font-weight: bold; }\n"
            "h2 { font-size: 1.35em; font-weight: bold; }\n"
            "h3 { font-size: 1.15em; font-weight: bold; }\n"
            "pre { background-color: #f0f0f0; font-family: muzefont; }\n"
            "code { font-family: muzefont; }\n"
            "blockquote { color: #444444; margin-left: 1em; }\n"
        )
        arch = pymupdf.Archive(str(font.parent))
    story = pymupdf.Story(html=f"<html><body>{html_body}</body></html>",
                          user_css=css, archive=arch)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    mediabox = pymupdf.paper_rect("a4")
    where = mediabox + (40, 40, -40, -40)
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
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
        html_body = "<p>" + _esc(text or "").replace("\n", "<br>") + "</p>"
    font = find_user_font(font_dir, font_override)
    pdf = _story_pdf(html_body, font)
    try:  # shrink the embedded fallback font (no-op without fonttools)
        doc = pymupdf.Document(stream=pdf, filetype="pdf")
        doc.subset_fonts()
        return doc.tobytes()
    except Exception:
        return pdf


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
        doc = pymupdf.Document(stream=pdf, filetype="pdf")
        n = min(len(doc), max_pages)
        if n <= 0:
            return None
        dpi = int(72 * max(1.0, scale))
        imgs = [doc[i].get_pixmap(dpi=dpi).pil_image() for i in range(n)]
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
