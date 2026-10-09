"""Perception chains: voice transcription (ASR), image OCR, and message
value recognition.

All LLM-bound work routes through the multi-model registry (llm.roles):
- asr  -> api provider (OpenAI-compatible /audio/transcriptions)
- ocr  -> multimodal model reads the image verbatim
- value-> a cheap model scores messages 0..3

Local backends are optional dependencies (faster-whisper, rapidocr) that
degrade gracefully to "unsupported" when not installed; "qq" (protocol-side
transcription) is reserved — Milky exposes no such API today.

Everything here runs in background enrichment passes, never inline with
message ingestion: perception is best-effort, interruptible, and budgeted.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx

from muze.ai.llm import LLMClient
from muze.config import AsrConfig, OcrConfig, ValueConfig

log = logging.getLogger("muze.perception")

_RISK_WORDS = ("风控", "冻结", "frozen", "risk", "blocked", "too many", "频率")


# ------------------------------------------------------------------- ASR
def _asr_api_entry(llm: LLMClient):
    chain = llm.resolve("asr")
    if not chain:
        return None
    # prefer entries that DECLARE audio capability (catalog caps.asr),
    # e.g. MiMo's ASR-capable ids; any endpoint still works as a fallback
    declared = [(n, e) for n, e in chain if getattr(e.caps, "asr", False)]
    return (declared or chain)[0][1]


def _faster_whisper():
    try:
        from faster_whisper import WhisperModel  # type: ignore

        return WhisperModel
    except Exception:
        return None


class AsrChain:
    """provider pick: api (registry asr role) | local (faster-whisper) |
    qq (reserved stub) | auto. resolve() reports what is actually usable."""

    def __init__(self, cfg: AsrConfig, llm: LLMClient) -> None:
        self.cfg = cfg
        self.llm = llm
        self._local_model = None  # lazy faster-whisper instance

    def resolve(self) -> tuple[str, str]:
        """(active_provider, note) — "" provider means unavailable."""
        p = (self.cfg.provider or "").strip().lower()
        if p in ("", "off", "none"):
            return "", "disabled"
        if p == "qq":
            # reserved: the protocol side (LLBot/Milky) has no transcription
            # API; kept as a config target so it can light up without a
            # config change the day one appears
            return "", "qq: 协议侧无语音转写 API"
        if p == "api":
            if _asr_api_entry(self.llm) is None:
                return "", "api: llm.roles 未配置 asr 角色"
            return "api", ""
        if p == "local":
            if _faster_whisper() is None:
                return "", "local: 未安装 faster-whisper"
            return "local", ""
        # auto: api first (no local footprint), then local, then qq
        if _asr_api_entry(self.llm) is not None:
            return "api", ""
        if _faster_whisper() is not None:
            return "local", ""
        return "", "auto: 无可用 ASR 后端（配置 api 角色或安装 faster-whisper）"

    async def transcribe(self, wav_path: str | Path, duration_sec: float = 0.0) -> str:
        provider, note = self.resolve()
        if not provider:
            raise RuntimeError(note or "ASR 不可用")
        if provider == "api":
            return await self._transcribe_api(Path(wav_path), duration_sec)
        return await self._transcribe_local(Path(wav_path))

    async def _transcribe_api(self, p: Path, duration_sec: float = 0.0) -> str:
        chain = self.llm.resolve("asr")
        declared = [(n, e) for n, e in chain if getattr(e.caps, "asr", False)]
        name, e = (declared or chain)[0]
        base = e.base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=120) as hc:
            with open(p, "rb") as f:
                r = await hc.post(
                    f"{base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {e.api_key}"},
                    files={"file": (p.name, f, "audio/wav")},
                    data={
                        "model": e.model,
                        "language": self.cfg.language or None,
                        "response_format": "json",
                    },
                )
        if r.status_code != 200:
            raise RuntimeError(f"asr api {r.status_code}: {r.text[:200]}")
        text = (r.json().get("text") or "").strip()
        # ledger: audio length drives the per-minute price when declared
        rec = getattr(self.llm, "record_usage", None)
        if rec is not None:
            from muze.model_catalog import Usage

            rec(name, Usage(audio_sec=float(duration_sec or 0.0)))
        return text

    async def _transcribe_local(self, p: Path) -> str:
        WhisperModel = _faster_whisper()
        if WhisperModel is None:
            raise RuntimeError("local: 未安装 faster-whisper")
        if self._local_model is None:
            self._local_model = WhisperModel(
                self.cfg.local_model, device="cpu", compute_type="int8"
            )
        segments, _info = await _to_thread(
            self._local_model.transcribe, str(p), language=self.cfg.language or None
        )

        def _collect():
            return " ".join(s.text.strip() for s in segments).strip()

        return await _to_thread(_collect)


async def _to_thread(fn, *a):
    import asyncio

    return await asyncio.to_thread(fn, *a)


# ------------------------------------------------------------------- OCR
def _rapidocr():
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        return RapidOCR
    except Exception:
        return None


class OcrChain:
    """provider pick: local (rapidocr-onnxruntime) | llm (ocr role chain:
    multimodal models read the image verbatim) | auto."""

    def __init__(self, cfg: OcrConfig, llm: LLMClient) -> None:
        self.cfg = cfg
        self.llm = llm
        self._local = None

    def resolve(self) -> tuple[str, str]:
        p = (self.cfg.provider or "").strip().lower()
        if p in ("", "off", "none"):
            return "", "disabled"
        if p == "local":
            if _rapidocr() is None:
                return "", "local: 未安装 rapidocr-onnxruntime"
            return "local", ""
        if p == "llm":
            if not self.llm.resolve("ocr"):
                return "", "llm: ocr 角色链不可用"
            return "llm", ""
        if p == "api":  # legacy alias: api == multimodal via registry
            if not self.llm.resolve("ocr"):
                return "", "api: ocr 角色链不可用"
            return "llm", ""
        if _rapidocr() is not None:
            return "local", ""
        if self.llm.resolve("ocr"):
            return "llm", ""
        return "", "auto: 无可用 OCR 后端（安装 rapidocr-onnxruntime 或配置 ocr 角色）"

    async def extract_text(self, image_path: str | Path) -> str:
        provider, note = self.resolve()
        if not provider:
            raise RuntimeError(note or "OCR 不可用")
        if provider == "local":
            return await self._ocr_local(Path(image_path))
        return await self.llm.vision_describe(
            image_path,
            "识别并逐字输出图片中的所有文字（保留换行），不要解释。",
        )

    async def _ocr_local(self, p: Path) -> str:
        RapidOCR = _rapidocr()
        if RapidOCR is None:
            raise RuntimeError("local: 未安装 rapidocr-onnxruntime")
        if self._local is None:
            self._local = RapidOCR()

        def _run():
            result, _elapse = self._local(str(p))
            if not result:
                return ""
            return "\n".join(str(line[1]) for line in result).strip()

        return await _to_thread(_run)


# --------------------------------------------------------- value scoring
_VALUE_PROMPT = """给下面每条聊天消息打"信息价值"分（0-3）：
0=寒暄/表情/贴图/无信息量；1=一般闲聊；2=含事实/约定/观点/提问；3=重要（联系方式、时间地点计划、关键决定）。
只输出 JSON 对象 {{"行号": 分数}}，不要解释。

{lines}"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


class ValueScorer:
    """Batch-scores messages with the value role chain (cheap model first)."""

    def __init__(self, cfg: ValueConfig, llm: LLMClient) -> None:
        self.cfg = cfg
        self.llm = llm

    def available(self) -> bool:
        return bool(self.llm.resolve("value"))

    @staticmethod
    def build_prompt(rows: list[dict]) -> str:
        lines = "\n".join(
            f"{i+1}\t{(r.get('text_content') or '').strip()[:200]}"
            for i, r in enumerate(rows)
        )
        return _VALUE_PROMPT.format(lines=lines)

    @staticmethod
    def parse_scores(rows: list[dict], text: str) -> list[tuple[int, int]]:
        """Model reply -> [(_rid, score)]; unmentioned/unparseable dropped."""
        m = _JSON_RE.search(text or "")
        if not m:
            raise ValueError(f"value scoring: no JSON in reply: {(text or '')[:120]}")
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"value scoring: bad JSON ({e}): {(text or '')[:120]}") from e
        out: list[tuple[int, int]] = []
        for k, v in parsed.items():
            try:
                idx = int(str(k).strip()) - 1
                score = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(rows) and 0 <= score <= 3:
                out.append((rows[idx]["_rid"], score))
        return out

    async def score(self, rows: list[dict]) -> list[tuple[int, int]]:
        """rows: repo.messages_unjudged dicts (with _rid). Returns
        [(_rid, score)] for every parseable line; unmentioned rows are
        NOT returned (caller may re-judge them later)."""
        if not rows:
            return []
        msg = await self.llm.chat(
            [{"role": "user", "content": self.build_prompt(rows)}], role="value"
        )
        return self.parse_scores(rows, msg.get("content") or "")
