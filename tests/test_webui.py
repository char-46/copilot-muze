"""WebUI API: memories / rag endpoints + rag fields in /api/info."""

import math

import pytest
from aiohttp.test_utils import TestClient, TestServer

from muze.config import MuzeConfig
from muze.core.service import ControlService, Runtime
from muze.chatlog import ChatLog
from muze.rag import RagIndexer, VecStore
from muze.store.db import open_db
from muze.store.repo import Repository
from muze.web.server import WebAccount, create_app

from test_permissions import Recorder


class FakeEmbedder:
    provider = "local"
    configured = True
    available = True
    healthy = True
    model_id = "fake-embed"

    class _Cfg:
        model = "fake-embed"

    def __init__(self, dims: int = 8) -> None:
        self.cfg = self._Cfg()
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    async def prepare(self, governor=None) -> None:
        return None

    def unload_if_idle(self, now=None) -> bool:
        return False

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self._dims
        for ch in text:
            v[ord(ch) % self._dims] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        return [self._vec(t) for t in texts]


@pytest.fixture
async def client(tmp_path):
    conn = await open_db(str(tmp_path / "webui.db"))
    repo = Repository(conn)
    svc = ControlService(MuzeConfig(), Recorder(), repo, ChatLog(), Runtime())
    svc.agent = object()  # agent=on

    cfg = MuzeConfig()
    cfg.rag.enabled = True
    vec = VecStore(repo)
    await vec.setup()
    rag = RagIndexer(repo, vec, FakeEmbedder(), cfg)
    svc.runtime.rag = rag

    await repo.add_memory("identity", "owner", "主人喜欢喝乌龙茶", source="test")
    await repo.add_memory("preference", "friend/1", "老王喜欢钓鱼", source="test")
    n = await rag.run_once()
    assert n >= 2  # two memories indexed

    app = create_app({"main": WebAccount(svc, repo, svc.runtime)}, "test-token")
    server = TestServer(app)
    cl = TestClient(server)
    await cl.start_server()
    cl.session.headers["Authorization"] = "Bearer test-token"
    yield cl
    await cl.close()
    await conn.close()


async def test_memories_endpoint(client):
    r = await client.get("/api/memories")
    assert r.status == 200
    d = await r.json()
    assert len(d["memories"]) == 2

    r = await client.get("/api/memories?q=钓鱼")
    d = await r.json()
    assert len(d["memories"]) == 1
    assert "老王" in d["memories"][0]["content"]

    r = await client.get("/api/memories?q=不存在的东西")
    d = await r.json()
    assert d["memories"] == []


async def test_rag_endpoint_and_search(client):
    r = await client.get("/api/rag")
    assert r.status == 200
    s = await r.json()
    assert s["enabled"] is True
    assert s["docs"] >= 2
    assert s["docs_by_kind"].get("memory") == 2
    assert s["vec_model"] == "fake-embed"

    r = await client.get("/api/rag/search", params={"q": "钓鱼"})
    d = await r.json()
    assert d["enabled"] is True
    assert d["results"] and any("钓鱼" in x["text"] for x in d["results"])

    r = await client.get("/api/rag/search", params={"q": "乌龙茶", "kind": "memory"})
    d = await r.json()
    assert d["results"] and all(x["kind"] == "memory" for x in d["results"])

    r = await client.get("/api/rag/search")  # missing q
    assert r.status == 400


async def test_info_includes_rag_and_memories(client):
    r = await client.get("/api/info")
    assert r.status == 200
    d = await r.json()
    a = d["accounts"][0]
    assert a["memories"] == 2
    assert a["rag"]["enabled"] is True
    assert a["rag"]["docs"] >= 2


async def test_rag_endpoints_require_token(client):
    r = await client.get("/api/rag", headers={"Authorization": "Bearer wrong"})
    assert r.status == 401
