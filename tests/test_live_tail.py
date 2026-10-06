"""Live streaming tail: the webui session view must show streamed text and
thinking DURING a round (not just counters), and the tail must roll."""

from __future__ import annotations

from muze.ai.sessions import Session


def test_session_stream_keeps_rolling_tail():
    s = Session(id="a", chat_key="k", prompt="p")
    s.stream("abc")
    s.stream("def")
    assert s.live == "abcdef"
    assert s.live_reason == ""
    s.stream("想了一层", reasoning=True)
    s.stream("又想一层", reasoning=True)
    assert s.live_reason == "想了一层又想一层"
    # tail is capped: only the last LIVE_TAIL chars survive
    big = "x" * (Session.LIVE_TAIL + 10)
    s.stream(big)
    assert len(s.live) == Session.LIVE_TAIL
    # a new round clears the live buffers
    s.new_round()
    assert s.live == "" and s.live_reason == "" and s.round_chars == 0
