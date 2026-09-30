"""Web research: provider parsing + the research() closure."""

import pytest

from muze.config import ResearchConfig
from muze.research import SearchClient


class FakeResp:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self._text = text
        self.status = 200

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("get", url))
        return FakeCtx(self._resp)

    def post(self, url, **kw):
        self.calls.append(("post", url))
        return FakeCtx(self._resp)

    async def close(self):
        pass


async def test_searxng_parsing():
    cfg = ResearchConfig(provider="searxng", base_url="http://searx")
    sess = FakeSession(FakeResp(payload={"results": [{"title": "T", "url": "http://a", "content": "c"}]}))
    client = SearchClient(cfg, session=sess)
    hits = await client.search("hello", 3)
    assert hits == [{"title": "T", "url": "http://a", "snippet": "c"}]
    assert sess.calls and sess.calls[0][0] == "get" and "format=json" in sess.calls[0][1]


async def test_tavily_and_brave_parsing():
    tav = SearchClient(
        ResearchConfig(provider="tavily", api_key="k"),
        session=FakeSession(FakeResp(payload={"results": [{"title": "A", "url": "u", "content": "s"}]})),
    )
    assert (await tav.search("q"))[0]["title"] == "A"

    brave = SearchClient(
        ResearchConfig(provider="brave", api_key="k"),
        session=FakeSession(FakeResp(payload={"web": {"results": [{"title": "B", "url": "u2", "description": "d"}]}})),
    )
    hit = (await brave.search("q"))[0]
    assert hit["title"] == "B" and hit["snippet"] == "d"


async def test_configured_flags():
    assert SearchClient(ResearchConfig(provider="brave")).configured is False  # no key
    assert SearchClient(ResearchConfig(provider="brave", api_key="k")).configured is True
    assert SearchClient(ResearchConfig(provider="searxng", base_url="http://s")).configured is True
    assert SearchClient(ResearchConfig(provider="")).configured is False


async def test_research_closure(monkeypatch):
    import muze.docs as docs

    async def fake_fetch(url):
        return "PAGE-BODY:" + url

    monkeypatch.setattr(docs, "fetch_url", fake_fetch)
    cfg = ResearchConfig(provider="searxng", base_url="http://s", fetch_chars=100)
    resp = FakeResp(
        payload={
            "results": [
                {"title": "one", "url": "http://1", "content": "c1"},
                {"title": "two", "url": "http://2", "content": "c2"},
                {"title": "three", "url": "http://3", "content": "c3"},
            ]
        }
    )
    client = SearchClient(cfg, session=FakeSession(resp))
    out = await client.research("topic", pages=2)
    assert [r["title"] for r in out["results"]] == ["one", "two"]
    assert out["results"][0]["extract"] == "PAGE-BODY:http://1"
    assert [r["title"] for r in out["other_hits"]] == ["three"]
