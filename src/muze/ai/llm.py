"""OpenAI-compatible LLM abstraction (GLM / DeepSeek / MiMo / ... all speak it).

Two endpoints: `chat` for text and tool-calling, `vision` for images
(configurable separately because e.g. DeepSeek has no vision API).

Every request/response (messages, tools, choices, usage) is logged verbatim
as single-line JSON on the `muze.llm` logger so the full LLM interaction
flow is greppable in the ever-growing log file.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from openai import AsyncOpenAI, Timeout

from muze.config import LLMConfig

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


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._chat: AsyncOpenAI | None = None
        self._vision: AsyncOpenAI | None = None

    @property
    def configured(self) -> bool:
        c = self.cfg.chat
        return bool(c.base_url and c.api_key and c.model)

    def apply_config(self, cfg: LLMConfig) -> None:
        """Hot-reload support: swap config, drop cached clients."""
        self.cfg = cfg
        self._chat = None
        self._vision = None

    def _chat_client(self) -> AsyncOpenAI:
        if self._chat is None:
            self._chat = AsyncOpenAI(
                base_url=self.cfg.chat.base_url,
                api_key=self.cfg.chat.api_key,
                # read = max silence between stream chunks: slow output never
                # times out, a stuck connection is cut after `timeout`
                timeout=Timeout(
                    connect=15, read=self.cfg.chat.timeout, write=30, pool=15
                ),
                max_retries=1,
            )
        return self._chat

    def _vision_client(self) -> AsyncOpenAI:
        if self._vision is None:
            v = self.cfg.vision
            self._vision = AsyncOpenAI(
                base_url=v.base_url or self.cfg.chat.base_url,
                api_key=v.api_key or self.cfg.chat.api_key,
                timeout=Timeout(connect=15, read=v.timeout or self.cfg.chat.timeout,
                                write=60, pool=15),
                max_retries=1,
            )
        return self._vision

    def _vision_model(self) -> str:
        return self.cfg.vision.model or self.cfg.chat.model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_chunk=None,
    ) -> dict:
        """One chat.completions call (streamed); returns the assembled choice
        message dict.

        Streaming is not for UX — it is how we tell "slow" from "stuck":
        `read` timeout applies per chunk gap, so a model that keeps producing
        tokens never times out no matter how long the full answer takes,
        while a dead connection is cut after `llm.chat.timeout` of silence.
        `on_chunk(n_chars)` fires for every content delta so callers can show
        live progress.
        """
        client = self._chat_client()
        # never echo provider-side extras (reasoning_content/finish_reason)
        # back to the API on later rounds — servers may reject unknown fields
        _DROP = {"reasoning_content", "finish_reason"}
        clean = [
            {k: v for k, v in m.items() if k not in _DROP}
            if isinstance(m, dict) else m
            for m in messages
        ]
        kwargs: dict = {
            "model": self.cfg.chat.model,
            "messages": clean,
            "stream": True,
        }
        if self.cfg.chat.temperature is not None:
            kwargs["temperature"] = self.cfg.chat.temperature
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
        """Ask the vision model about a local image file."""
        if not self.cfg.vision_enabled:
            return "(视觉模型未配置，无法识别图片内容)"
        p = Path(image_path)
        if not p.exists():
            return f"(图片文件不存在: {p})"
        mime = MIME_BY_EXT.get(p.suffix.lower(), "image/jpeg")
        b64 = base64.b64encode(p.read_bytes()).decode()
        client = self._vision_client()
        model = self._vision_model()
        log.info(
            "LLM req  | model=%s vision file=%s q=%s", model, p, question,
        )
        try:
            resp = await client.chat.completions.create(
                model=model,
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
        except Exception as e:
            log.warning("LLM fail | vision %s: %s", model, e)
            raise
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        usage_d = usage.model_dump() if usage else {}
        log.info("LLM resp | usage=%s vision | %s", _j(usage_d), _j(text))
        return text
