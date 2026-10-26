"""Perception chains (ASR/OCR/value), busy-unload, and anti-ban pacing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

import muze.ingest.backfill as bf_mod
from muze.ai.embeddings import Embedder
from muze.config import (
    AsrConfig,
    LLMConfig,
    LLMEndpoint,
    ModelEntry,
    MuzeConfig,
    OcrConfig,
    RagEmbedding,
    ValueConfig,
)
from muze.perception import AsrChain, OcrChain, ValueScorer
from muze.store.db import open_db
from muze.store.repo import Repository


class FakeLLM:
    """Registry-aware fake: resolve() mirrors the real chain logic."""

    def __init__(self, reply: str = "", **entries: ModelEntry) -> None:
        self.cfg = LLMConfig(chat=LLMEndpoint(), models=dict(entries))
        self.reply = reply

    def resolve(self, role: str):
        reg = self.cfg.effective_models()
        out = []
        for n in self.cfg.role_chain(role):
            e = reg.get(n)
            if e and e.base_url and e.api_key and e.model:
                out.append((n, e))
        return out

    async def chat(self, messages, tools=None, on_chunk=None, role="chat"):
        return {"role": "assistant", "content": self.reply}

    async def vision_describe(self, path, question):
        return "OCR-LLM 你好世界"


def _llm(**entries: ModelEntry) -> FakeLLM:
    return FakeLLM(**entries)


# ----------------------------------------------------------------- ASR
def test_asr_resolve_modes():
    llm = _llm(
        asr=ModelEntry(base_url="https://x", api_key="k", model="whisper-1")
    )
    # auto: api configured -> api
    p, _ = AsrChain(AsrConfig(), llm).resolve()
    assert p == "api"
    # explicit qq -> reserved-stub refusal (no API on the protocol side)
    p, note = AsrChain(AsrConfig(provider="qq"), llm).resolve()
    assert p == "" and "协议侧" in note
    # explicit api without the role -> actionable note
    p, note = AsrChain(AsrConfig(provider="api"), _llm()).resolve()
    assert p == "" and "asr" in note
    # disabled
    assert AsrChain(AsrConfig(provider=""), llm).resolve() == ("", "disabled")


async def test_asr_api_transcription(monkeypatch, tmp_path):
    import muze.perception as perc

    llm = _llm(
        asr=ModelEntry(base_url="https://x/v1", api_key="k", model="whisper-1")
    )
    chain = AsrChain(AsrConfig(language="zh"), llm)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")

    calls: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"text": " 今天天气不错 "}

    class _HC:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            calls["url"] = url
            calls["data"] = kw.get("data")
            return _Resp()

    monkeypatch.setattr(perc.httpx, "AsyncClient", _HC)
    text = await chain.transcribe(wav)
    assert text == "今天天气不错"
    assert calls["url"].endswith("/audio/transcriptions")
    assert calls["data"]["model"] == "whisper-1" and calls["data"]["language"] == "zh"


# ----------------------------------------------------------------- OCR
def test_ocr_resolve_and_llm_path():
    from muze.config import ModelCaps

    llm = _llm(
        ocr=ModelEntry(
            base_url="https://x", api_key="k", model="m", caps=ModelCaps(vision=True)
        )
    )
    # llm provider via registry (local rapidocr not installed in test env)
    chain = OcrChain(OcrConfig(provider="llm"), llm)
    p, note = chain.resolve()
    assert p == "llm", note
    # local without the dependency -> actionable refusal
    p, note = OcrChain(OcrConfig(provider="local"), llm).resolve()
    assert p == "" and "rapidocr" in note


async def test_ocr_extract_via_llm(tmp_path):
    from muze.config import ModelCaps

    llm = _llm(
        ocr=ModelEntry(
            base_url="https://x", api_key="k", model="m", caps=ModelCaps(vision=True)
        )
    )
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    text = await OcrChain(OcrConfig(), llm).extract_text(img)
    assert "OCR-LLM" in text


# --------------------------------------------------------------- value
async def test_value_scoring_parses_and_maps():
    llm = FakeLLM(reply='好的\n{"1": 0, "2": 3, "3": 9, "xx": 1}')
    scorer = ValueScorer(ValueConfig(), llm)
    rows = [
        {"_rid": 11, "text_content": "早"},
        {"_rid": 12, "text_content": "周三下午三点开会"},
        {"_rid": 13, "text_content": "hi"},
    ]
    out = await scorer.score(rows)
    # 9 out of range -> dropped; "xx" unparseable index -> dropped
    assert sorted(out) == [(11, 0), (12, 3)]


async def test_value_scoring_bad_json_raises():
    llm = FakeLLM(reply="no json at all")
    scorer = ValueScorer(ValueConfig(), llm)
    with pytest.raises(ValueError):
        await scorer.score([{"_rid": 1, "text_content": "x"}])
    assert await scorer.score([]) == []


# ---------------------------------------------------- repo + persistence
async def _mk_db(tmp_path, name="m.db"):
    conn = await open_db(str(tmp_path / name))
    return conn, Repository(conn)


async def test_value_columns_and_unjudged(tmp_path):
    conn, repo = await _mk_db(tmp_path)
    try:
        import time as _t

        now = int(_t.time())
        for i, txt in enumerate(["早", "明天交报告", "哈哈哈"]):
            await conn.execute(
                "INSERT INTO messages (scene, peer_id, message_seq, sender_id, time,"
                " raw_json, text_content, source, ingested_at)"
                " VALUES ('friend',7,?,1,?,'{}',?,'event',?)",
                (100 + i, now, txt, now),
            )
        await conn.commit()
        rows = await repo.messages_unjudged(lookback_days=3, limit=10)
        assert len(rows) == 3
        rid = rows[0]["_rid"]
        await repo.set_message_values([(rid, 2)])
        left = await repo.messages_unjudged(3, 10)
        assert len(left) == 2

        # message text update (ASR transcript path)
        await repo.update_message_text("friend", 7, 100, "hello [语音:3s]")
        assert await repo.message_text("friend", 7, 100) == "hello [语音:3s]"
        # lookback window: an old row is never picked up
        await conn.execute(
            "UPDATE messages SET time=time-30*86400 WHERE message_seq=101"
        )
        await conn.commit()
        assert len(await repo.messages_unjudged(3, 10)) == 1
    finally:
        await conn.close()


async def test_schema_v12_value_column(tmp_path):
    conn = await open_db(str(tmp_path / "v12.db"))
    try:
        async with conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ) as cur:
            assert (await cur.fetchone())[0] == "15"
        async with conn.execute("PRAGMA table_info(messages)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "value" in cols
    finally:
        await conn.close()


async def test_media_pending_enrichment(tmp_path):
    conn, repo = await _mk_db(tmp_path, "p.db")
    try:
        import time as _t

        now = int(_t.time())
        for seq, kind, path, summ in [
            (1, "record", "voice/ab/x.wav", None),
            (2, "image", "image/ab/y.jpg", None),
            (3, "image", "image/ab/z.jpg", "already"),
            (4, "video", None, None),
        ]:
            await conn.execute(
                "INSERT INTO media (scene, peer_id, message_seq, seg_index, kind,"
                " file_path, summary, status, duration, created_at, updated_at)"
                " VALUES ('friend',7,?,0,?,?,?,'done',?,?,?)",
                (seq, kind, path, summ, 3 if kind == "record" else None, now, now),
            )
        await conn.commit()
        voice = await repo.media_pending_enrichment(["record"], 10)
        imgs = await repo.media_pending_enrichment(["image"], 10)
        assert len(voice) == 1 and voice[0]["kind"] == "record"
        assert len(imgs) == 1 and imgs[0]["file_path"].endswith("y.jpg")
    finally:
        await conn.close()


# -------------------------------------------------------- busy unload
async def test_busy_unload_under_cpu_pressure():
    e = Embedder(RagEmbedding(busy_unload_cpu_pct=90, busy_unload_gpu_util=95))
    e._local = object()  # pretend a resident model
    e._device = "cuda"

    class Gov:
        def cpu_busy(self):
            return 96.0

        def gpu_util(self):
            return 10.0

    why = await e.unload_if_busy(Gov())
    assert why and "cpu" in why
    assert e._local is None and e._device == ""
    # debounce registered: prepare() refuses to reload right after
    import time as _t

    assert _t.monotonic() - e._last_busy_unload < e._BUSY_RELOAD_GRACE

    # calm machine: nothing happens
    e2 = Embedder(RagEmbedding())
    e2._local = object()

    class Calm:
        def cpu_busy(self):
            return 20.0

        def gpu_util(self):
            return 5.0

    assert await e2.unload_if_busy(Calm()) is None
    assert e2._local is not None


# ----------------------------------------------------- anti-ban pacing
def test_jitter_bounds():
    for _ in range(200):
        v = bf_mod._jittered(1.0, 0.5)
        assert 0.5 <= v <= 1.5
    assert bf_mod._jittered(0, 0.5) == 0.0
    assert bf_mod._jittered(1.0, 0) == 1.0


def test_risk_backoff_streak_doubles_and_risky_words_trip_immediately():
    bf = MuzeConfig().ingest.backfill
    bf_mod._note_pull_success()
    bf_mod._RISK_UNTIL = 0.0  # clear any cooldown left by earlier tests
    step = 0.0
    for _ in range(bf.fail_streak_limit):
        step = bf_mod._note_pull_failure(bf, "connection reset")
    assert step == bf.risk_backoff_sec
    paused, left = bf_mod._risk_paused()
    assert paused and left > 0
    # a success resets the STREAK and the doubling base, but an active
    # cooldown stands (it is a safety pause, not an error counter)
    bf_mod._note_pull_success()
    assert bf_mod._FAIL_STREAK == 0 and bf_mod._RISK_NEXT_SEC == 0.0
    assert bf_mod._risk_paused() == (True, left)  # still paused
    # ...it expires with time; a risk-control-sounding error then trips a
    # FRESH base-step cooldown immediately (no streak needed)
    bf_mod._RISK_UNTIL = 0.0
    step = bf_mod._note_pull_failure(bf, "操作过于频繁，请稍后再试（风控）")
    assert step == bf.risk_backoff_sec
    bf_mod._RISK_UNTIL = 0.0
    bf_mod._note_pull_success()


async def test_budget_take_rolls_over_at_midnight(tmp_path):
    used = await bf_mod._budget_take(str(tmp_path), 5)
    assert used == 5
    used = await bf_mod._budget_take(str(tmp_path), 3)
    assert used == 8
    d = json.loads((tmp_path / "pull_budget.json").read_text("utf-8"))
    assert d["pages"] == 8 and len(d["date"]) == 10
    # a stale file from yesterday resets the counter
    (tmp_path / "pull_budget.json").write_text(
        json.dumps({"date": "2000-01-01", "pages": 99999}), "utf-8"
    )
    assert await bf_mod._budget_take(str(tmp_path), 2) == 2
