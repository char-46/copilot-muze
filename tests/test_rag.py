"""Identity profiles + vector RAG (sqlite-vec) + resource governor/back-off."""

import math

import pytest

from muze.ai.embeddings import Embedder
from muze.config import MuzeConfig
from muze.rag import RagIndexer, VecStore
from muze.rag_resource import ResourceGovernor
from muze.store.db import open_db
from muze.store.repo import Repository


class FakeEmbedder:
    """Deterministic char-histogram embedding — enough to test the plumbing."""

    provider = "openai"
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
async def repo(tmp_path):
    conn = await open_db(str(tmp_path / "rag.db"))
    yield Repository(conn)
    await conn.close()


async def test_profile_crud_and_index_lines(repo):
    await repo.upsert_profile("group/9", "项目组", "# 成员\n- 10001 张三（aka 老张）")
    p = await repo.get_profile("group/9")
    assert p and p["name"] == "项目组" and "老张" in p["content"]

    from muze.profiles import index_lines

    lines = index_lines([p])
    assert lines and "group/9" in lines[0] and "成员" in lines[0]


async def test_rag_index_and_semantic_search(repo):
    await repo.upsert_profile("friend/1", "老王", "老王喜欢钓鱼，昵称 渔翁")
    await repo.add_memory("fact", "owner", "主人喜欢喝乌龙茶", source="test")
    cfg = MuzeConfig()
    cfg.rag.enabled = True

    vec = VecStore(repo)
    assert await vec.setup() is True
    indexer = RagIndexer(repo, vec, FakeEmbedder(), cfg)
    assert indexer.enabled is True

    n = await indexer.run_once()
    assert n >= 2  # profile + memory

    hits = await indexer.search("钓鱼", k=3)
    assert hits and any(h["kind"] == "profile" for h in hits)

    mem = await indexer.search("乌龙茶", k=3, kind="memory")
    assert mem and mem[0]["kind"] == "memory"


async def test_rag_disabled_without_embedding(repo):
    cfg = MuzeConfig()  # rag.enabled False by default
    vec = VecStore(repo)
    await vec.setup()

    class Off(FakeEmbedder):
        available = False
        healthy = False

    indexer = RagIndexer(repo, vec, Off(), cfg)
    assert indexer.enabled is False
    assert await indexer.run_once() == 0


async def test_rag_chat_windows_optin(repo):
    from muze.provider.milky.models import IncomingMessage

    def mk(seq, text):
        return IncomingMessage.from_raw(
            {
                "message_scene": "group",
                "peer_id": 7,
                "message_seq": seq,
                "sender_id": 42,
                "time": 1790000000 + seq,
                "segments": [{"type": "text", "data": {"text": text}}],
            }
        )

    for seq, text in [(1, "今天又加班到很晚"), (2, "项目要上线了"), (3, "明天继续")]:
        await repo.upsert_message(mk(seq, text), text, "event")

    cfg = MuzeConfig()
    cfg.rag.enabled = True
    cfg.rag.chat = True
    vec = VecStore(repo)
    await vec.setup()
    indexer = RagIndexer(repo, vec, FakeEmbedder(), cfg)
    await indexer.run_once()

    hits = await indexer.search("加班", k=3, scene="group", peer_id=7)
    assert hits and hits[0]["kind"] == "chat"
    assert "加班" in hits[0]["text"]


async def test_who_command(repo):
    from muze.chatlog import ChatLog
    from muze.core.service import ControlContext, ControlService, Runtime
    from test_permissions import Recorder

    svc = ControlService(MuzeConfig(), Recorder(), repo, ChatLog(), Runtime())
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)
    await repo.upsert_profile("friend/1", "老王", "老王喜欢钓鱼，昵称 渔翁")

    listing = await svc.run_command("who", ctx)
    assert "friend/1" in listing.text and "老王" in listing.text
    detail = await svc.run_command("who friend/1", ctx)
    assert "钓鱼" in detail.text


async def test_vecstore_rebuilds_on_model_change(repo):
    vec = VecStore(repo)
    await vec.setup()
    await vec.ensure(4, "model-a")
    rid = await repo.upsert_rag_doc("profile", "friend/1", "hello")
    await vec.upsert(rid, [1.0, 0.0, 0.0, 0.0])
    assert len(await repo.list_rag_docs()) == 1

    # same dims, different model => vectors are meaningless -> rebuild
    await vec.ensure(4, "model-b")
    assert await repo.list_rag_docs() == []
    assert await repo.rag_get_state("vec_model") == "model-b"


# ----------------------------------------------------------- governor
def _gov(**over):
    cfg = MuzeConfig()
    g = ResourceGovernor(cfg.rag)

    class G(ResourceGovernor):
        pass

    g.power_plugged = lambda: over.get("power")
    g.cpu_busy = lambda: over.get("cpu")
    g.gpu_util = lambda: over.get("gpu")
    g.gpu_free_gb = lambda: over.get("free")
    return g


def test_governor_idle_rules():
    g = _gov(power=False, cpu=5, gpu=0, free=8)
    assert g.idle(last_activity=0)[0] is False  # on battery
    g = _gov(power=True, cpu=90, gpu=0, free=8)
    assert g.idle(last_activity=0)[0] is False  # cpu busy
    g = _gov(power=True, cpu=5, gpu=80, free=8)
    assert g.idle(last_activity=0)[0] is False  # gpu busy
    g = _gov(power=True, cpu=5, gpu=0, free=8)
    import time as _t
    assert g.idle(last_activity=_t.time())[0] is False  # recent activity
    assert g.idle(last_activity=0)[0] is True


def test_governor_device_and_threads():
    assert _gov(power=True, cpu=5, gpu=5, free=8).choose_device() == "cuda"
    assert _gov(power=True, cpu=5, gpu=5, free=1).choose_device() == "cpu"  # can't fit
    # fits, but the GPU is already busy -> CPU
    assert _gov(power=True, cpu=5, gpu=99, free=8).choose_device() == "cpu"
    assert _gov(power=True, cpu=0, gpu=0, free=8).cpu_threads() >= 1
    assert _gov(power=True, cpu=100, gpu=0, free=8).cpu_threads() == 1


# ----------------------------------------------------------- back-off
class _BoomEmbedder(Embedder):
    def __init__(self):
        super().__init__(MuzeConfig().rag.embedding)
        self.calls = 0

    def _embed_local_sync(self, texts):
        self.calls += 1
        raise RuntimeError("boom")


async def test_embedder_backoff_trips_cooldown():
    emb = _BoomEmbedder()
    emb.cfg.max_retries = 1
    emb.cfg.cooldown_sec = 100.0
    with pytest.raises(Exception):
        await emb.embed(["x"])
    assert emb.cooling is True
    assert emb.local_ready is False
    assert emb.healthy is False


class _FallbackEmbedder(Embedder):
    def __init__(self):
        cfg = MuzeConfig().rag.embedding
        cfg.provider = "auto"
        cfg.model = "local-model"
        cfg.remote_model = "remote-model"
        cfg.base_url = "http://remote"
        cfg.api_key = "k"
        super().__init__(cfg)

    def _embed_local_sync(self, texts):
        raise RuntimeError("local down")

    async def _embed_openai(self, texts):
        return [[0.1, 0.2] for _ in texts]


async def test_embedder_falls_back_to_remote():
    emb = _FallbackEmbedder()
    assert emb.available is True
    vecs = await emb.embed(["hello"])
    assert vecs == [[0.1, 0.2]]
