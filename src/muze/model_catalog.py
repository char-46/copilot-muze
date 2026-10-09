"""Python model catalog: capability declarations + pricing functions.

The MODEL LIST is Python, not YAML, because pricing is logic, not data:
peak/off-peak windows (DeepSeek's 00:30-08:30 half price), per-modality
rates (MiMo: vision on some ids, ASR on others), subscription tiers.

User file: `<data_dir>/models.py` (gitignored like config.yaml; copy
models.example.py). It defines MODELS = {name: ModelSpec(...)}; endpoint
secrets (api_key) are merged from config.yaml llm.models.<name>.api_key
so keys never live in the catalog.

    from muze.model_catalog import ModelSpec, Pricing, PeakOffPeak, ModelCaps

    MODELS = {
        "dpsk-chat": ModelSpec(
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            caps=ModelCaps(tools=True),
            pricing=Pricing(
                input_per_mtok=2.0, output_per_mtok=8.0,
                peak=PeakOffPeak(off_start="00:30", off_end="08:30",
                                 off_factor=0.5),
            ),
        ),
    }

Everything is optional: entries without pricing are tracked in tokens
only; a missing file means "no catalog" (pure-yaml registry keeps working).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from muze.config import ModelCaps

log = logging.getLogger("muze.catalog")


@dataclass(frozen=True)
class Usage:
    """One billable interaction, provider-agnostic."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    images: int = 0        # image parts sent (vision/OCR)
    audio_sec: float = 0.0  # transcribed audio length (ASR)
    calls: int = 1

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.images + other.images,
            self.audio_sec + other.audio_sec,
            self.calls + other.calls,
        )


def _hm(s: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    h, m = str(s).strip().split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True)
class PeakOffPeak:
    """Time-of-day price multiplier (峰谷计价).

    Inside [off_start, off_end) every TOKEN price is multiplied by
    off_factor (e.g. DeepSeek 谷时 00:30-08:30 ×0.5); outside it, by
    peak_factor (1.0 unless a subscription marks peak hours up). Windows
    may wrap midnight (22:00-06:30).
    """

    off_start: str = "00:30"
    off_end: str = "08:30"
    off_factor: float = 0.5
    peak_factor: float = 1.0

    def factor_at(self, dt) -> float:
        m = dt.hour * 60 + dt.minute
        a, b = _hm(self.off_start), _hm(self.off_end)
        inside = (a <= m < b) if a <= b else (m >= a or m < b)
        return self.off_factor if inside else self.peak_factor


@dataclass(frozen=True)
class Pricing:
    """Per-model price sheet (CNY). All fields optional.

    cost(usage, when) = tokens*rate*peak_factor*[batch_factor] +
    images*flat + audio*flat (modality flats are NOT time-discounted;
    only token prices are — batch jobs discount tokens only as well).
    """

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    image_per_call: float = 0.0
    audio_per_minute: float = 0.0
    currency: str = "CNY"
    peak: PeakOffPeak | None = None
    batch_factor: float = 0.5  # async /v1/batch jobs (OpenAI & DeepSeek: 50%)

    def cost(self, usage: Usage, when=None, batch: bool = False) -> float:
        import datetime as _dt

        when = when or _dt.datetime.now()
        f = self.peak.factor_at(when) if self.peak else 1.0
        if batch:
            f *= self.batch_factor
        tok = (
            usage.prompt_tokens / 1e6 * self.input_per_mtok
            + usage.completion_tokens / 1e6 * self.output_per_mtok
        ) * f
        img = usage.images * self.image_per_call
        aud = usage.audio_sec / 60.0 * self.audio_per_minute
        return round(tok + img + aud, 6)


PriceFn = Callable[[Usage, Any], float]


@dataclass(frozen=True)
class ModelSpec:
    """One catalog entry: endpoint + capabilities + rate limits + pricing.

    api_key intentionally absent — secrets merge in from config.yaml
    (llm.models.<name>.api_key) or the MUZE_KEY_<NAME> env var.
    """

    base_url: str = ""
    model: str = ""
    caps: ModelCaps = field(default_factory=ModelCaps)
    rpm: int = 0
    max_concurrency: int = 0
    timeout: float = 120.0
    temperature: float | None = None
    pricing: Pricing | PriceFn | None = None
    note: str = ""

    def price_of(self, usage: Usage, when=None, batch: bool = False) -> float | None:
        if self.pricing is None:
            return None
        if callable(self.pricing) and not isinstance(self.pricing, Pricing):
            return float(self.pricing(usage, when))
        return self.pricing.cost(usage, when, batch=batch)


def load_user_catalog(path: str | Path) -> dict[str, ModelSpec]:
    """Exec a user models.py; returns {} on missing file or any error
    (the pure-yaml registry keeps working — a broken catalog must never
    take the assistant down)."""
    p = Path(path)
    if not p.is_file():
        return {}
    ns: dict[str, Any] = {"__file__": str(p), "__name__": "muze_user_catalog"}
    try:
        code = p.read_text(encoding="utf-8")
        exec(compile(code, str(p), "exec"), ns)  # noqa: S102 - user's own file
        raw = ns.get("MODELS")
        if not isinstance(raw, dict):
            raise ValueError("models.py must define MODELS = {name: ModelSpec(...)}")
        out: dict[str, ModelSpec] = {}
        for name, spec in raw.items():
            if isinstance(spec, ModelSpec):
                out[str(name)] = spec
            else:
                raise ValueError(f"MODELS[{name!r}] is {type(spec).__name__}, not ModelSpec")
        log.info("model catalog loaded: %s entries from %s", len(out), p)
        return out
    except Exception:
        log.exception("model catalog %s ignored (falling back to llm.models)", p)
        return {}


class CostTracker:
    """Usage + cost ledger, persisted as JSON (day -> model -> totals).

    Cost is computed AT RECORD TIME (the peak/off-peak factor of the
    moment is baked in), so the ledger stays a plain sum of numbers.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, list]] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            import json

            self._data = json.loads(self.path.read_text("utf-8"))
        except Exception:
            log.warning("cost ledger unreadable; starting fresh", exc_info=True)
            self._data = {}

    def _flush(self) -> None:
        if self.path is None:
            return
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    def record(
        self, model: str, usage: Usage, cost: float | None, when=None
    ) -> float | None:
        import datetime as _dt

        when = when or _dt.datetime.now()
        day = when.strftime("%Y-%m-%d")
        with self._lock:
            row = self._data.setdefault(day, {}).setdefault(
                model, [0, 0, 0, 0.0, 0, 0.0]  # p_tok, c_tok, img, audio_s, calls, cost
            )
            row[0] += usage.prompt_tokens
            row[1] += usage.completion_tokens
            row[2] += usage.images
            row[3] += usage.audio_sec
            row[4] += usage.calls
            if cost is not None:
                row[5] = round(row[5] + cost, 6)
            self._flush()
            return row[5] or None

    def summary(self, day_prefix: str = "") -> dict[str, list]:
        """Rows for days matching the prefix ('2026-10' = month, '' = all),
        merged per model."""
        out: dict[str, list] = {}
        with self._lock:
            for day, models in sorted(self._data.items()):
                if day_prefix and not day.startswith(day_prefix):
                    continue
                for m, row in models.items():
                    acc = out.setdefault(m, [0, 0, 0, 0.0, 0, 0.0])
                    for i, v in enumerate(row):
                        acc[i] += v
        return out
