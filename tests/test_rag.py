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

    async def prepare(self, governor=None, force: bool = False) -> None:
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


async def test_rag_chat_covers_all_conversations_no_starvation(repo):
    """Regression: _index_chat used to visit only the 300 most-recently-active
    conversations (list_conversations' cap ordered by last_active_at DESC) —
    with 346 in the archive the least-active 46 were starved forever: they sat
    in the progress denominator but never got windows. Coverage must reach
    every conversation regardless of activity."""
    from muze.provider.milky.models import IncomingMessage

    cfg = MuzeConfig()
    cfg.rag.enabled = True
    cfg.rag.chat = True
    vec = VecStore(repo)
    await vec.setup()
    indexer = RagIndexer(repo, vec, FakeEmbedder(), cfg)

    n = 305  # just past the old 300 cap
    for i in range(1, n + 1):
        scene = "friend" if i % 2 else "group"
        text = f"会话{i}的消息"
        await repo.upsert_message(
            IncomingMessage.from_raw(
                {
                    "message_scene": scene,
                    "peer_id": i,
                    "message_seq": 1,
                    "sender_id": i,
                    "time": 1790000000 + i,
                    "segments": [{"type": "text", "data": {"text": text}}],
                }
            ),
            text,
            "event",
        )

    await indexer.run_once(force=True)

    prog = await repo.rag_chat_progress()
    assert prog["total_conversations"] == n
    assert prog["messages_covered"] == prog["messages_total"] == n


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
    # a busy GPU is NOT a veto anymore: indexing just runs on CPU
    g = _gov(power=True, cpu=5, gpu=99, free=8)
    assert g.idle(last_activity=0)[0] is True
    g = _gov(power=True, cpu=5, gpu=0, free=8)
    import time as _t
    assert g.idle(last_activity=_t.time())[0] is False  # recent activity
    assert g.idle(last_activity=0)[0] is True


def test_governor_device_tiers():
    # comfortable: free covers model + reserve -> cuda
    assert _gov(power=True, cpu=5, gpu=5, free=8).choose_device() == "cuda"
    # squeeze: free covers the model (1.6G) but not the reserve -> still cuda
    assert _gov(power=True, cpu=5, gpu=5, free=2).choose_device() == "cuda"
    # cannot even fit the model -> cpu
    assert _gov(power=True, cpu=5, gpu=5, free=1).choose_device() == "cpu"
    # fits, but the GPU is busy -> cpu
    assert _gov(power=True, cpu=5, gpu=99, free=8).choose_device() == "cpu"
    # explicit device wins
    cfg = MuzeConfig()
    cfg.rag.embedding.device = "cuda"
    assert ResourceGovernor(cfg.rag).choose_device() == "cuda"
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


# ------------------------------------------------------ query_context
async def test_query_context_retrieves_and_never_raises(repo):
    await repo.upsert_profile("friend/1", "老王", "老王喜欢钓鱼，昵称 渔翁")
    cfg = MuzeConfig()
    cfg.rag.enabled = True
    vec = VecStore(repo)
    await vec.setup()
    indexer = RagIndexer(repo, vec, FakeEmbedder(), cfg)
    await indexer.run_once()

    hits = await indexer.query_context("钓鱼")
    assert hits and any("钓鱼" in h["text"] for h in hits)
    # blank / disabled -> [] without raising
    assert await indexer.query_context("   ") == []
    class Off(RagIndexer):
        @property
        def enabled(self):
            return False
    off = Off(repo, vec, FakeEmbedder(), cfg)
    assert await off.query_context("钓鱼") == []


# ------------------------------------------------------ forced indexing
class _BlockingGovernor:
    def idle(self, last_activity=0.0):
        return False, "always busy"

    def snapshot(self):
        return {"power": True, "cpu_pct": 5.0, "gpu_util": 2.0, "gpu_free_gb": 8.0}


async def test_run_once_force_bypasses_governor(repo):
    await repo.add_memory("fact", "owner", "主人喜欢喝乌龙茶", source="t")
    cfg = MuzeConfig()
    cfg.rag.enabled = True
    vec = VecStore(repo)
    await vec.setup()
    indexer = RagIndexer(repo, vec, FakeEmbedder(), cfg, governor=_BlockingGovernor())
    assert await indexer.run_once() == 0  # gated off
    assert await indexer.run_once(force=True) >= 1  # forced through


async def test_run_forced_runs_until_stopped(repo):
    import asyncio as _a

    cfg = MuzeConfig()
    cfg.rag.enabled = True
    vec = VecStore(repo)
    await vec.setup()
    indexer = RagIndexer(repo, vec, FakeEmbedder(), cfg)

    stop = _a.Event()
    stop.set()  # already stopping -> returns immediately, flag restored
    await indexer.run_forced(stop)
    assert indexer.forced is False


async def test_rag_chat_progress(repo):
    from muze.provider.milky.models import IncomingMessage

    def mk(seq, text):
        return IncomingMessage.from_raw(
            {
                "message_scene": "group", "peer_id": 7, "message_seq": seq,
                "sender_id": 42, "time": 1790000000 + seq,
                "segments": [{"type": "text", "data": {"text": text}}],
            }
        )

    for seq in (1, 2, 3, 4, 5):
        await repo.upsert_message(mk(seq, f"msg{seq}"), f"msg{seq}", "event")
    await repo.rag_set_state("chat:group:7", "2")
    prog = await repo.rag_chat_progress()
    assert prog["total_conversations"] == 1
    # true coverage: messages with seq <= cursor (2 of 5), NOT seq arithmetic
    assert prog["messages_covered"] == 2 and prog["messages_total"] == 5
    conv = prog["conversations"][0]
    assert conv["scene"] == "group" and conv["peer_id"] == 7
    assert conv["cursor"] == 2 and conv["last_seq"] == 5


# ------------------------------------------------------ /rag command
class _CmdRag:
    enabled = True

    def __init__(self):
        self.forced = False
        self.governor = None  # None -> the /rag report skips the resource line

    async def status(self):
        return {
            "enabled": True, "chat_enabled": False, "running": self.forced,
            "forced": self.forced, "docs": 3, "docs_by_kind": {"memory": 3},
            "vec_model": "fake", "vec_dims": 8, "embedding_model": "fake",
            "embedding_available": True, "embedding_healthy": True,
            "last_index_at": 0.0, "last_count": 3,
        }

    async def run_forced(self, stop):
        self.forced = True
        try:
            await stop.wait()
        finally:
            self.forced = False


async def test_rag_command_status_run_stop(repo):
    import asyncio as _a

    from muze.chatlog import ChatLog
    from muze.core.service import ControlContext, ControlService, Runtime
    from test_permissions import Recorder

    svc = ControlService(MuzeConfig(), Recorder(), repo, ChatLog(), Runtime())
    fake = _CmdRag()
    svc.runtime.rag = fake
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)

    out = await svc.run_command("rag", ctx)
    assert "文档3篇" in out.text and "memory:3" in out.text

    out = await svc.run_command("rag run", ctx)
    assert "已启动强制索引" in out.text
    await _a.sleep(0.01)
    assert fake.forced is True
    assert "强制索引中" in (await svc.run_command("rag", ctx)).text

    out = await svc.run_command("rag stop", ctx)
    assert "已停止" in out.text
    for _ in range(50):
        if not fake.forced:
            break
        await _a.sleep(0.01)
    assert fake.forced is False
    out = await svc.run_command("rag stop", ctx)
    assert "未在运行" in out.text


# ------------------------------------------------------ "." command prefix
async def test_dot_prefix_commands(repo):
    from muze.chatlog import ChatLog
    from muze.core.service import ControlContext, ControlService, Runtime
    from test_permissions import Recorder

    svc = ControlService(MuzeConfig(), Recorder(), repo, ChatLog(), Runtime())
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0)

    v = await svc.execute(".version", ctx)
    assert not v.is_error and "0.1.0" in v.text
    v = await svc.execute("/version", ctx)
    assert not v.is_error  # "/" still works
    v = await svc.execute(".不存在的命令", ctx)
    assert v.is_error and "未知命令" in v.text


# ------------------------------------------------------ embedder stickiness
class _StickyEmbedder(Embedder):
    def __init__(self):
        cfg = MuzeConfig().rag.embedding
        cfg.provider = "local"
        cfg.model = "local-model"
        super().__init__(cfg)
        self.loads: list[str] = []

    async def _load_local(self, device, governor=None, full_threads=False):
        self.loads.append(device)
        self._device = device
        self._local = object()


class _CudaGovernor:
    def choose_device(self):
        return "cuda"


async def test_embedder_device_sticky_no_reload_thrash():
    emb = _StickyEmbedder()
    emb._local = object()
    emb._device = "cpu"
    await emb.prepare(_CudaGovernor())  # loaded already -> stays on cpu
    assert emb.loads == []
    emb._local = None  # idle-unloaded -> next prepare re-picks the device
    await emb.prepare(_CudaGovernor())
    assert emb.loads == ["cuda"]


async def test_embedder_force_prepare_repicks_device():
    emb = _StickyEmbedder()
    emb._local = object()
    emb._device = "cpu"
    await emb.prepare(_CudaGovernor(), force=True)  # explicit run re-picks
    assert emb.loads == ["cuda"]


async def test_run_forced_repicks_device_before_first_pass(repo):
    import asyncio as _a

    class _Forced(RagIndexer):
        prepared = []

        async def _prepare(self, force: bool = False) -> None:
            self.prepared.append(force)

        async def run_once(self, force: bool = False, stop=None) -> int:
            if stop is not None:
                stop.set()
            return 0

    cfg = MuzeConfig()
    vec = VecStore(repo)
    await vec.setup()
    idx = _Forced(repo, vec, FakeEmbedder(), cfg)
    stop = _a.Event()
    await idx.run_forced(stop)
    assert _Forced.prepared == [True]  # device re-picked once, forced


def test_governor_force_uses_full_thread_cap():
    assert _gov(power=True, cpu=90, gpu=0, free=8).cpu_threads(force=True) == 4
    assert _gov(power=True, cpu=100, gpu=0, free=8).cpu_threads() == 1  # busy-scaled


# ------------------------------------------------------ auto-inject
class FakeRag:
    enabled = True

    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    async def query_context(self, q, k=None):
        self.queries.append((q, k))
        return self.hits


async def test_system_prompt_auto_injects_rag_hits(repo):
    from muze.chatlog import ChatLog
    from muze.core.service import ControlContext, ControlService, Runtime
    from test_inject import StubAdapter, make_agent

    cfg = MuzeConfig()
    cfg.rag.enabled = True
    cfg.rag.auto_inject = True
    cfg.rag.inject_k = 3
    agent, service = make_agent(repo, cfg)
    rag = FakeRag([{"kind": "chat", "text": "张三: 又加班到半夜", "scene": "group", "peer_id": 7}])
    service.runtime.rag = rag
    ctx = ControlContext(source="qq", chat_key="k", scene="group", peer_id=7)

    prompt = await agent._system_prompt(ctx, query="谁抱怨过加班")
    assert "语义检索命中" in prompt and "又加班到半夜" in prompt
    assert rag.queries == [("谁抱怨过加班", 3)]

    # no query (bare resume) -> no retrieval, no section
    p2 = await agent._system_prompt(ctx)
    assert rag.queries == [("谁抱怨过加班", 3)] and "语义检索命中" not in p2

    # switch off -> nothing injected even with a query
    cfg.rag.auto_inject = False
    p3 = await agent._system_prompt(ctx, query="谁抱怨过加班")
    assert "语义检索命中" not in p3 and len(rag.queries) == 1
