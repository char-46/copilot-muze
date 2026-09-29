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

from openai import AsyncOpenAI

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
                base_url=self.cfg.chat.base_url, api_key=self.cfg.chat.api_key
            )
        return self._chat

    def _vision_client(self) -> AsyncOpenAI:
        if self._vision is None:
            v = self.cfg.vision
            self._vision = AsyncOpenAI(
                base_url=v.base_url or self.cfg.chat.base_url,
                api_key=v.api_key or self.cfg.chat.api_key,
            )
        return self._vision

    def _vision_model(self) -> str:
        return self.cfg.vision.model or self.cfg.chat.model

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """One chat.completions call; returns the raw choice message dict.

        The full request (model, tools, messages incl. system prompt), the
        raw response message and token usage are logged on muze.llm.
        """
        client = self._chat_client()
        kwargs: dict = {
            "model": self.cfg.chat.model,
            "messages": messages,
        }
        if self.cfg.chat.temperature is not None:
            kwargs["temperature"] = self.cfg.chat.temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        log.info(
            "LLM req  | model=%s tools=%s msgs=%s | %s",
            kwargs["model"], len(tools or []), len(messages), _j(messages),
        )
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:
            log.warning("LLM fail | %s: %s", kwargs["model"], e)
            raise
        msg = resp.choices[0].message.model_dump(exclude_none=True)
        usage = getattr(resp, "usage", None)
        usage_d = usage.model_dump() if usage else {}
        log.info(
            "LLM resp | usage=%s | %s",
            _j(usage_d), _j(msg),
        )
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
