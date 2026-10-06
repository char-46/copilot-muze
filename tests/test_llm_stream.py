"""Streaming LLM client (delta accumulation) + session store."""

from types import SimpleNamespace as NS

from muze.ai.llm import LLMClient
from muze.ai.sessions import SessionStore
from muze.config import LLMConfig


def _chunk(content=None, tool_calls=None, finish=None):
    return NS(usage=None, choices=[NS(finish_reason=finish, delta=NS(content=content, tool_calls=tool_calls))])


def _tcd(index, cid=None, name=None, args=None):
    return NS(index=index, id=cid, function=NS(name=name, arguments=args))


class _Stream:
    def __init__(self, chunks):
        self._it = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeClient:
    def __init__(self, chunks):
        self.chat = NS(completions=NS(create=self._create))
        self._chunks = chunks
        self.kwargs: dict | None = None

    async def _create(self, **kwargs):
        self.kwargs = kwargs
        return _Stream(self._chunks)


async def test_chat_streams_and_accumulates_content_and_tool_calls():
    llm = LLMClient(LLMConfig())
    llm._chat = _FakeClient(
        [
            _chunk(content="你"),
            _chunk(content="好"),
            _chunk(
                tool_calls=[_tcd(0, cid="call1", name="search_", args='{"que'),
                _tcd(0, name=None, args='ry": "x"}')],
                finish=None,
            ),
            _chunk(finish="tool_calls"),
        ]
    )
    seen: list[int] = []
    msg = await llm.chat(
        [{"role": "user", "content": "hi"}], tools=[{"x": 1}],
        on_chunk=lambda n, piece="", reasoning=False: seen.append(n),
    )
    assert msg["content"] == "你好"
    assert seen == [1, 1]  # every delta fired on_chunk
    tc = msg["tool_calls"][0]
    assert tc["id"] == "call1"
    assert tc["function"]["name"] == "search_"
    assert tc["function"]["arguments"] == '{"query": "x"}'
    assert msg["finish_reason"] == "tool_calls"
    assert llm._chat.kwargs["stream"] is True


async def test_chat_content_only_round():
    llm = LLMClient(LLMConfig())
    llm._chat = _FakeClient([_chunk(content="答案"), _chunk(finish="stop")])
    msg = await llm.chat([{"role": "user", "content": "q"}])
    assert msg == {"role": "assistant", "finish_reason": "stop", "content": "答案"}


def test_session_store_lifecycle_and_render():
    store = SessionStore(cap=3)
    s = store.create("qq:friend:1", "帮我查上周的记录")
    assert s.status == "running"
    assert store.get(s.id) is s
    assert store.get("#" + s.id) is s  # '#' tolerated
    assert store.get("nope") is None

    s.new_round()
    s.bump(12)
    s.event("tool", "search_archive({\"query\":\"x\"})")
    s.event("tool_result", "search_archive → {\"count\": 2}")
    s.finish(ok=True, preview="共 2 条")

    assert s.status == "done" and s.total_chars == 12
    text = s.render()
    assert s.id in text and "running" not in text.splitlines()[0].split("]")[1]
    assert "search_archive" in text and "count" in text

    # cap: oldest finished sessions are pruned first
    for i in range(5):
        store.create("c", f"p{i}").finish(ok=True)
    assert len(store.recent(10)) <= 6  # cap=3 + the running one kept
