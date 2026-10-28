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


async def test_qwen_second_call_reuses_model(monkeypatch, tmp_path):
    """Regression: `import time` used to live INSIDE the cold-load branch,
    so every call after the first crashed with UnboundLocalError at
    `self._last_used = time.time()` — i.e. all real transcriptions died
    after the model was warm, exactly one step before returning text."""
    calls = _install_fake_qwen(monkeypatch, _result())
    chain = AsrChain(AsrConfig(provider="qwen"), _FakeLLM())
    first = await chain.transcribe_full(tmp_path / "a.wav")
    second = await chain.transcribe_full(tmp_path / "b.wav")  # model cached
    assert first == second == ("你好世界", [["你", 0.1, 0.25], ["好", 0.25, 0.4]])
    assert calls["audio"].endswith("b.wav")  # second call really ran
    assert chain._qwen._last_used > 0  # touch survived the reuse path


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
            assert (await cur.fetchone())[0] == "19"
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


# ------------------------------------------------- transcribe_media_row
async def test_transcribe_media_row_writeback(tmp_path, monkeypatch):
    from muze.perception import transcribe_media_row
    from muze.store.db import open_db
    from muze.store.repo import Repository

    _install_fake_qwen(monkeypatch, _result())
    conn = await open_db(str(tmp_path / "t.db"))
    try:
        repo = Repository(conn)
        from muze.provider.milky.models import IncomingMessage

        now = 1700000000
        await repo.upsert_message(
            IncomingMessage.from_raw({
                "message_scene": "friend", "peer_id": 1, "message_seq": 7,
                "sender_id": 7, "time": now,
                "segments": [{"type": "text", "data": {"text": "[语音:3s]"}}],
            }),
            "[语音:3s]", "event",
        )
        await repo.add_media({
            "resource_id": None, "file_id": "f1", "file_name": None,
            "file_hash": None, "width": None, "height": None, "duration": 3,
            "summary": None, "scene": "friend", "peer_id": 1, "message_seq": 7,
            "seg_index": 0, "kind": "record", "status": "done",
            "file_path": "x.wav",
            "created_at": now, "updated_at": now,
        })
        row = (await repo.media_for_message("friend", 1, 7))[0]
        chain = AsrChain(AsrConfig(provider="qwen"), _FakeLLM())

        # fake audio file exists so the exists() check passes
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"RIFF")

        class _Store:
            def abs_path(self, m):
                return wav

        text, ts = await transcribe_media_row(chain, repo, row, _Store())
        assert text == "你好世界"
        got = await repo.media_by_id(row["id"])
        assert got["summary"].startswith("[转写] 你好世界")
        assert json.loads(got["asr_ts"])[0] == ["你", 0.1, 0.25]
        msg = await repo.message_text("friend", 1, 7)
        assert "[语音转写] 你好世界" in msg
    finally:
        await conn.close()


# ------------------------------------------------------- /reenrich reset
async def test_media_reenrich_reset(tmp_path):
    from muze.store.db import open_db
    from muze.store.repo import Repository

    conn = await open_db(str(tmp_path / "t.db"))
    try:
        repo = Repository(conn)
        now = 1700000000
        base = {
            "resource_id": None, "file_id": None, "file_name": None,
            "file_hash": None, "width": None, "height": None, "duration": 3,
            "file_path": "x", "summary": "[转写] 旧", "asr_ts": "[[旧,0,1]]",
            "status": "done", "created_at": now, "updated_at": now,
        }
        await repo.add_media({**base, "scene": "friend", "peer_id": 1,
                              "message_seq": 1, "seg_index": 0, "kind": "record"})
        await repo.add_media({**base, "scene": "friend", "peer_id": 1,
                              "message_seq": 2, "seg_index": 0, "kind": "image"})
        await repo.add_media({**base, "scene": "group", "peer_id": 9,
                              "message_seq": 1, "seg_index": 0, "kind": "record"})
        n = await repo.media_reenrich("friend", 1, ["record"])
        assert n == 1
        r = (await repo.media_for_message("friend", 1, 1))[0]
        assert r["summary"] is None and r["asr_ts"] is None
        img = (await repo.media_for_message("friend", 1, 2))[0]
        assert img["summary"] == "[转写] 旧"  # other kind untouched
        other = (await repo.media_for_message("group", 9, 1))[0]
        assert other["summary"] == "[转写] 旧"  # other conversation untouched
        # back on the enrichment queue after reset
        pend = await repo.media_pending_enrichment(["record"], 10)
        assert [p["id"] for p in pend] == [r["id"]]
    finally:
        await conn.close()


# -------------------------------------------------- ocr_image local-first
async def test_ocr_image_prefers_local_chain(tmp_path):
    from muze.ai.tools import ToolRunner
    from muze.config import MuzeConfig
    from muze.core.service import ControlService, Runtime
    from muze.chatlog import ChatLog
    from muze.store.db import open_db
    from muze.store.repo import Repository
    from test_permissions import Recorder

    conn = await open_db(str(tmp_path / "t.db"))
    try:
        repo = Repository(conn)
        svc = ControlService(MuzeConfig(), Recorder(), repo, ChatLog(), Runtime())
        now = 1700000000
        await repo.add_media({
            "resource_id": None, "file_id": None, "file_name": None,
            "file_hash": None, "width": None, "height": None, "duration": None,
            "file_path": "a.png", "summary": None, "scene": "friend", "peer_id": 1,
            "message_seq": 3, "seg_index": 0, "kind": "image", "status": "done",
            "created_at": now, "updated_at": now,
        })

        class _LocalOcr:
            def resolve(self):
                return "local", ""

            async def extract_text(self, path):
                return "本地OCR结果"

        svc.runtime.ocr = _LocalOcr()
        runner = ToolRunner.__new__(ToolRunner)
        runner.repo = repo
        runner.service = svc
        runner.llm = None
        out = await runner.t_ocr_image(
            {"scene": "friend", "peer_id": 1, "message_seq": 3}
        )
        assert out["source"] == "local-ocr" and out["text"] == "本地OCR结果"
    finally:
        await conn.close()


# ------------------------------------------- transcribe_voice tool (cache)
async def test_transcribe_voice_tool_cached(tmp_path):
    from muze.ai.tools import ToolRunner
    from muze.config import MuzeConfig
    from muze.core.service import ControlService, Runtime
    from muze.chatlog import ChatLog
    from muze.store.db import open_db
    from muze.store.repo import Repository
    from test_permissions import Recorder

    conn = await open_db(str(tmp_path / "t.db"))
    try:
        repo = Repository(conn)
        svc = ControlService(MuzeConfig(), Recorder(), repo, ChatLog(), Runtime())
        now = 1700000000
        await repo.add_media({
            "resource_id": None, "file_id": None, "file_name": None,
            "file_hash": None, "width": None, "height": None, "duration": 3,
            "file_path": "a.wav",
            "summary": "[转写] 缓存的转写", "asr_ts": '[["缓",0.0,0.5]]',
            "scene": "friend", "peer_id": 1, "message_seq": 4, "seg_index": 0,
            "kind": "record", "status": "done",
            "created_at": now, "updated_at": now,
        })
        svc.runtime.asr = object()  # present but must not be hit (cache path)
        runner = ToolRunner.__new__(ToolRunner)
        runner.repo = repo
        runner.service = svc
        runner.llm = None
        out = await runner.t_transcribe_voice(
            {"scene": "friend", "peer_id": 1, "message_seq": 4}
        )
        assert out["text"] == "缓存的转写"
        assert out["timestamps"] == [["缓", 0.0, 0.5]]
    finally:
        await conn.close()


# --------------------------------------------------- AMR -> wav conversion
def test_ensure_wav_passthrough_and_no_ffmpeg(tmp_path, monkeypatch):
    from muze.perception import ensure_wav

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    assert ensure_wav(wav) == wav  # wav passes through untouched
    monkeypatch.setattr("muze.perception._ffmpeg_exe", lambda: None)
    amr = tmp_path / "b.amr"
    amr.write_bytes(b"#!AMR")
    assert ensure_wav(amr) == amr  # no ffmpeg: original returned as-is


def test_ensure_wav_converts_and_caches(tmp_path, monkeypatch):
    from muze.perception import ensure_wav

    amr = tmp_path / "v.amr"
    amr.write_bytes(b"#!AMR")
    runs = []

    class _R:
        returncode = 0

    def _run(cmd, **k):
        runs.append(cmd)
        out = cmd[-1]
        Path(out).write_bytes(b"RIFF16k")
        return _R()

    monkeypatch.setattr("muze.perception._ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr("subprocess.run", _run)
    out = ensure_wav(amr)
    assert out.suffixes[-2:] == [".conv", ".wav"] and out.exists()
    assert "-ar" in runs[0] and "16000" in runs[0]
    out2 = ensure_wav(amr)  # sidecar cache: no second conversion
    assert out2 == out and len(runs) == 1


async def test_transcribe_media_row_amr(tmp_path, monkeypatch):
    """QQ AMR voice: the clip handed to the backend must be the converted
    16k wav sidecar, never the raw AMR blob."""
    from muze.perception import transcribe_media_row
    from muze.provider.milky.models import IncomingMessage
    from muze.store.db import open_db
    from muze.store.repo import Repository

    calls = _install_fake_qwen(monkeypatch, _result())
    conn = await open_db(str(tmp_path / "t.db"))
    try:
        repo = Repository(conn)
        now = 1700000000
        await repo.upsert_message(
            IncomingMessage.from_raw({
                "message_scene": "friend", "peer_id": 1, "message_seq": 7,
                "sender_id": 7, "time": now,
                "segments": [{"type": "text", "data": {"text": "[语音:2s]"}}],
            }),
            "[语音:2s]", "event",
        )
        await repo.add_media({
            "resource_id": None, "file_id": "f1", "file_name": None,
            "file_hash": None, "width": None, "height": None, "duration": 2,
            "summary": None, "scene": "friend", "peer_id": 1, "message_seq": 7,
            "seg_index": 0, "kind": "record", "status": "done",
            "file_path": "v.amr",
            "created_at": now, "updated_at": now,
        })
        row = (await repo.media_for_message("friend", 1, 7))[0]

        class _R:
            returncode = 0

        def _run(cmd, **k):
            Path(cmd[-1]).write_bytes(b"RIFF")
            return _R()

        monkeypatch.setattr("muze.perception._ffmpeg_exe", lambda: "ffmpeg")
        monkeypatch.setattr("subprocess.run", _run)

        class _Store:
            def abs_path(self, m):
                return tmp_path / "v.amr"

        (tmp_path / "v.amr").write_bytes(b"#!AMR")
        text, ts = await transcribe_media_row(
            AsrChain(AsrConfig(provider="qwen"), _FakeLLM()), repo, row, _Store()
        )
        assert text == "你好世界" and ts
        assert calls["audio"].endswith(".conv.wav")  # converted, not the AMR
    finally:
        await conn.close()
