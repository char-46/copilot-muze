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
import time
from pathlib import Path

import httpx

from muze.ai.llm import LLMClient, bg_chat
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


def _qwen_asr_pkg():
    try:
        from qwen_asr import Qwen3ASRModel  # type: ignore

        return Qwen3ASRModel
    except Exception:
        return None


# cfg.language ("zh", "en", ...) -> Qwen3-ASR language name; None = autodetect
_QWEN_LANG = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ja": "Japanese",
    "ko": "Korean", "ru": "Russian", "fr": "French", "de": "German",
    "es": "Spanish", "pt": "Portuguese", "it": "Italian", "": None,
}


class QwenAsrBackend:
    """Local Qwen3-ASR (+ optional Qwen3-ForcedAligner timestamps).

    Loads lazily on first clip; GPU (bf16) when free VRAM fits, else CPU
    (float32). unload_if_idle() releases the weights between sparse
    enrichment passes, mirroring the embedder governor philosophy."""

    def __init__(self, cfg: AsrConfig) -> None:
        self.cfg = cfg
        self._model = None
        self._last_used = 0.0

    # -- lifecycle -------------------------------------------------
    def _pick_device(self) -> tuple[str, str]:
        """(device_map, dtype-torch-name)"""
        want = (self.cfg.qwen_device or "auto").strip().lower()
        if want in ("cpu", ""):
            return "cpu", "float32"
        try:
            import torch

            if torch.cuda.is_available():
                if want == "cuda":
                    return "cuda:0", "bfloat16"
                free, _total = torch.cuda.mem_get_info()
                if free >= self.cfg.qwen_gpu_weight_gb * (1 << 30):
                    return "cuda:0", "bfloat16"
        except Exception:
            pass
        return "cpu", "float32"

    def _load(self):
        Model = _qwen_asr_pkg()
        if Model is None:
            raise RuntimeError("qwen: 未安装 qwen-asr（uv sync --extra asr-qwen）")
        if self._model is None:
            import torch

            device, dtype_name = self._pick_device()
            kwargs = dict(
                dtype=getattr(torch, dtype_name),
                device_map=device,
                max_inference_batch_size=8,
                max_new_tokens=self.cfg.qwen_max_new_tokens,
            )
            if self.cfg.aligner_model and self.cfg.timestamps:
                kwargs["forced_aligner"] = self.cfg.aligner_model
                kwargs["forced_aligner_kwargs"] = dict(
                    dtype=getattr(torch, dtype_name), device_map=device
                )
            log.info("loading qwen3-asr %s on %s", self.cfg.qwen_model, device)
            self._model = Model.from_pretrained(self.cfg.qwen_model, **kwargs)
        self._last_used = time.time()
        return self._model

    def unload_if_idle(self, now: float | None = None) -> bool:
        limit = self.cfg.qwen_unload_idle_sec
        if not limit or self._model is None:
            return False
        if (now or time.time()) - self._last_used >= limit:
            log.info("unloading idle qwen3-asr")
            self._model = None
            return True
        return False

    # -- inference -------------------------------------------------
    def transcribe_full(self, wav_path: Path) -> tuple[str, list[list]]:
        """-> (text, [[word, start, end], ...]) — timings empty without an
        aligner. Blocking; callers wrap in a thread."""
        model = self._load()
        lang = _QWEN_LANG.get((self.cfg.language or "").strip().lower())
        want_ts = bool(self.cfg.aligner_model) and self.cfg.timestamps
        r = model.transcribe(
            audio=str(wav_path), language=lang, return_time_stamps=want_ts
        )[0]
        text = (getattr(r, "text", "") or "").strip()
        ts = []
        for w in getattr(r, "time_stamps", None) or []:
            ts.append([getattr(w, "text", ""), round(float(w.start_time), 2),
                       round(float(w.end_time), 2)])
        return text, ts


class AsrChain:
    """provider pick: api (registry asr role) | qwen (local Qwen3-ASR) |
    local (faster-whisper) | qq (reserved stub) | auto. resolve() reports
    what is actually usable."""

    def __init__(self, cfg: AsrConfig, llm: LLMClient) -> None:
        self.cfg = cfg
        self.llm = llm
        self._local_model = None  # lazy faster-whisper instance
        self._qwen = QwenAsrBackend(cfg) if _qwen_asr_pkg() else None

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
        if p == "qwen":
            if _qwen_asr_pkg() is None:
                return "", "qwen: 未安装 qwen-asr（uv sync --extra asr-qwen）"
            return "qwen", ""
        if p == "local":
            if _faster_whisper() is None:
                return "", "local: 未安装 faster-whisper"
            return "local", ""
        # auto: api first (no local footprint), then qwen, then whisper
        if _asr_api_entry(self.llm) is not None:
            return "api", ""
        if _qwen_asr_pkg() is not None:
            return "qwen", ""
        if _faster_whisper() is not None:
            return "local", ""
        return "", "auto: 无可用 ASR 后端（配置 api 角色或安装 qwen-asr/faster-whisper）"

    async def transcribe(self, wav_path: str | Path, duration_sec: float = 0.0) -> str:
        text, _ts = await self.transcribe_full(wav_path, duration_sec)
        return text

    async def transcribe_full(
        self, wav_path: str | Path, duration_sec: float = 0.0
    ) -> tuple[str, list[list]]:
        provider, note = self.resolve()
        if not provider:
            raise RuntimeError(note or "ASR 不可用")
        if provider == "api":
            return await self._transcribe_api(Path(wav_path), duration_sec), []
        if provider == "qwen":
            if self._qwen is None:  # package appeared after __init__
                self._qwen = QwenAsrBackend(self.cfg)
            return await _to_thread(self._qwen.transcribe_full, Path(wav_path))
        return await self._transcribe_local(Path(wav_path)), []

    def maybe_unload(self) -> None:
        if self._qwen is not None:
            self._qwen.unload_if_idle()

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


VOICE_PLACEHOLDER_RE = re.compile(r"\[语音:[\d.]+s?\]")


def _ffmpeg_exe() -> str | None:
    """Guaranteed ffmpeg: the imageio-ffmpeg wheel bundles one; a system
    ffmpeg is a fine fallback. None -> no audio conversion possible."""
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    import shutil

    return shutil.which("ffmpeg")


def ensure_wav(p: Path) -> Path:
    """QQ voice blobs are SILK (transcoded to .wav at ingest) or AMR
    (stored raw — libsndfile/soundfile cannot read AMR, so every
    transcription of an AMR clip died with "Format not recognised").
    Convert non-wav audio to 16kHz mono wav next to the source; return the
    original path when it is already wav or when no ffmpeg exists (the
    backend then reports its own error)."""
    if p.suffix.lower() in (".wav", ".wave"):
        return p
    exe = _ffmpeg_exe()
    if exe is None:
        return p
    out = p.with_suffix(".conv.wav")
    if out.exists():  # blob content is immutable; the sidecar stays valid
        return out
    import subprocess

    r = subprocess.run(
        [exe, "-y", "-loglevel", "error", "-i", str(p),
         "-ar", "16000", "-ac", "1", str(out)],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0 or not out.exists():
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(
            f"audio convert failed for {p.name}: "
            f"{r.stderr.decode(errors='replace')[-200:]}"
        )
    return out


async def transcribe_media_row(
    asr: "AsrChain", repo, m: dict, media_store=None
) -> tuple[str, list[list]]:
    """Transcribe ONE record media row and write everything back: media
    summary + asr_ts, plus the [语音转写] rewrite of the message text.
    Shared by the background perception pass and the transcribe_voice
    agent tool (old clips re-understood on demand). Returns (text, ts);
    ("", []) means "handled, nothing to show" (too short / file gone)."""
    dur = float(m.get("duration") or 0)
    min_dur = getattr(asr.cfg, "min_duration_sec", 1.0)
    p = None
    if media_store is not None:
        p = media_store.abs_path(m)
    p = Path(p or m.get("file_path") or "")
    if (dur and dur < min_dur) or not p.exists():
        await repo.update_media(m["id"], status="done", summary="")
        return "", []
    p = await _to_thread(ensure_wav, p)  # AMR/other formats -> 16k wav
    text, ts = await asr.transcribe_full(p, duration_sec=dur)
    text = text.strip()
    if not text:
        await repo.update_media(m["id"], status="done", summary="")
        return "", []
    await repo.update_media(
        m["id"], status="done", summary=("[转写] " + text)[:500],
        asr_ts=json.dumps(ts, ensure_ascii=False) if ts else None,
    )
    old = await repo.message_text(m["scene"], m["peer_id"], m["message_seq"])
    if old:
        new = VOICE_PLACEHOLDER_RE.sub(lambda _: f"[语音转写] {text}", old, count=1)
        if new == old:  # no placeholder (mixed-content message)
            new = old + f"\n[语音转写] {text}"
        await repo.update_message_text(
            m["scene"], m["peer_id"], m["message_seq"], new
        )
    return text, ts


# ------------------------------------------------------------------- OCR
def _rapidocr():
    """rapidocr-onnxruntime ships as `rapidocr_onnxruntime` on the 1.3.x
    line and as the unified `rapidocr` module since 1.4 — accept both."""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        return RapidOCR
    except Exception:
        pass
    try:
        from rapidocr import RapidOCR  # type: ignore

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
        # bg_chat short-circuits while the account is faulted (402/quota):
        # the caller catches LLMAccountFault and skips the pass
        msg = await bg_chat(
            self.llm, [{"role": "user", "content": self.build_prompt(rows)}], role="value"
        )
        return self.parse_scores(rows, msg.get("content") or "")
