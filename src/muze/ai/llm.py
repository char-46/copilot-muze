"""OpenAI-compatible LLM abstraction (GLM / DeepSeek / MiMo / ... all speak it).

Two endpoints: `chat` for text and tool-calling, `vision` for images
(configurable separately because e.g. DeepSeek has no vision API).

Every request/response (messages, tools, choices, usage) is logged verbatim
as single-line JSON on the `muze.llm` logger so the full LLM interaction
flow is greppable in the ever-growing log file.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from pathlib import Path

import httpx
from openai import AsyncOpenAI, Timeout

from muze.config import LLMConfig, ModelEntry

log = logging.getLogger("muze.llm")

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _j(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return repr(obj)

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class _Limiter:
    """Per-model process-wide throttle: in-flight cap + requests/minute
    sliding window. rpm-exhausted callers wait for the oldest slot to age
    out (bounded by one window), so bursts queue instead of erroring."""

    def __init__(self, rpm: int = 0, max_concurrency: int = 0) -> None:
        self.rpm = rpm
        self._win: deque[float] = deque()
        self._sem = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None

    async def acquire(self) -> None:
        if self._sem is not None:
            await self._sem.acquire()
        if self.rpm <= 0:
            return
        try:
            now = time.monotonic()
            while self._win and now - self._win[0] >= 60.0:
                self._win.popleft()
            if len(self._win) >= self.rpm:
                wait = 60.0 - (now - self._win[0])
                log.info("LLM ratelimit | rpm=%s window full, waiting %.1fs", self.rpm, wait)
                await asyncio.sleep(max(0.0, wait))
                now = time.monotonic()
                while self._win and now - self._win[0] >= 60.0:
                    self._win.popleft()
            self._win.append(time.monotonic())
        except BaseException:
            # the wait was cancelled/failed: give the concurrency slot back
            self.release()
            raise

    def release(self) -> None:
        if self._sem is not None:
            self._sem.release()


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._chat: AsyncOpenAI | None = None
        self._vision: AsyncOpenAI | None = None
        self._clients: dict[str, AsyncOpenAI] = {}
        self._limiters: dict[str, _Limiter] = {}

    @property
    def configured(self) -> bool:
        return bool(self.cfg.effective_models())

    def apply_config(self, cfg: LLMConfig) -> None:
        """Hot-reload support: swap config, drop cached clients."""
        self.cfg = cfg
        self._chat = None
        self._vision = None
        self._clients = {}
        self._limiters = {}

    # ------------------------------------------------------------ registry
    def resolve(self, role: str) -> list[tuple[str, ModelEntry]]:
        """Ordered (name, entry) fallback chain for a role; unknown names
        and unconfigured entries are dropped."""
        entries = self.cfg.effective_models()
        out: list[tuple[str, ModelEntry]] = []
        seen: set[str] = set()
        for name in self.cfg.role_chain(role):
            e = entries.get(name)
            if e and (e.base_url and e.api_key and e.model) and name not in seen:
                seen.add(name)
                out.append((name, e))
        return out

    def _entry_client(self, e: ModelEntry) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url=e.base_url,
            api_key=e.api_key,
            timeout=Timeout(connect=15, read=e.timeout, write=30, pool=15),
            max_retries=1,
        )

    def _limiter(self, name: str, e: ModelEntry) -> _Limiter:
        if name not in self._limiters:
            self._limiters[name] = _Limiter(rpm=e.rpm, max_concurrency=e.max_concurrency)
        return self._limiters[name]

    def _client_for(self, name: str, e: ModelEntry) -> AsyncOpenAI:
        # legacy injection points first (tests, hot compat)
        if name == "chat" and self._chat is not None:
            return self._chat
        if name == "vision" and self._vision is not None:
            return self._vision
        c = self._clients.get(name)
        if c is None:
            c = self._entry_client(e)
            self._clients[name] = c
        return c

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_chunk=None,
        role: str = "chat",
    ) -> dict:
        """One chat.completions call (streamed); returns the assembled choice
        message dict.

        Streaming is not for UX — it is how we tell "slow" from "stuck":
        `read` timeout applies per chunk gap, so a model that keeps producing
        tokens never times out no matter how long the full answer takes,
        while a dead connection is cut after `llm.chat.timeout` of silence.
        `on_chunk(n_chars)` fires for every content delta so callers can show
        live progress.

        `role` picks the model chain (llm.roles); on failure the next entry
        in the chain answers instead (failover), and per-entry rpm/concurrency
        limits are honored along the way.
        """
        chain = self.resolve(role)
        if tools and chain:
            capable = [(n, e) for n, e in chain if e.caps.tools]
            if capable:
                chain = capable
        if not chain and (self._chat is not None or "chat" in self._clients):
            # legacy/test path: a raw client was injected with no registry
            chain = [("chat", ModelEntry())]
        last_err: Exception | None = None
        for i, (name, e) in enumerate(chain):
            lim = self._limiter(name, e)
            try:
                await lim.acquire()
                try:
                    return await self._round(
                        self._client_for(name, e), e, name, messages, tools, on_chunk
                    )
                finally:
                    lim.release()
            except Exception as ex:
                last_err = ex
                if i + 1 < len(chain):
                    log.warning(
                        "LLM failover | %s/%s failed (%s); trying next in role=%s chain",
                        name, e.model, ex, role,
                    )
        raise last_err if last_err else RuntimeError("no model configured")

    async def _round(
        self,
        client: AsyncOpenAI,
        e: ModelEntry,
        name: str,
        messages: list[dict],
        tools: list[dict] | None,
        on_chunk,
    ) -> dict:
        # never echo provider-side extras (reasoning_content/finish_reason)
        # back to the API on later rounds — servers may reject unknown fields
        _DROP = {"reasoning_content", "finish_reason"}
        clean = [
            {k: v for k, v in m.items() if k not in _DROP}
            if isinstance(m, dict) else m
            for m in messages
        ]
        kwargs: dict = {
            "model": e.model,
            "messages": clean,
            "stream": True,
        }
        if e.temperature is not None:
            kwargs["temperature"] = e.temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        # INFO gets a size sketch only — full payloads at DEBUG (a 16-step
        # subagent re-sends the whole history every step; full dumps made
        # the log balloon by hundreds of MB)
        sketch = ",".join(
            f"{m.get('role')}:{len(str(m.get('content') or ''))}"
            + (f"+{len(m.get('tool_calls') or [])}call" if m.get("tool_calls") else "")
            for m in messages
        )
        log.info(
            "LLM req  | model=%s tools=%s msgs=%s | %s",
            kwargs["model"], len(tools or []), len(messages), sketch,
        )
        log.debug("LLM req full | %s", _j(messages))
        try:
            stream = await client.chat.completions.create(**kwargs)
        except Exception as e:
            log.warning("LLM fail | %s: %s", kwargs["model"], e)
            raise

        content: list[str] = []
        reasoning: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish = ""
        usage_d: dict = {}
        try:
            async for chunk in stream:
                u = getattr(chunk, "usage", None)
                if u:
                    usage_d = u.model_dump()
                for choice in chunk.choices or []:
                    if choice.finish_reason:
                        finish = choice.finish_reason
                    delta = choice.delta
                    if delta is None:
                        continue
                    piece = delta.content or ""
                    if piece:
                        content.append(piece)
                        if on_chunk is not None:
                            try:
                                on_chunk(len(piece), piece, False)
                            except Exception:
                                pass
                    # thinking models stream their reasoning separately; it
                    # never goes back to the API (stripped above) — it is
                    # kept on the message for the webui "思考过程" view
                    rpiece = getattr(delta, "reasoning_content", None) or ""
                    if rpiece:
                        reasoning.append(rpiece)
                        if on_chunk is not None:
                            try:
                                on_chunk(len(rpiece), rpiece, True)
                            except Exception:
                                pass
                    for tc in delta.tool_calls or []:
                        i = tc.index if tc.index is not None else 0
                        acc = tool_calls.setdefault(
                            i, {"id": "", "type": "function",
                                "function": {"name": "", "arguments": ""}}
                        )
                        if tc.id:
                            acc["id"] = tc.id
                        fn = tc.function
                        if fn is not None:
                            if fn.name:
                                acc["function"]["name"] += fn.name
                            if fn.arguments:
                                acc["function"]["arguments"] += fn.arguments
        except Exception as e:
            log.warning("LLM stream fail | %s: %s", kwargs["model"], e)
            raise

        msg: dict = {"role": "assistant", "finish_reason": finish or None}
        text = "".join(content)
        if text:
            msg["content"] = text
        rtext = "".join(reasoning)
        if rtext:
            msg["reasoning_content"] = rtext
        if tool_calls:
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        log.info(
            "LLM resp | usage=%s finish=%s | %.500s",
            _j(usage_d), finish, _j(msg),
        )
        log.debug("LLM resp full | %s", _j(msg))
        return msg

    async def vision_describe(self, image_path: str | Path, question: str) -> str:
        """Ask a vision-capable model about a local image file.

        Prefers entries with caps.vision from the vision role chain (any
        multimodal endpoint); falls back to the whole chain — many modern
        chat models accept image parts on the same endpoint anyway.
        """
        if not self.cfg.vision_enabled:
            return "(视觉模型未配置，无法识别图片内容)"
        p = Path(image_path)
        if not p.exists():
            return f"(图片文件不存在: {p})"
        mime = MIME_BY_EXT.get(p.suffix.lower(), "image/jpeg")
        b64 = base64.b64encode(p.read_bytes()).decode()
        chain = self.resolve("vision")
        vision_only = [(n, e) for n, e in chain if e.caps.vision]
        if vision_only:
            chain = vision_only
        if not chain:
            return "(视觉模型未配置，无法识别图片内容)"
        last_err: Exception | None = None
        for i, (name, e) in enumerate(chain):
            client = self._client_for(name, e)
            log.info(
                "LLM req  | model=%s vision file=%s q=%s", e.model, p, question,
            )
            lim = self._limiter(name, e)
            try:
                await lim.acquire()
                try:
                    resp = await client.chat.completions.create(
                        model=e.model,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": question},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                                    },
                                ],
                            }
                        ],
                    )
                finally:
                    lim.release()
            except Exception as ex:
                last_err = ex
                log.warning("LLM fail | vision %s/%s: %s", name, e.model, ex)
                if i + 1 < len(chain):
                    continue
                raise
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            usage_d = usage.model_dump() if usage else {}
            log.info("LLM resp | usage=%s vision | %s", _j(usage_d), _j(text))
            return text
        raise last_err if last_err else RuntimeError("no vision model")

    # ------------------------------------------------------------ balance
    async def get_balance(self, name: str) -> dict:
        """Best-effort account balance for a registered model entry.

        Probes provider-specific patterns against the endpoint (DeepSeek,
        SiliconFlow, Moonshot, then the one-api/new-api dashboard API that
        most OpenAI-compatible aggregators expose).
        """
        e = self.cfg.effective_models().get(name)
        if not e or not e.base_url:
            return {"model": name, "ok": False, "error": "未配置"}
        base = e.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {e.api_key}"}
        host = base.split("//", 1)[-1].lower()

        async def _get(url: str) -> dict | None:
            try:
                async with httpx.AsyncClient(timeout=15) as hc:
                    r = await hc.get(url, headers=headers)
                if r.status_code != 200:
                    return None
                return r.json()
            except Exception:
                return None

        if "deepseek" in host:
            d = await _get(f"{base}/user/balance")
            if d and d.get("balance_infos"):
                parts = [
                    f"{b.get('total_balance', '?')} {b.get('currency', '')}".strip()
                    for b in d["balance_infos"]
                ]
                return {"model": name, "ok": True, "balance": " / ".join(parts)}
        if "siliconflow" in host:
            d = await _get(f"{base}/v1/user/info")
            if d:
                u = d.get("data") or {}
                bal = u.get("balance") or u.get("total_balance") or ""
                if str(bal):
                    cur = u.get("currency", "CNY")
                    return {"model": name, "ok": True, "balance": f"{bal} {cur}".strip()}
        if "moonshot" in host:
            d = await _get(f"{base}/v1/users/me/balance")
            if d and isinstance(d.get("data"), dict):
                bal = d["data"].get("available_balance")
                if bal is not None:
                    return {"model": name, "ok": True, "balance": f"{bal} {d['data'].get('currency', '')}".strip()}

        # one-api / new-api style dashboard: quota limit minus usage
        sub = await _get(f"{base}/dashboard/billing/subscription")
        if sub and sub.get("hard_limit_usd") is not None:
            import datetime as _dt

            now = _dt.datetime.now(_dt.timezone.utc)
            start = now.replace(day=1).strftime("%Y-%m-%d")
            end = (now + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
            usage = await _get(
                f"{base}/dashboard/billing/usage?start_date={start}&end_date={end}"
            )
            if usage and usage.get("total_usage") is not None:
                remain = float(sub["hard_limit_usd"]) - float(usage["total_usage"]) / 100.0
                return {
                    "model": name,
                    "ok": True,
                    "balance": f"${remain:.2f} (额度 ${sub['hard_limit_usd']}, 已用 ${float(usage['total_usage']) / 100:.2f})",
                }
        return {"model": name, "ok": False, "error": "该端点不支持余额查询"}
