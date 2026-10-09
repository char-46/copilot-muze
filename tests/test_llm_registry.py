"""Multi-model registry: role chains, failover, rate limits, custom wake
prefix, and balance probing."""

from __future__ import annotations

import asyncio

from muze.ai.llm import LLMClient, _Limiter
from muze.config import LLMConfig, LLMEndpoint, ModelCaps, ModelEntry
from muze.core.service import is_llm_invocation, strip_llm_prefix


def _cfg(**kw) -> LLMConfig:
    c = LLMConfig(
        chat=LLMEndpoint(base_url="https://a", api_key="k", model="m-chat"),
        **kw,
    )
    return c


def _entry(model: str, **kw) -> ModelEntry:
    return ModelEntry(base_url="https://x", api_key="k", model=model, **kw)


# ------------------------------------------------------------- resolution
def test_effective_models_synthesizes_legacy_entries():
    c = _cfg()
    m = c.effective_models()
    assert list(m) == ["chat"]  # vision without its own model is not an entry
    assert m["chat"].model == "m-chat"
    c2 = LLMConfig(
        chat=LLMEndpoint(base_url="https://a", api_key="k", model="m"),
        vision=LLMEndpoint(base_url="https://a", api_key="k", model="m-v"),
    )
    assert list(c2.effective_models()) == ["chat", "vision"]
    assert c2.effective_models()["vision"].caps.vision is True


def test_role_chain_defaults_and_overrides():
    c = _cfg()
    assert c.role_chain("chat") == ["chat"]
    assert c.role_chain("vision") == ["vision", "chat"]  # vision dropped at resolve
    assert c.role_chain("value") == ["value", "cheap", "chat"]
    c3 = _cfg(roles={"chat": ["big", "small"]})
    assert c3.role_chain("chat") == ["big", "small"]
    assert c3.role_chain("unknown-role") == ["chat"]


def test_resolve_drops_unknown_and_unconfigured():
    c = _cfg(
        models={
            "big": _entry("m-big"),
            "dead": ModelEntry(base_url="", api_key="", model="x"),
        },
        roles={"chat": ["big", "missing", "dead", "chat"]},
    )
    llm = LLMClient(c)
    chain = llm.resolve("chat")
    assert [n for n, _ in chain] == ["big", "chat"]


# -------------------------------------------------------------- failover
class _FakeClient:
    def __init__(self, chunks=None, error: Exception | None = None):
        from types import SimpleNamespace as NS

        self.calls = 0
        self.error = error
        self._chunks = chunks or []
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self

    async def __aiter__(self):
        for ch in self._chunks:
            yield ch


def _chunk(content=None, finish=None):
    from types import SimpleNamespace as NS

    return NS(
        usage=None,
        choices=[NS(finish_reason=finish, delta=NS(content=content, tool_calls=None))],
    )


async def test_chat_fails_over_to_next_model_in_chain():
    c = _cfg(
        models={"big": _entry("m-big"), "small": _entry("m-small")},
        roles={"chat": ["big", "small"]},
    )
    llm = LLMClient(c)
    broken = _FakeClient(error=RuntimeError("boom"))
    ok = _FakeClient(chunks=[_chunk(content="答案"), _chunk(finish="stop")])
    llm._clients = {"big": broken, "small": ok}
    msg = await llm.chat([{"role": "user", "content": "q"}])
    assert msg["content"] == "答案"
    assert broken.calls == 1 and ok.calls == 1


async def test_chat_raises_when_whole_chain_fails():
    c = _cfg(models={"a": _entry("ma"), "b": _entry("mb")}, roles={"chat": ["a", "b"]})
    llm = LLMClient(c)
    e1, e2 = RuntimeError("e1"), RuntimeError("e2")
    llm._clients = {"a": _FakeClient(error=e1), "b": _FakeClient(error=e2)}
    try:
        await llm.chat([{"role": "user", "content": "q"}])
        raised = False
    except RuntimeError as ex:
        raised = True
        assert ex is e2  # the LAST error surfaces
    assert raised


async def test_tools_skip_toolless_models():
    c = _cfg(
        models={
            "notools": _entry("m1", caps=ModelCaps(tools=False)),
            "with": _entry("m2"),
        },
        roles={"chat": ["notools", "with"]},
    )
    llm = LLMClient(c)
    no, yes = _FakeClient(), _FakeClient(chunks=[_chunk(content="x", finish="stop")])
    llm._clients = {"notools": no, "with": yes}
    await llm.chat([{"role": "user", "content": "q"}], tools=[{"t": 1}])
    assert no.calls == 0 and yes.calls == 1  # routed straight to the capable one


# ------------------------------------------------------------ rate limits
async def test_limiter_rpm_and_concurrency():
    # rpm window full -> the next acquire waits for the oldest slot to age out
    lim = _Limiter(rpm=2, max_concurrency=0)
    await lim.acquire()
    await lim.acquire()
    t = asyncio.create_task(lim.acquire())
    await asyncio.sleep(0.05)
    assert not t.done()
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass

    # concurrency cap: holding the only slot blocks the next acquire
    lim2 = _Limiter(rpm=0, max_concurrency=1)
    await lim2.acquire()
    t2 = asyncio.create_task(lim2.acquire())
    await asyncio.sleep(0.05)
    assert not t2.done()
    lim2.release()
    await asyncio.wait_for(t2, 1)  # released -> goes through


# --------------------------------------------------------------- prefix
def test_custom_wake_prefix():
    assert is_llm_invocation("::hi") is True
    assert is_llm_invocation("：：hi") is True
    assert is_llm_invocation("：:hi") is True  # mixed widths
    assert is_llm_invocation(":hi") is False
    assert is_llm_invocation("hi") is False

    assert is_llm_invocation("!!你好", "!!") is True
    assert is_llm_invocation("！！你好", "!!") is False  # non-colon prefix: exact
    assert is_llm_invocation("::你好", "!!") is False
    assert strip_llm_prefix("!! 你好", "!!") == "你好"
    assert strip_llm_prefix("!!!", "!!") == "!"
    # empty prefix never matches (safety: bare chatter must not wake the LLM)
    assert is_llm_invocation("anything", "") is False


# -------------------------------------------------------------- balance
async def test_get_balance_deepseek(monkeypatch):
    import muze.ai.llm as llm_mod

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "is_available": True,
                "balance_infos": [
                    {"currency": "CNY", "total_balance": "110.00", "unused_balance": "9.00"}
                ],
            }

    class _HC:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert "user/balance" in url
            return _Resp()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _HC)
    c = _cfg(models={"ds": ModelEntry(base_url="https://api.deepseek.com", api_key="k", model="deepseek-chat")})
    llm = LLMClient(c)
    r = await llm.get_balance("ds")
    assert r["ok"] and "110.00" in r["balance"] and "CNY" in r["balance"]


async def test_get_balance_unconfigured_and_unsupported():
    llm = LLMClient(LLMConfig())
    r = await llm.get_balance("nope")
    assert r["ok"] is False and "未配置" in r["error"]
    llm2 = LLMClient(_cfg(models={"x": ModelEntry(base_url="https://nowhere.example", api_key="k", model="m")}))
    r2 = await llm2.get_balance("x")
    assert r2["ok"] is False  # no provider pattern matches -> graceful refusal
