"""fetch_url capability: static first, headless-browser retry for JS pages
(user direction 2026-10-31: 不要求对所有页面总结，至少保证能力)."""

import asyncio

import pytest

import muze.docs as D


# ------------------------------------------------------------ browser find
def test_find_browser_explicit_wins(tmp_path):
    exe = tmp_path / "brw.exe"
    exe.write_bytes(b"")
    assert D.find_browser(str(exe)) == str(exe)


def test_find_browser_explicit_missing_is_empty():
    assert D.find_browser(r"Z:\nope\brw.exe") == ""


def test_find_browser_autodetect(monkeypatch, tmp_path):
    fake = tmp_path / "edge.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(D, "_BROWSER_CANDIDATES", (str(fake),))
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert D.find_browser() == str(fake)


def test_find_browser_none(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(D, "_BROWSER_CANDIDATES", ())
    assert D.find_browser() == ""


# ------------------------------------------------------------ decode
def test_decode_html_gb18030_page():
    raw = b"<html><body>\xd4\xcb\xb6\xaf\xbb\xe1\xcd\xa8\xd6\xaa</body></html>"
    assert "运动会通知" in D._decode_html(raw)


def test_decode_html_declared_charset():
    raw = ('<?xml encoding="gb2312"?><title>\xd6\xd0</title>').encode("latin-1")
    assert D._decode_html(raw)


# ------------------------------------------------------------- render_dom
async def test_render_dom_invokes_browser_with_dump_dom(monkeypatch, tmp_path):
    exe = tmp_path / "brw.exe"
    exe.write_bytes(b"")
    seen: dict = {}

    class FakeProc:
        def __init__(self, *a, **kw):
            pass

        async def communicate(self):
            return b"<html><body>rendered</body></html>", b""

        def kill(self):
            pass

    async def fake_exec(path, *argv, **kw):
        seen["path"] = path
        seen["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(D, "_BROWSER_CANDIDATES", (str(exe),))
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)

    html = await D.render_dom("https://x.example/a", browser_cmd=str(exe))
    assert "rendered" in html
    assert seen["path"] == str(exe)
    assert "--dump-dom" in seen["argv"] and seen["argv"][-1] == "https://x.example/a"


# ------------------------------------------------------- fetch fallback
def _patch_http(monkeypatch, raw: bytes, ctype: str = "text/html"):
    async def fake_get(url, timeout):
        return raw, ctype

    monkeypatch.setattr(D, "_http_get", fake_get)


async def test_fetch_url_falls_back_to_headless_for_js_shell(monkeypatch):
    """Static DOM is an empty shell -> the browser retry must rescue 正文."""
    _patch_http(
        monkeypatch,
        b"<html><head><title>shell</title></head><body><div id='app'></div></body></html>",
    )

    async def fake_render(url, *, timeout=30.0, browser_cmd=""):
        return (
            "<html><head><title>真正的标题</title></head><body>"
            + "<p>这是渲染后才出现的页面正文内容，足够长可以提取。</p>" * 5
            + "</body></html>"
        )

    monkeypatch.setattr(D, "render_dom", fake_render)
    out = await D.fetch_url("https://x.example/a")
    assert "真正的标题" in out and "渲染后才出现" in out


async def test_fetch_url_rich_static_page_skips_the_browser(monkeypatch):
    body = "<html><body>" + ("<p>这是真实的静态正文内容，足够长。</p>" * 40) + "</body></html>"
    _patch_http(monkeypatch, body.encode())

    async def no_render(url, **kw):
        raise AssertionError("static page must not pay a browser launch")

    monkeypatch.setattr(D, "render_dom", no_render)
    out = await D.fetch_url("https://x.example/b")
    assert "真实的静态正文" in out


async def test_fetch_url_antibot_stub_triggers_render(monkeypatch):
    """Weixin-style stub: ~75 chars of junk (参数错误...) passes the old
    60-char bar -- the 400-char bar catches it and the browser wins."""
    stub = "<html><body>参数错误 ， ， ， 。 视频 小程序 赞 在看 分享 留言 收藏</body></html>"
    _patch_http(monkeypatch, stub.encode())

    async def fake_render(url, *, timeout=30.0, browser_cmd=""):
        return (
            "<html><head><title>推送标题</title></head><body>"
            + "<p>渲染后的公众号正文内容出现在这里。</p>" * 30
            + "</body></html>"
        )

    monkeypatch.setattr(D, "render_dom", fake_render)
    out = await D.fetch_url("https://mp.weixin.qq.com/s/xxx")
    assert "推送标题" in out and "参数错误" not in out


async def test_fetch_url_http_403_dodged_by_browser(monkeypatch):
    async def refused(url, timeout):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(D, "_http_get", refused)

    async def fake_render(url, *, timeout=30.0, browser_cmd=""):
        return "<html><head><title>板式推送</title></head><body>" + "<p>正文内容在这里。</p>" * 30 + "</body></html>"

    monkeypatch.setattr(D, "render_dom", fake_render)
    out = await D.fetch_url("https://x.example/c")
    assert "板式推送" in out


async def test_fetch_url_no_browser_still_raises_hint(monkeypatch):
    _patch_http(monkeypatch, b"<html><body><div id=app></div></body></html>")

    async def boom(url, **kw):
        raise RuntimeError("未找到可用的无头浏览器")

    monkeypatch.setattr(D, "render_dom", boom)
    import pytest

    with pytest.raises(RuntimeError):
        await D.fetch_url("https://docs.qq.com/x")
