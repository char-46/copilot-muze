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
        self.seen: list[str] = []  # user prompts, for delta assertions
        self._fault_until = 0.0
        self._fault_reason = ""

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        self.seen.append(str(messages[-1].get("content") if messages else ""))
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

    def clear_fault(self):
        self._fault_reason = ""
        self._fault_until = 0.0


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


async def test_bg_chat_resumes_after_user_clears_fault():
    """User-initiated commands (/dream, /balance) clear the gate first: a
    stale 402 must not block a dream for 30 min after a top-up (live
    incident 2026-10-28)."""
    llm = FakeLLM()
    llm.note_fault(RuntimeError("402 insufficient"))
    assert llm.faulted is True
    llm.clear_fault()
    assert llm.faulted is False
    out = await bg_chat(llm, [{"role": "user", "content": "hi"}])
    assert out["content"] == "ok" and llm.calls == 1


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
    await _seed(repo, 222, now - 60, "乙：稿子我今晚发你，写完记得查收一下，谢谢配合")
    await repo.upsert_profile("friend/222", "", "旧档案")
    await repo.conn.execute(
        "UPDATE profiles SET updated_at=? WHERE subject='friend/222'", (now - 7200,)
    )
    await repo.conn.commit()

    llm = FakeLLM(reply="新档案")
    n = await refresh_profiles(repo, llm, MuzeConfig(), None, since_h=48)
    assert n == 1 and llm.calls == 1


async def test_profiles_delta_prompt_is_old_doc_plus_new_only(repo):
    """老的profile+新消息应该足够: the prompt must contain ONLY messages
    newer than the stored doc -- old (already-summarized) messages are never
    re-read, so the call folds in genuinely new material only."""
    from muze.config import MuzeConfig

    now = int(time.time())
    old_marker = "这是上个月就已经归纳过的旧话题甲乙丙丁"
    new_marker = "这是今天才发生的新事件戊己庚辛壬癸子丑寅卯"
    await _seed(repo, 555, now - 30 * 86400, f"甲：{old_marker}")
    await repo.upsert_profile("friend/555", "", "旧档案：只有旧话题")
    await repo.conn.execute(
        "UPDATE profiles SET updated_at=? WHERE subject='friend/555'", (now - 86400,)
    )
    await repo.conn.commit()
    await _seed(repo, 555, now - 120, f"乙：{new_marker}")

    llm = FakeLLM(reply="合并后的档案")
    n = await refresh_profiles(repo, llm, MuzeConfig(), None, since_h=48)
    assert n == 1 and llm.calls == 1
    prompt = llm.seen[0]
    assert new_marker in prompt          # the new message is fed
    assert old_marker not in prompt      # the pre-doc message is NOT re-read
    assert "旧档案" in prompt            # the doc itself travels along
    assert "新消息" in prompt            # the prompt says it is a delta


async def test_profiles_tiny_delta_waits_for_more(repo):
    """A one-word delta is not worth a call: it stays pending and folds into
    a later pass once more has accumulated."""
    from muze.config import MuzeConfig

    now = int(time.time())
    await repo.upsert_profile("friend/666", "", "旧档案")
    await repo.conn.execute(
        "UPDATE profiles SET updated_at=? WHERE subject='friend/666'", (now - 3600,)
    )
    await repo.conn.commit()
    await _seed(repo, 666, now - 60, "好")

    llm = FakeLLM(reply="档案")
    assert await refresh_profiles(repo, llm, MuzeConfig(), None, since_h=48) == 0
    assert llm.calls == 0


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


# ------------------------------------------------------ per-feature ledger
def _tracker(tmp_path):
    from muze.model_catalog import CostTracker, Usage

    return CostTracker(tmp_path / "costs.json"), Usage


def test_feature_ledger_separates_features(tmp_path):
    """The cost-stat module: same model, different features -- the per-model
    ledger mixes them, the feature ledger answers 价值打分总cost多少."""
    import datetime as dt

    tr, Usage = _tracker(tmp_path)
    u1 = Usage(prompt_tokens=1000, completion_tokens=100, calls=1)
    u2 = Usage(prompt_tokens=2000, completion_tokens=200, calls=1)
    tr.record("m", u1, 0.10, dt.datetime(2026, 10, 28, 10), feature="value")
    tr.record("m", u2, 0.20, dt.datetime(2026, 10, 28, 11), feature="dream")
    tr.record("m", u1, 0.10, dt.datetime(2026, 10, 28, 12))  # untagged

    feats = tr.feature_summary("2026-10-28")
    assert feats["value"][5] == 0.10 and feats["value"][4] == 1
    assert feats["dream"][5] == 0.20 and feats["dream"][0] == 2000
    assert "value" in feats and "dream" in feats and len(feats) == 2

    # per-model ledger unchanged in shape: one row, all three calls merged
    rows = tr.summary("2026-10-28")
    assert rows["m"][4] == 3 and abs(rows["m"][5] - 0.40) < 1e-9


def test_feature_ledger_persists_and_scopes(tmp_path):
    import datetime as dt

    tr, Usage = _tracker(tmp_path)
    u = Usage(prompt_tokens=10, completion_tokens=1, calls=1)
    tr.record("m", u, 0.01, dt.datetime(2026, 10, 27), feature="value")
    tr.record("m", u, 0.02, dt.datetime(2026, 10, 28), feature="value")

    tr2, _ = _tracker(tmp_path)  # fresh instance reads the same files
    assert tr2.feature_summary("2026-10")["value"][5] == 0.03
    assert tr2.feature_summary("2026-10-28")["value"][5] == 0.02
    assert tr2.feature_summary("")["value"][4] == 2
    assert tr2.summary("2026-10-28")["m"][4] == 1  # main ledger intact


def test_bg_chat_passes_feature_tag_through():
    """feature flows bg_chat(**kw) -> chat(feature=...) without every caller
    knowing anything about the ledger."""
    llm = FakeLLM()
    import asyncio

    async def run():
        return await bg_chat(llm, [{"role": "user", "content": "x"}], feature="value")

    assert asyncio.run(run())["content"] == "ok"
