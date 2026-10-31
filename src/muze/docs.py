"""Document & URL ingestion for the agent.

- read_document(path, entry=None): local files already archived by the media
  pipeline. Text (.txt/.md/.log/.json/.csv/.xml), HTML, docx, pdf, xlsx, xls,
  pptx, rtf, legacy .doc/.ppt (sniffed: renamed docx/HTML/RTF or crude OLE
  text), and archives (zip/7z/rar/tar/gz/bz2) — `entry=None` lists contents
  with previews of small text members, `entry` extracts one member.
- fetch_url(url): plain HTTP GET; HTML is stripped to text. JS-rendered
  pages (金山文档/腾讯文档 web apps, some 小程序 landing pages) cannot be
  fully extracted this way — the caller is told so with actionable advice
  (the doc-sources skill explains how to dig URLs out of light_app payloads
  and what to do when a page needs login/JS).
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("muze.docs")

MAX_DOC_CHARS = 20000
MAX_FETCH_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 400
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".tgz", ".txz", ".xz"}
_TEXT_EXTS = {
    ".txt", ".md", ".log", ".json", ".csv", ".xml", ".html", ".htm", ".xhtml",
    ".py", ".js", ".ts", ".css", ".yaml", ".yml", ".ini", ".toml", ".sql",
    ".srt", ".lrc", ".rtf",
}


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


def _read_pdf(p: Path) -> str:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(str(p))
    except Exception as e:  # noqa: BLE001 - pdfium raises PdfiumError
        msg = str(e)
        if "password" in msg.lower() or "password" in repr(e).lower():
            raise ValueError("PDF 已加密，无法读取") from e
        raise ValueError(f"PDF 读取失败: {msg[:120]}") from e
    out: list[str] = []
    try:
        for i, page in enumerate(doc):
            tp = page.get_textpage()
            try:
                out.append(f"--- 第{i + 1}页 ---\n{tp.get_text_range().strip()}")
            finally:
                tp.close()
                page.close()
            if sum(len(x) for x in out) > MAX_DOC_CHARS:
                break
    finally:
        doc.close()
    return "\n\n".join(out)


def _sheet_rows(rows) -> list[str]:
    out: list[str] = []
    for row in rows:
        cells = ["" if c is None else str(c).strip() for c in row]
        if any(cells):
            out.append(" | ".join(cells))
        if len(out) > 600:
            out.append("...(行数过多已截断)")
            break
    return out


def _read_xlsx(p: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(p), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        for ws in wb.worksheets:
            parts.append(f"=== 工作表 {ws.title} ===\n" + "\n".join(_sheet_rows(ws.iter_rows(values_only=True))))
        return "\n\n".join(parts)
    finally:
        wb.close()


def _read_xls(p: Path) -> str:
    import xlrd

    book = xlrd.open_workbook(str(p))
    parts = []
    for ws in book.sheets():
        rows = [
            ["" if c.value is None else str(c.value).strip() for c in ws.row(r)]
            for r in range(ws.nrows)
        ]
        parts.append(f"=== 工作表 {ws.name} ===\n" + "\n".join(_sheet_rows(rows)))
    return "\n\n".join(parts)


def _read_pptx(p: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(p))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        texts.append(" | ".join(cells))
        if texts:
            parts.append(f"=== 幻灯片 {i} ===\n" + "\n".join(texts))
    return "\n\n".join(parts) or "(幻灯片无文本)"


_RTF_CONTROL = re.compile(r"\\(?:par|line)\b")
_RTF_STRIP = re.compile(r"\\[a-z]+-?\d*\s?|\\[{}]|{|}|\\'")


def _read_rtf(p: Path) -> str:
    raw = p.read_text(encoding="utf-8", errors="replace")
    txt = _RTF_CONTROL.sub("\n", raw)
    txt = _RTF_STRIP.sub("", txt)
    lines = [ln.strip() for ln in txt.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_ole_text(data: bytes) -> str:
    """Crude text from legacy OLE binaries (.doc/.ppt): keep runs of printable
    ASCII and UTF-16LE (Word stores most body text as UTF-16LE)."""
    out: list[str] = []
    # UTF-16LE runs
    cur: list[str] = []
    for i in range(0, len(data) - 1, 2):
        ch = data[i] | (data[i + 1] << 8)
        if 0x20 <= ch < 0xFFFE and ch not in (0x7F,) and not (0xD800 <= ch < 0xE000):
            cur.append(chr(ch))
        else:
            if len(cur) >= 6:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 6:
        out.append("".join(cur))
    # printable ASCII runs
    cur2: list[str] = []
    for b in data:
        if 0x20 <= b < 0x7F:
            cur2.append(chr(b))
        else:
            if len(cur2) >= 10:
                out.append("".join(cur2))
            cur2 = []
    text = "\n".join(r.strip() for r in out if len(r.strip()) >= 6)
    return text


def _read_legacy_binary(p: Path, ext: str) -> str:
    """Sniff what a .doc/.ppt really is, then extract best-effort."""
    head = p.read_bytes()[:8]
    if head[:2] == b"PK":
        inner = p.with_suffix(".docx" if ext == ".doc" else ".pptx")
        import shutil
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=inner.suffix, delete=False) as f:
            shutil.copyfile(p, f.name)
            try:
                return _read_docx(Path(f.name)) if ext == ".doc" else _read_pptx(Path(f.name))
            finally:
                Path(f.name).unlink(missing_ok=True)
    data = p.read_bytes()
    if data[:5] == b"{\\rtf":
        return _read_rtf(p)
    low = data[:2048].lstrip().lower()
    if low.startswith(b"<!doctype") or b"<html" in low:
        return html_to_text(data.decode("utf-8", errors="replace"))[1]
    if head[:4] == b"\xd0\xcf\x11\xe0":
        text = _extract_ole_text(data)
        if len(text) > 40:
            return text
    raise ValueError(
        f"{ext} 旧格式无法可靠解析（非 OLE/RTF/HTML）。"
        "建议请发送方另存为 docx/pptx，或用 PDF。"
    )


# ---------------------------------------------------------------- archives
def _zip_entries(p: Path) -> list[tuple[str, int]]:
    import zipfile

    with zipfile.ZipFile(p) as z:
        return [(i.filename, i.file_size) for i in z.infolist()]


def _zip_read(p: Path, name: str) -> bytes:
    import zipfile

    with zipfile.ZipFile(p) as z:
        with z.open(name) as f:
            return f.read(MAX_FETCH_BYTES)


def _rar_backend() -> str | None:
    """7z.exe is the practical rar backend on Windows."""
    import os
    import shutil

    if shutil.which("7z"):
        return "7z"
    for cand in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _rar_entries(p: Path) -> list[tuple[str, int]]:
    backend = _rar_backend()
    if not backend:
        raise ValueError("读取 .rar/.7z 需要安装 7-Zip（或把压缩包转存为 zip 后重试）")
    import subprocess

    r = subprocess.run(
        [backend, "l", "-slt", str(p)], capture_output=True, timeout=60
    )
    if r.returncode != 0:
        raise ValueError(f"7z 列表失败: {r.stderr.decode(errors='replace')[:200]}")
    out: list[tuple[str, int]] = []
    name = ""
    for ln in r.stdout.decode(errors="replace").splitlines():
        if ln.startswith("Path = "):
            name = ln[7:].strip()
        elif ln.startswith("Size = ") and name:
            try:
                out.append((name, int(ln[7:].strip())))
            except ValueError:
                out.append((name, 0))
            name = ""
    # [0] is the archive itself when the header block carries a Size line
    # (.rar does, .7z does not) — drop it by identity, not position.
    arch = (str(p), p.name)
    return [(n, s) for n, s in out if n not in arch]


def _rar_read(p: Path, name: str) -> bytes:
    backend = _rar_backend()
    if not backend:
        raise ValueError("读取 .rar/.7z 需要安装 7-Zip（或把压缩包转存为 zip 后重试）")
    import subprocess

    r = subprocess.run(
        [backend, "x", "-so", str(p), name], capture_output=True, timeout=120
    )
    if r.returncode != 0:
        raise ValueError(f"7z 解压失败: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout[:MAX_FETCH_BYTES]


def _tar_entries(p: Path) -> list[tuple[str, int]]:
    import tarfile

    with tarfile.open(p) as t:
        return [(m.name, m.size) for m in t.getmembers()]


def _tar_read(p: Path, name: str) -> bytes:
    import tarfile

    with tarfile.open(p) as t:
        f = t.extractfile(name)
        if f is None:
            raise ValueError(f"{name} 不是普通文件")
        return f.read(MAX_FETCH_BYTES)


def _archive_kind(ext: str):
    if ext in (".zip",):
        return _zip_entries, _zip_read
    if ext in (".7z",):
        return _rar_entries, _rar_read  # same system 7z.exe backend as .rar
    if ext in (".rar",):
        return _rar_entries, _rar_read
    if ext in (".tar", ".tgz", ".txz", ".gz", ".bz2", ".xz"):
        return _tar_entries, _tar_read
    return None


def _read_archive(p: Path, ext: str, entry: str | None) -> str:
    kind = _archive_kind(ext)
    if kind is None:
        raise ValueError(f"不支持的压缩包类型 {ext}")
    list_fn, read_fn = kind
    entries = list_fn(p)
    if not entries:
        raise ValueError("压缩包为空")
    if entry is None:
        lines = [f"压缩包共 {len(entries)} 项（read_document 传 entry=文件名 读取其中一项）:"]
        for name, size in entries[:MAX_ARCHIVE_ENTRIES]:
            mark = "  " if Path(name).suffix.lower() in _TEXT_EXTS or not Path(name).suffix else " [二进制]"
            lines.append(f"- {name} ({size} B){mark}")
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            lines.append(f"...(共 {len(entries)} 项)")
        # preview small text members
        shown = 0
        for name, size in entries:
            if shown >= 3 or size > 20000:
                continue
            if Path(name).suffix.lower() not in _TEXT_EXTS or name.endswith("/"):
                continue
            try:
                raw = read_fn(p, name)
                txt = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if txt:
                lines.append(f"\n=== {name} 预览 ===\n{txt[:1500]}")
                shown += 1
        return "\n".join(lines)
    # exact match first, then unique suffix match
    names = [n for n, _ in entries]
    target = entry if entry in names else next(
        (n for n in names if n.endswith("/" + entry) or n == entry), None
    )
    if target is None:
        cands = [n for n in names if entry in n]
        if len(cands) == 1:
            target = cands[0]
        elif not cands:
            raise ValueError(f"压缩包里没有 {entry}（共 {len(names)} 项，先不传 entry 看列表）")
        else:
            raise ValueError(f"{entry} 匹配多项: {cands[:5]}")
    raw = read_fn(p, target)
    inner_ext = Path(target).suffix.lower()
    if inner_ext in ARCHIVE_EXTS:
        raise ValueError(f"{target} 是嵌套压缩包，请解压后读取")
    if inner_ext in _TEXT_EXTS:
        return raw.decode("utf-8", errors="replace")
    # binary member (pdf/docx/...): render through the document pipeline
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=inner_ext, delete=False) as f:
        f.write(raw)
        tmp = Path(f.name)
    try:
        return read_document(str(tmp))
    except Exception as e:
        raise ValueError(f"{target} 是二进制文件（{inner_ext}），无法按文本读取: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)


def read_document(path: str | Path, entry: str | None = None) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    ext = p.suffix.lower()
    if ext in ARCHIVE_EXTS or ext == ".zip":
        text = _read_archive(p, ext, entry)
    elif ext == ".docx":
        text = _read_docx(p)
    elif ext in (".html", ".htm", ".xhtml"):
        _, text = html_to_text(p.read_text(encoding="utf-8", errors="replace"))
    elif ext in (".txt", ".md", ".log", ".json", ".csv", ".xml"):
        text = p.read_text(encoding="utf-8", errors="replace")
    elif ext == ".pdf":
        text = _read_pdf(p)
    elif ext == ".xlsx":
        text = _read_xlsx(p)
    elif ext == ".xls":
        text = _read_xls(p)
    elif ext == ".pptx":
        text = _read_pptx(p)
    elif ext == ".rtf":
        text = _read_rtf(p)
    elif ext in (".doc", ".ppt"):
        text = _read_legacy_binary(p, ext)
    else:
        # best effort raw read
        text = p.read_text(encoding="utf-8", errors="replace")
        if "\x00" in text[:2000]:
            raise ValueError(f"二进制文件 ({ext}) 暂不支持解析")
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + f"\n...(截断，共超过 {MAX_DOC_CHARS} 字符)"
    return text


_JS_HINT_PAGES = ("docs.qq.com", "kdocs.cn", "www.kdocs.cn", "drive.weixin")


async def fetch_url(
    url: str, *, timeout: float = 20.0, render: bool | None = None,
    browser_cmd: str = "",
) -> str:
    """Page text for one URL.

    Static fetch first; the headless browser retries when the static HTML
    is a thin shell/anti-bot stub (<_RETRY_MIN_CHARS visible text) or the
    plain HTTP request is refused (403/412-class) -- whichever side yields
    more text wins (capability guarantee, user direction 2026-10-31: 不要求
    对所有页面总结，至少保证能力)."""
    raw, ctype = b"", ""
    http_err: Exception | None = None
    try:
        raw, ctype = await _http_get(url, timeout)
    except Exception as e:
        http_err = e

    if http_err is None and "html" not in ctype:
        # json / plain / binary: verbatim, exactly as before
        out = raw.decode("utf-8", errors="replace")
        if len(out) > MAX_DOC_CHARS:
            out = out[:MAX_DOC_CHARS] + f"\n...(截断，共超过 {MAX_DOC_CHARS} 字符)"
        return out

    title, text = "", ""
    if raw:
        title, text = html_to_text(_decode_html(raw))
    if render is not False and (
        http_err is not None or len(text.strip()) < _RETRY_MIN_CHARS
    ):
        # JS page / anti-bot stub / plain refusal: let a real browser try
        try:
            dom = await render_dom(url, browser_cmd=browser_cmd)
            t2, x2 = html_to_text(dom)
            if len(x2.strip()) > len(text.strip()):
                title, text = (t2 or title), x2
        except Exception as e:
            log.debug("headless render failed for %s: %s", url[:80], e)
    if len(text.strip()) < 60:
        if http_err is not None:
            raise http_err
        raise RuntimeError(_js_hint(url) or "页面无可见文本（可能需要 JS 渲染或登录）")
    out = f"[标题] {title}\n\n{text}" if title else text
    if len(out) > MAX_DOC_CHARS:
        out = out[:MAX_DOC_CHARS] + f"\n...(截断，共超过 {MAX_DOC_CHARS} 字符)"
    return out


# below this many visible characters the static HTML is treated as a shell
# (JS app or anti-bot stub like weixin's 参数错误 page, which still carries
# ~75 chars of junk) and the browser retry kicks in
_RETRY_MIN_CHARS = 400


async def _http_get(url: str, timeout: float) -> tuple[bytes, str]:
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
    return raw[:MAX_FETCH_BYTES], ctype


def _decode_html(raw: bytes) -> str:
    """Charset-aware HTML decode: declared charset, then utf-8 strict,
    then gb18030 (legacy Chinese pages decode as mojibake under utf-8
    'replace' and the text extractor would still 'succeed' wrongly)."""
    import re as _re

    head = raw[:2048]
    m = _re.search(rb"charset=[\"']?([\w-]+)", head, _re.IGNORECASE)
    if m:
        try:
            return raw.decode(m.group(1).decode("ascii", "ignore"), "replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", "replace")


# ------------------------------------------------- headless browser render
_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


def find_browser(explicit: str = "") -> str:
    """A usable Chromium-family browser exe (explicit > PATH > known spots).
    '' = none found (headless render unavailable)."""
    if explicit:
        return explicit if Path(explicit).exists() else ""
    import shutil

    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    for p in _BROWSER_CANDIDATES:
        if Path(p).exists():
            return p
    return ""


async def render_dom(
    url: str, *, timeout: float = 30.0, browser_cmd: str = ""
) -> str:
    """Run the system browser headless and return the RENDERED DOM html.

    --virtual-time-budget makes the run settle scripts quickly; the hard
    asyncio timeout guards a hung page and kills the process."""
    import asyncio as _aio

    exe = find_browser(browser_cmd)
    if not exe:
        raise RuntimeError("未找到可用的无头浏览器（Edge/Chrome）")
    proc = await _aio.create_subprocess_exec(
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--disable-extensions",
        "--dump-dom",
        "--virtual-time-budget=8000",
        url,
        stdout=_aio.subprocess.PIPE,
        stderr=_aio.subprocess.DEVNULL,
    )
    try:
        out, _err = await _aio.wait_for(proc.communicate(), timeout=timeout)
    except _aio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"无头渲染超时（>{timeout:.0f}s）")
    html = (out or b"").decode("utf-8", errors="replace")
    if len(html) > MAX_FETCH_BYTES:
        html = html[:MAX_FETCH_BYTES]
    return html


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
