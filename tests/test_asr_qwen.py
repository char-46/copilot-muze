"""Qwen3-ASR local backend: provider resolution, language mapping,
timestamp formatting, idle unload, rapidocr dual-module fallback, and the
media.asr_ts (schema v13) persistence path."""

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from muze.config import AsrConfig
from muze.perception import AsrChain, OcrChain, QwenAsrBackend


class _FakeLLM:
    def resolve(self, role):
        return []


def _install_fake_qwen(monkeypatch, result):
    mod = types.ModuleType("qwen_asr")
    calls = {}

    class Qwen3ASRModel:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls["model_id"] = model_id
            calls["kwargs"] = kwargs
            m = Qwen3ASRModel()
            return m

        def transcribe(self, audio, language=None, return_time_stamps=False):
            calls["audio"] = audio
            calls["language"] = language
            calls["want_ts"] = return_time_stamps
            return [result]

    mod.Qwen3ASRModel = Qwen3ASRModel
    monkeypatch.setitem(sys.modules, "qwen_asr", mod)
    return calls


def _result(text="你好世界", ts=True):
    stamps = (
        [
            SimpleNamespace(text="你", start_time=0.10, end_time=0.25),
            SimpleNamespace(text="好", start_time=0.25, end_time=0.40),
        ]
        if ts
        else None
    )
    return SimpleNamespace(text=text, time_stamps=stamps, language="Chinese")


# ---------------------------------------------------------------- resolve
async def test_qwen_unavailable_without_package():
    chain = AsrChain(AsrConfig(provider="qwen"), _FakeLLM())
    provider, note = chain.resolve()
    assert provider == "" and "qwen-asr" in note


def test_qwen_resolves_with_package(monkeypatch):
    _install_fake_qwen(monkeypatch, _result())
    chain = AsrChain(AsrConfig(provider="qwen"), _FakeLLM())
    assert chain.resolve() == ("qwen", "")
    # auto prefers qwen when no api role and no faster-whisper
    chain = AsrChain(AsrConfig(provider="auto"), _FakeLLM())
    assert chain.resolve() == ("qwen", "")


# ------------------------------------------------------------ transcribe
async def test_qwen_transcribe_full_language_and_ts(monkeypatch, tmp_path):
    calls = _install_fake_qwen(monkeypatch, _result())
    cfg = AsrConfig(provider="qwen", language="zh")
    chain = AsrChain(cfg, _FakeLLM())
    text, ts = await chain.transcribe_full(tmp_path / "a.wav")
    assert text == "你好世界"
    assert ts == [["你", 0.1, 0.25], ["好", 0.25, 0.4]]
    assert calls["language"] == "Chinese"  # zh -> Chinese
    assert calls["want_ts"] is True
    assert calls["kwargs"]["forced_aligner"] == cfg.aligner_model


async def test_qwen_no_aligner_disables_timestamps(monkeypatch, tmp_path):
    calls = _install_fake_qwen(monkeypatch, _result(ts=False))
    cfg = AsrConfig(provider="qwen", language="en", aligner_model="")
    chain = AsrChain(cfg, _FakeLLM())
    text, ts = await chain.transcribe_full(tmp_path / "a.wav")
    assert text and ts == []
    assert calls["want_ts"] is False
    assert "forced_aligner" not in calls["kwargs"]
    assert calls["language"] == "English"


async def test_qwen_unknown_language_autodetect(monkeypatch, tmp_path):
    calls = _install_fake_qwen(monkeypatch, _result())
    chain = AsrChain(AsrConfig(provider="qwen", language="xx"), _FakeLLM())
    await chain.transcribe_full(tmp_path / "a.wav")
    assert calls["language"] is None


async def test_qwen_transcribe_plain(monkeypatch, tmp_path):
    _install_fake_qwen(monkeypatch, _result())
    chain = AsrChain(AsrConfig(provider="qwen"), _FakeLLM())
    assert await chain.transcribe(tmp_path / "a.wav") == "你好世界"


# ------------------------------------------------------------- lifecycle
def test_qwen_unload_if_idle(monkeypatch):
    _install_fake_qwen(monkeypatch, _result())
    cfg = AsrConfig(provider="qwen", qwen_unload_idle_sec=60)
    back = QwenAsrBackend(cfg)
    back._model = object()
    back._last_used = 0.0
    assert back.unload_if_idle(now=100) is True
    assert back._model is None
    # a fresh model stays loaded
    back._model = object()
    back._last_used = 100.0
    assert back.unload_if_idle(now=110) is False


def test_qwen_cpu_device_short_circuits():
    back = QwenAsrBackend(AsrConfig(qwen_device="cpu"))
    assert back._pick_device() == ("cpu", "float32")


# ------------------------------------------------------------------- OCR
def test_rapidocr_unified_module_name(monkeypatch):
    mod = types.ModuleType("rapidocr")
    mod.RapidOCR = object
    monkeypatch.setitem(sys.modules, "rapidocr", mod)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)  # block 1.3 name
    chain = OcrChain.__new__(OcrChain)  # resolve() only needs _rapidocr import
    from muze.perception import _rapidocr

    assert _rapidocr() is mod.RapidOCR


# ------------------------------------------------------------ schema v13
async def test_media_asr_ts_column(tmp_path):
    from muze.store.db import open_db
    from muze.store.repo import Repository

    conn = await open_db(str(tmp_path / "t.db"))
    try:
        async with conn.execute("PRAGMA table_info(media)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "asr_ts" in cols
        async with conn.execute("SELECT value FROM meta WHERE key='schema_version'") as cur:
            assert (await cur.fetchone())[0] == "13"
        repo = Repository(conn)
        now = 1700000000
        await repo.add_media({
            "resource_id": None, "file_id": "f1", "file_name": None,
            "file_hash": None, "width": None, "height": None, "duration": 3,
            "summary": None, "scene": "friend", "peer_id": 1, "message_seq": 9,
            "seg_index": 0, "kind": "record", "status": "pending",
            "created_at": now, "updated_at": now,
        })
        row = (await repo.media_for_message("friend", 1, 9))[0]
        ts = json.dumps([["你", 0.1, 0.25]], ensure_ascii=False)
        await repo.update_media(row["id"], status="done", summary="[转写] 你", asr_ts=ts)
        got = await repo.media_by_id(row["id"])
        assert got["asr_ts"] == ts
        assert got["summary"] == "[转写] 你"
    finally:
        await conn.close()
