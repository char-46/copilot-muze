"""The account-fault layer: ONE place decides "the account is unusable",
and background passes skip through ONE entry point (user direction
2026-10-28: 不要每个功能单独维护 llm 对话判断 402，用统一 layer).

Cost incident behind it: HTTP 402 (insufficient balance) drained the
account while nightly passes retried the dead endpoint conversation by
conversation -- ~2200 profile calls/day, two thirds of all spend.
"""

import time

import pytest

from muze.ai.llm import (
    ACCOUNT_FAULT_MARKERS,
    LLMAccountFault,
    _is_account_fault,
    bg_chat,
)
from muze.profiles import refresh_profiles


class FakeLLM:
    """Configurable endpoint: configured + optional failure."""

    def __init__(self, exc: Exception | None = None, reply: str = "ok"):
        self.configured = True
        self.exc = exc
        self.reply = reply
        self.calls = 0
        self._fault_until = 0.0
        self._fault_reason = ""

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return {"role": "assistant", "content": self.reply}

    # the gate lives on LLMClient; emulate the attribute bg_chat reads
    @property
    def faulted(self):
        return bool(self._fault_reason) and time.time() < self._fault_until

    def note_fault(self, ex, cooldown_s=1800.0):
        self._fault_reason = str(ex)[:160]
        self._fault_until = time.time() + cooldown_s
        return True


class BoomLLM(FakeLLM):
    """Raises an account-level error, like the real 402."""

    def __init__(self):
        super().__init__(exc=RuntimeError("Error code: 402 - Insufficient balance"))


# --------------------------------------------------------- fault detection
@pytest.mark.parametrize("msg,expected", [
    ("Error code: 402 - Insufficient balance", True),
    ("insufficient account balance", True),
    ("quota exceeded your limit", True),
    ("payment required", True),
    ("500 internal server error", False),
    ("connection reset by peer", False),
    ("timeout reading response", False),
])
def test_only_this_layer_classifies(msg, expected):
    assert _is_account_fault(RuntimeError(msg)) is expected
    assert any(m in msg.lower() for m in ACCOUNT_FAULT_MARKERS) is expected


# ---------------------------------------------------------------- bg_chat
async def test_bg_chat_passes_through_when_healthy():
    llm = FakeLLM()
    out = await bg_chat(llm, [{"role": "user", "content": "hi"}])
    assert out["content"] == "ok" and llm.calls == 1


async def test_bg_chat_calls_provider_even_while_faulted_for_interactive():
    """Interactive paths use llm.chat directly (user may have topped up) --
    bg_chat is the one that refuses."""
    llm = FakeLLM()
    llm.note_fault(RuntimeError("402 insufficient"))
    assert llm.faulted is True
    with pytest.raises(LLMAccountFault):
        await bg_chat(llm, [{"role": "user", "content": "hi"}])
    assert llm.calls == 0  # NOT sent: no spend on a dead account


async def test_bg_chat_records_fault_and_raises_for_callers():
    llm = BoomLLM()
    with pytest.raises(LLMAccountFault):
        await bg_chat(llm, [{"role": "user", "content": "hi"}])
    assert llm.calls == 1  # first call really happened and detected 402
    # a later pass is short-circuited without touching the provider
    with pytest.raises(LLMAccountFault):
        await bg_chat(llm, [{"role": "user", "content": "again"}])
    assert llm.calls == 1


async def test_bg_chat_propagates_non_account_errors():
    llm = FakeLLM(exc=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError):
        await bg_chat(llm, [{"role": "user", "content": "hi"}])
    assert not llm.faulted  # transient, not an account problem


# ------------------------------------------- profiles: incremental + abort
@pytest.fixture
async def repo(tmp_path):
    from muze.store.db import open_db
    from muze.store.repo import Repository

    conn = await open_db(str(tmp_path / "p.db"))
    yield Repository(conn)
    await conn.close()


async def _seed(repo, peer, ts, text):
    from muze.provider.milky.models import IncomingMessage

    m = IncomingMessage.from_raw(
        {
            "message_scene": "friend", "peer_id": peer,
            "message_seq": int(ts) % 100000, "sender_id": 1473195957,
            "time": ts, "segments": [{"type": "text", "data": {"text": text}}],
        }
    )
    await repo.upsert_message(m, text, "event")


async def test_profiles_skip_unchanged_conversations(repo):
    """The actual cost fix: a chat with no new messages is NOT re-summarized."""
    from muze.config import MuzeConfig
    from muze.digest import local_date

    now = int(time.time())
    await _seed(repo, 111, now - 3600, "甲：明天运动会记得带相机，收到请回复")
    await repo.upsert_profile("friend/111", "", "档案：甲")  # written "now"

    llm = FakeLLM(reply="新档案")
    n = await refresh_profiles(repo, llm, MuzeConfig(), None, since_h=48)
    assert n == 0 and llm.calls == 0  # doc is newer than the messages


async def test_profiles_refresh_when_new_messages_arrive(repo):
    from muze.config import MuzeConfig

    now = int(time.time())
    await _seed(repo, 222, now - 60, "乙：稿子我今晚发你，记得查收一下")
    await repo.upsert_profile("friend/222", "", "旧档案")
    await repo.conn.execute(
        "UPDATE profiles SET updated_at=? WHERE subject='friend/222'", (now - 7200,)
    )
    await repo.conn.commit()

    llm = FakeLLM(reply="新档案")
    n = await refresh_profiles(repo, llm, MuzeConfig(), None, since_h=48)
    assert n == 1 and llm.calls == 1


async def test_profiles_abort_on_account_fault(repo):
    """One 402 must not turn into N failing conversations."""
    from muze.config import MuzeConfig

    now = int(time.time())
    for i, peer in enumerate((301, 302, 303, 304)):
        await _seed(repo, peer, now - 60, f"会话{i}：请把材料发我邮箱，谢谢配合")

    llm = BoomLLM()
    n = await refresh_profiles(repo, llm, MuzeConfig(), None, since_h=48)
    assert n == 0 and llm.calls == 1  # aborted after the first 402


async def test_profiles_daily_budget_caps_the_pass(repo):
    from muze.config import MuzeConfig

    cfg = MuzeConfig()
    cfg.profiles.daily_budget = 2
    now = int(time.time())
    for peer in (401, 402, 403, 404):
        await _seed(repo, peer, now - 60, "请把材料发我邮箱，收到请回复一下")

    llm = FakeLLM(reply="档案")
    n = await refresh_profiles(repo, llm, cfg, None, since_h=48)
    assert n == 2  # budget reached, remaining conversations deferred
    assert await repo.get_meta("profile_calls_n") == "2"

    # exhausted budget -> the next pass is a no-op
    llm2 = FakeLLM(reply="档案")
    assert await refresh_profiles(repo, llm2, cfg, None, since_h=48) == 0
    assert llm2.calls == 0
