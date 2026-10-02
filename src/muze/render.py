"""Document rendering: markdown text -> styled PDF -> PNG image.

Used for two outbound reply forms:
- long QQ replies get their markdown formatting preserved as a rendered image
  (QQ plain text carries no formatting);
- /topdf & /sess getpdf export documents as PDF files.

Pipeline: markdown -> HTML (python-markdown) -> PDF (xhtml2pdf, fonts
embedded as base64 @font-face — path/URL font loading is unreliable on
Windows) -> PNG (pypdfium2 raster, pages stitched vertically).

Fonts: user-droppable data/fonts/ wins (Maple Mono* or LxgwWenKai*), else
LXGW WenKai is downloaded there once (GitHub direct TTF links), else system
CJK fonts. PDF is the intermediate of the PNG path, so a future PDF export
needs no extra plumbing (render_pdf is exposed directly).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import urllib.request
from pathlib import Path

log = logging.getLogger("muze.render")

# deps are optional at import time (rendering degrades to "no image")
try:  # pragma: no cover - trivial import guard
    import markdown as _md
    import pypdfium2 as _pdfium
    from PIL import Image
    from xhtml2pdf import pisa

    RENDER_OK = True
except Exception:  # pragma: no cover
    _md = _pdfium = Image = pisa = None
    RENDER_OK = False

# LXGW WenKai: open-source (OFL) CJK font, direct TTF release assets.
_LXGW_TAG = "v1.520"
_FONT_URLS = {
    "body": f"https://github.com/lxgw/LxgwWenKai/releases/download/{_LXGW_TAG}/LXGWWenKai-Regular.ttf",
    "mono": f"https://github.com/lxgw/LxgwWenKai/releases/download/{_LXGW_TAG}/LXGWWenKaiMono-Regular.ttf",
}
# user-dropped files win; first pattern match per role is used
_LOCAL_PATTERNS = {
    "body": ["maplemono*cn*regular*.ttf", "maplemono*-regular*.ttf", "maplemono*.ttf",
             "lxgwwenkalite*.ttf", "lxgwwenkai-regular*.ttf", "lxgwwenkai.ttf", "wenkai*.ttf"],
    "mono": ["maplemono*cn*regular*.ttf", "maplemono*-regular*.ttf", "maplemono*.ttf",
             "lxgwwenkaimono*.ttf"],
}
# last-resort system fonts (Windows-first, then common Linux/macOS)
_SYSTEM_CANDIDATES = [
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simfang.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

_bytes_cache: dict[str, bytes] = {}


# ------------------------------------------------------------------ fonts
def _download(url: str, dest: Path) -> bool:
    try:
        log.info("downloading font %s -> %s", url.split("/")[-1], dest)
        req = urllib.request.Request(url, headers={"User-Agent": "muze"})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        return dest.stat().st_size > 1_000_000
    except Exception as e:
        log.warning("font download failed (%s): %s", url, e)
        dest.unlink(missing_ok=True)
        return False


def _find_local(directory: Path, patterns: list[str]) -> Path | None:
    if not directory.is_dir():
        return None
    files = {p.name.lower(): p for p in directory.glob("*.ttf")}
    for pat in patterns:
        for name, p in files.items():
            if re.fullmatch(pat.replace(".", r"\.").replace("*", ".*"), name):
                return p
    return None


def resolve_fonts(font_dir: Path | None) -> dict[str, bytes]:
    """role -> font file bytes; roles: body, mono. Cached by path+mtime."""
    out: dict[str, bytes] = {}
    d = Path(font_dir) if font_dir else None
    for role in ("body", "mono"):
        src: Path | None = None
        if d is not None:
            src = _find_local(d, _LOCAL_PATTERNS[role])
            if src is None and role == "body" and out.get("body") is None:
                pass  # fall through to download below
        if src is None and d is not None and role in _FONT_URLS and RENDER_OK:
            dest = d / _FONT_URLS[role].split("/")[-1]
            if not dest.exists() and _download(_FONT_URLS[role], dest):
                pass
            if dest.exists():
                src = dest
        if src is None and role == "mono":
            src = None  # mono is optional: body covers code with CJK glyphs
        if src is None:
            continue
        key = str(src)
        blob = _bytes_cache.get(key)
        if blob is None:
            blob = Path(src).read_bytes()
            _bytes_cache[key] = blob
        out[role] = blob
    if "body" not in out:  # system fallback, same file for both roles
        for cand in _SYSTEM_CANDIDATES:
            p = Path(cand)
            if p.exists():
                blob = _bytes_cache.get(cand)
                if blob is None:
                    blob = p.read_bytes()
                    _bytes_cache[cand] = blob
                out["body"] = blob
                break
    if "mono" not in out and "body" in out:
        out["mono"] = out["body"]  # CJK-safe code font (mono if provided)
    return out


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
    hits = sum(1 for rx in _MD_HINTS if rx.search(text))
    return hits >= 1


def _b64_uri(blob: bytes) -> str:
    return "data:font/ttf;base64," + base64.b64encode(blob).decode()


def _css(fonts: dict[str, bytes]) -> str:
    body = fonts["body"]
    mono = fonts.get("mono") or body
    face = f"@font-face {{ font-family: muzefont; src: url({_b64_uri(body)}); }}"
    facem = f"@font-face {{ font-family: muzemono; src: url({_b64_uri(mono)}); }}"
    return face + facem + """
body { font-family: muzefont; font-size: 12px; line-height: 1.55; }
h1 { font-size: 18px; } h2 { font-size: 15px; } h3 { font-size: 13px; }
h1, h2, h3, h4 { -pdf-keep-with-next: true; }
p { margin: .35em 0; }
ul, ol { margin: .3em 0 .3em 0; }
li { margin: .1em 0; }
pre { font-family: muzemono; font-size: 10.5px; background-color: #f2f2f2;
      padding: 6px 8px; margin: .4em 0; }
code { font-family: muzemono; font-size: 10.5px; }
blockquote { color: #444444; margin: .4em 0 .4em 10px; }
table { margin: .4em 0; -pdf-keep-in-frame-mode: shrink; }
td, th { padding: 3px 8px; font-size: 11px; }
hr { margin: .5em 0; }
"""


# --------------------------------------------------------------- document
def render_pdf(text: str, font_dir: Path | None = None, *, markdown: bool | None = None) -> bytes:
    """Markdown/plain text -> PDF bytes. ``markdown=None`` auto-detects."""
    if not RENDER_OK:
        raise RuntimeError("rendering dependencies unavailable")
    fonts = resolve_fonts(font_dir)
    if "body" not in fonts:
        raise RuntimeError("no usable CJK font found (drop one into data/fonts/)")
    if markdown is None:
        markdown = looks_markdown(text)
    if markdown:
        html_body = markdown_to_html(text)
    else:
        html_body = "<p>" + _esc(text).replace("\n", "<br>") + "</p>"
    doc = (
        "<html><head><style>" + _css(fonts) + "</style></head><body>"
        + html_body + "</body></html>"
    )
    buf = io.BytesIO()
    rc = pisa.CreatePDF(io.StringIO(doc), dest=buf)
    if rc.err:
        raise RuntimeError(f"pdf render failed: {rc.err}")
    return buf.getvalue()


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render_png(
    text: str,
    font_dir: Path | None = None,
    *,
    markdown: bool | None = None,
    scale: float = 2.0,
    max_pages: int = 8,
) -> bytes | None:
    """Text -> single tall PNG (A4 pages stitched vertically). None = failed."""
    if not RENDER_OK:
        return None
    try:
        pdf = render_pdf(text, font_dir, markdown=markdown)
        doc = _pdfium.PdfDocument(pdf)
        n = min(len(doc), max_pages)
        imgs = [doc[i].render(scale=scale).to_pil() for i in range(n)]
        if not imgs:
            return None
        if len(imgs) == 1:
            out = imgs[0]
        else:
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
