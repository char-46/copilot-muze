"""Link-title enrichment (user feedback 2026-10-31: 不会读链接的标题)."""

import pytest

from muze.linkmeta import extract_urls, title_from_html


@pytest.fixture
async def repo(tmp_path):
    from muze.store.db import open_db
    from muze.store.repo import Repository

    conn = await open_db(str(tmp_path / "link.db"))
    yield Repository(conn)
    await conn.close()


def test_extract_urls_dedupes_and_strips_punctuation():
    text = "看 https://a.example/x.html，再发 http://b.example/y 和 https://a.example/x.html。"
    urls = extract_urls(text)
    assert urls == ["https://a.example/x.html", "http://b.example/y"]


def test_title_from_html_utf8_and_entities():
    body = b"<html><head><title>  \xe8\xbf\x90\xe5\x8a\xa8\xe4\xbc\x9a&amp;\xe6\x8e\xa8\xe9\x80\x81  </title></head>"
    assert title_from_html(body) == "运动会&推送"


def test_title_from_html_gb18030_fallback():
    body = b"<title>\xd4\xcb\xb6\xaf\xbb\xe1\xcd\xa8\xd6\xaa</title>"
    assert title_from_html(body) == "运动会通知"


def test_title_from_html_none():
    assert title_from_html(b"<html><body>no title</body></html>") == ""


async def test_link_title_repo_cache_roundtrip(repo):
    assert await repo.get_link_title("https://x.example/a") is None  # never fetched
    await repo.set_link_title("https://x.example/a", "页面标题")
    assert await repo.get_link_title("https://x.example/a") == "页面标题"
    await repo.set_link_title("https://x.example/b", "")  # fetched, untitled
    assert await repo.get_link_title("https://x.example/b") == ""  # no retry


async def test_messages_with_unresolved_links_and_append(repo):
    from muze.provider.milky.models import IncomingMessage
    import time as _t

    m = IncomingMessage.from_raw(
        {
            "message_scene": "friend", "peer_id": 9,
            "message_seq": int(_t.time()) % 100000, "sender_id": 1,
            "time": int(_t.time()), "segments": [{"type": "text", "data": {"text": "看这个 https://x.example/a"}}],
        }
    )
    await repo.upsert_message(m, "看这个 https://x.example/a", "event")
    rows = await repo.messages_with_unresolved_links(0, limit=5)
    assert len(rows) == 1 and "https://x.example/a" in rows[0]["text_content"]
    await repo.append_message_text(rows[0]["_rid"], "\n[链接] 页面标题")
    assert not await repo.messages_with_unresolved_links(0, limit=5)
