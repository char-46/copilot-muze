"""Document & URL ingestion for the agent.

- read_document(path): local files already archived by the media pipeline —
  .txt/.md/.log/.json raw, .docx via python-docx (paragraphs + tables),
  .html/.htm via tag stripping, best-effort fallback for others.
- fetch_url(url): plain HTTP GET; HTML is stripped to text. JS-rendered
  pages (金山文档/腾讯文档 web apps, some 小程序 landing pages) cannot be
  fully extracted this way — the caller is told so with actionable advice
  (the doc-sources skill explains how to dig URLs out of light_app payloads
  and what to do when a page needs login/JS).
"""

from __future__ import annotations

import html as htmllib
import logging
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("muze.docs")

MAX_DOC_CHARS = 20000
MAX_FETCH_BYTES = 10 * 1024 * 1024


class _HTMLText(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _HTMLText()
    try:
        p.feed(html)
    except Exception:
        pass
    text = "".join(p.parts)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return (p.title or "").strip(), text


def _read_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    out: list[str] = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            out.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                out.append(" | ".join(cells))
    return "\n".join(out)


def read_document(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    ext = p.suffix.lower()
    if ext == ".docx":
        text = _read_docx(p)
    elif ext in (".html", ".htm", ".xhtml"):
        _, text = html_to_text(p.read_text(encoding="utf-8", errors="replace"))
    elif ext in (".txt", ".md", ".log", ".json", ".csv", ".xml"):
        text = p.read_text(encoding="utf-8", errors="replace")
    elif ext == ".doc":
        raise ValueError(".doc(旧格式)不支持，请让发送方转存为 .docx")
    elif ext == ".pdf":
        raise ValueError("PDF 暂不支持解析，请让发送方提供 docx/文本")
    else:
        # best effort raw read
        text = p.read_text(encoding="utf-8", errors="replace")
        if "\x00" in text[:2000]:
            raise ValueError(f"二进制文件 ({ext}) 暂不支持解析")
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + f"\n...(截断，共超过 {MAX_DOC_CHARS} 字符)"
    return text


_JS_HINT_PAGES = ("docs.qq.com", "kdocs.cn", "www.kdocs.cn", "drive.weixin")


async def fetch_url(url: str, *, timeout: float = 20.0) -> str:
    import aiohttp

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), headers=headers
    ) as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            ctype = resp.headers.get("Content-Type", "")
            raw = await resp.read()

    if len(raw) > MAX_FETCH_BYTES:
        raw = raw[:MAX_FETCH_BYTES]
    if "html" in ctype:
        title, text = html_to_text(raw.decode("utf-8", errors="replace"))
        if not text or len(text.strip()) < 60:
            hint = _js_hint(url)
            raise RuntimeError(hint or "页面无可见文本（可能需要 JS 渲染或登录）")
        out = f"[标题] {title}\n\n{text}" if title else text
    elif "json" in ctype:
        out = raw.decode("utf-8", errors="replace")
    else:
        out = raw.decode("utf-8", errors="replace")
    if len(out) > MAX_DOC_CHARS:
        out = out[:MAX_DOC_CHARS] + f"\n...(截断，共超过 {MAX_DOC_CHARS} 字符)"
    return out


def _js_hint(url: str) -> str | None:
    host = any(h in url for h in _JS_HINT_PAGES)
    if host:
        return (
            "该页面为在线文档（腾讯/金山文档），正文需要浏览器 JS 渲染或登录，直接抓取拿不到内容。"
            "可行做法：1) 请分享者开启「任何人可查看」链接权限后重试；"
            "2) 让对方导出 docx/txt 直接发到 QQ（消息档案会自动落盘，然后用 read_document 读取）；"
            "3) 若是 QQ 小程序消息，先从 light_app 的 json_payload 里找直链字段。"
        )
    return None
