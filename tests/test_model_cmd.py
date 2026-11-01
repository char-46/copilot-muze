"""/model: manual control over EVERY local model (user request 2026-11-01:
手动把模型移到 ram/vram 或加载、卸载模型所有的本地模型)."""

import pytest

from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.chatlog import ChatLog


class FakeEmbedder:
    def __init__(self):
        self.cfg = type("C", (), {"model": "bge-small"})()
        self._loaded = True
        self.dev = "gpu"
        self.calls = []

    @property
    def loaded(self):
        return self._loaded

    @property
    def device_label(self):
        return self.dev if self._loaded else ""

    @property
    def model_id(self):
        return "bge-small"

    async def prepare(self, gov=None, force=False):
        self.calls.append(("prepare", force))
        self._loaded = True
        self.dev = "gpu"

    async def force_unload(self):
        self.calls.append(("unload",))
        was, self._loaded = self._loaded, False
        self.dev = ""
        return was

    async def move(self, device, gov=None):
        self.calls.append(("move", device))
        self.dev = "gpu" if device == "cuda" else "ram"
        return f"已迁移到 {'GPU(vram)' if device == 'cuda' else '内存(ram)'}"


class FakeAsr:
    def __init__(self):
        self.loaded_flag = True
        self.dev = "gpu"

    def resolve(self):
        return "qwen", ""

    def local_loaded(self):
        return self.loaded_flag

    def local_device(self):
        return self.dev if self.loaded_flag else ""

    async def load_local(self):
        self.loaded_flag = True
        return "qwen-asr 已加载（cuda:0）"

    def unload_local(self):
        was, self.loaded_flag = self.loaded_flag, False
        return "qwen-asr" if was else ""

    async def move_local(self, device):
        self.dev = "gpu" if device == "cuda" else "ram"
        return f"qwen-asr 已迁移到 cuda:0" if device == "cuda" else "qwen-asr 已迁移到 cpu"


class FakeOcr:
    def resolve(self):
        return "local", ""

    def local_loaded(self):
        return False

    def local_device(self):
        return ""

    async def load_local(self):
        return "rapidocr 已加载（cpu）"

    def unload_local(self):
        return ""

    async def move_local(self, device):
        return "rapidocr(onnxruntime) 仅支持 CPU，不可迁 GPU"


class FakeRag:
    def __init__(self):
        self.embedder = FakeEmbedder()
        self.governor = object()


@pytest.fixture
async def svc(tmp_path):
    from muze.store.db import open_db
    from muze.store.repo import Repository

    conn = await open_db(str(tmp_path / "m.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)

    class NoAdapter:
        pass

    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    service.runtime.rag = FakeRag()
    service.runtime.asr = FakeAsr()
    service.runtime.ocr = FakeOcr()
    yield service
    await conn.close()


async def test_model_status_lists_every_local_model(svc):
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    out = await svc.run_command("model", ctx)
    assert "embedding" in out.text and "qwen-asr" in out.text and "rapidocr" in out.text
    assert "已加载 · 显存(vram)" in out.text


async def test_model_unload_releases_all(svc):
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    out = await svc.run_command("model unload", ctx)
    assert "embedding" in out.text and "qwen-asr" in out.text
    assert svc.runtime.rag.embedder.calls[-1] == ("unload",)
    assert svc.runtime.asr.loaded_flag is False


async def test_model_load_warms_all(svc):
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    svc.runtime.rag.embedder._loaded = False
    svc.runtime.asr.loaded_flag = False
    out = await svc.run_command("model load", ctx)
    assert "embedding" in out.text and "qwen-asr 已加载" in out.text and "rapidocr 已加载" in out.text
    assert svc.runtime.rag.embedder._loaded is True


async def test_model_ram_and_vram_move_all(svc, monkeypatch):
    import muze.perception as P

    monkeypatch.setattr(P, "_cuda_available", lambda: True)
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)

    out = await svc.run_command("model ram", ctx)
    assert "内存(ram)" in out.text and svc.runtime.rag.embedder.dev == "ram"

    out = await svc.run_command("model vram", ctx)
    assert "GPU(vram)" in out.text and svc.runtime.rag.embedder.dev == "gpu"
    assert "仅支持 CPU" in out.text  # rapidocr honestly refuses the GPU


async def test_model_vram_without_cuda_is_refused(svc, monkeypatch):
    import muze.perception as P

    monkeypatch.setattr(P, "_cuda_available", lambda: False)
    ctx = ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)
    out = await svc.run_command("model vram", ctx)
    assert out.is_error and "CUDA" in out.text
