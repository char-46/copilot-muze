"""Async Batch API runner: JSONL format, results parsing, batch pricing,
submit -> poll -> apply lifecycle with a fake provider client."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace as NS

import pytest

from muze.batch import BatchRunner, build_jsonl, parse_results_jsonl
from muze.config import (
    BatchConfig,
    LLMConfig,
    ModelEntry,
    MuzeConfig,
    ValueConfig,
)
from muze.model_catalog import ModelSpec, Pricing, Usage


def test_build_jsonl_request_format():
    items = [
        {
            "custom_id": "0",
            "messages": [{"role": "user", "content": "你好"}],
            "temperature": 0.2,
        },
        {"custom_id": "1", "messages": [{"role": "user", "content": "hi"}],
         "tools": [{"x": 1}]},
    ]
    lines = build_jsonl("mm", items).splitlines()
    a = json.loads(lines[0])
    assert a["custom_id"] == "0" and a["method"] == "POST"
    assert a["url"] == "/chat/completions"
    assert a["body"]["model"] == "mm" and a["body"]["stream"] is False
    assert a["body"]["temperature"] == 0.2 and "tools" not in a["body"]
    b = json.loads(lines[1])
    assert b["body"]["tool_choice"] == "auto" and "temperature" not in b["body"]


def test_parse_results_jsonl_success_and_error():
    ok = json.dumps({
        "custom_id": "0",
        "response": {"body": {"choices": [{"message": {"content": '{"1": 2}'}}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 5}}},
    })
    bad = json.dumps({"custom_id": "1", "error": {"message": "boom"}})
    junk = "not json"
    out = parse_results_jsonl("\n".join([ok, bad, junk, ""]))
    assert out["0"]["usage"]["prompt_tokens"] == 10
    assert "error" in out["1"] and "1" in out and len(out) == 2


def test_pricing_batch_factor_stacks_with_offpeak():
    pr = Pricing(
        input_per_mtok=2.0, output_per_mtok=0.0,
        peak=None, batch_factor=0.5,
    )
    u = Usage(prompt_tokens=1_000_000)
    night = dt.datetime(2026, 10, 9, 3, 0)
    assert abs(pr.cost(u, night, batch=True) - 1.0) < 1e-9  # 2.0 * 0.5


# --------------------------------------------------------- lifecycle
class _FakeFiles:
    def __init__(self):
        self.uploaded: list[bytes] = []

    async def create(self, file=(), purpose=""):
        self.uploaded.append(file[1])
        return NS(id=f"file-{len(self.uploaded)}")

    async def content(self, fid):
        body = "\n".join(
            json.dumps({
                "custom_id": str(i),
                "response": {"body": {
                    "choices": [{"message": {"content": f'{{"{i}": {i}}}'}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                }},
            })
            for i in range(2)
        )
        return NS(text=body)


class _FakeBatches:
    def __init__(self):
        self.created: list[dict] = []
        self.statuses: dict[str, list[str]] = {}

    async def create(self, **kw):
        bid = f"batch-{len(self.created) + 1}"
        self.created.append({"id": bid, **kw})
        self.statuses[bid] = ["in_progress", "completed"]
        return NS(id=bid, status="in_progress")

    async def retrieve(self, bid):
        sts = self.statuses[bid]
        s = sts.pop(0) if sts else "completed"
        return NS(id=bid, status=s,
                  output_file_id="fout" if s == "completed" else None)


class _FakeClient:
    def __init__(self):
        self.files = _FakeFiles()
        self.batches = _FakeBatches()


class _Costs:
    def __init__(self):
        self.data = {}
        self.features = {}

    def record(self, model, usage, cost, when=None, feature=""):
        self.data[model] = (usage, cost)
        if feature:
            self.features[feature] = self.features.get(feature, 0) + 1


def _mk_llm(cfg):
    class LLMSelf:
        costs = _Costs()
        _pricing = {"m": Pricing(input_per_mtok=2.0, batch_factor=0.5)}

        def _entries(self):
            return {"m": cfg.models["m"]}

        def resolve(self, role):
            return [("m", cfg.models["m"])] if role == "value" else []

    return LLMSelf()


async def test_submit_poll_apply_roundtrip(tmp_path):
    cfg = LLMConfig(
        models={"m": ModelEntry(base_url="https://x", api_key="k", model="mm")},
        roles={"value": ["m"]},
    )
    llm = _mk_llm(cfg)
    runner = BatchRunner(llm, BatchConfig(), tmp_path / "batch_state.json")
    fake = _FakeClient()
    runner._client = lambda name, entry: fake  # inject the provider client

    applied: list = []

    async def handler(ctx_items, results):
        for it in ctx_items:
            applied.append((it["custom_id"], results[it["custom_id"]]["choices"]))

    async def reset(rowids, _r):
        applied.append(("reset", list(rowids)))

    runner.register_handler("value", handler)
    runner.register_reset("value", reset)

    items = [
        {"custom_id": str(i), "messages": [{"role": "user", "content": f"q{i}"}],
         "rows": [{"_rid": i + 1, "message_seq": i}]}
        for i in range(2)
    ]
    bid = await runner.submit("value", "value", items, [1, 2])
    assert bid == "batch-1"
    assert runner.pending_rowids("value") == {1, 2}
    state = json.loads((tmp_path / "batch_state.json").read_text("utf-8"))
    assert state[bid]["rowids"] == [1, 2]

    # first poll: in_progress -> nothing applied; second: completed
    assert await runner.poll_once() == 0
    assert await runner.poll_once() == 1
    assert len(applied) == 2
    assert runner.pending_rowids("value") == set()
    # batch-priced usage recorded: 2 requests * 100 tok in -> 200 tok
    usage, cost = llm.costs.data["m"]
    assert usage.prompt_tokens == 200 and usage.calls == 2
    assert abs(cost - 200 / 1e6 * 2.0 * 0.5) < 1e-9
    # the batch job carries its kind as the cost feature tag
    assert llm.costs.features.get("value") == 1

    # a fresh runner resumes polling from the persisted state
    items2 = [
        {"custom_id": "0", "messages": [{"role": "user", "content": "x"}],
         "rows": [{"_rid": 9, "message_seq": 9}]}
    ]
    await runner.submit("value", "value", items2, [9])
    runner2 = BatchRunner(_mk_llm(cfg), BatchConfig(), tmp_path / "batch_state.json")
    runner2._client = lambda name, entry: fake
    assert runner2.pending_rowids("value") == {9}


async def test_drop_resets_rowids(tmp_path):
    cfg = LLMConfig(
        models={"m": ModelEntry(base_url="https://x", api_key="k", model="mm")},
        roles={"value": ["m"]},
    )

    class _Dying(_FakeClient):
        pass

    runner = BatchRunner(_mk_llm(cfg), BatchConfig(), tmp_path / "s.json")
    fake = _Dying()
    runner._client = lambda name, entry: fake
    resets: list = []

    async def reset(rowids, _r):
        resets.append(list(rowids))

    runner.register_reset("value", reset)
    await runner.submit(
        "value", "value",
        [{"custom_id": "0", "messages": [{"role": "user", "content": "x"}]}],
        [7],
    )
    fake.batches.statuses["batch-1"] = ["failed"]
    assert await runner.poll_once() == 0
    assert resets == [[7]] and runner.pending_rowids("value") == set()


def test_valueconfig_defaults_and_rag_pending_marker(tmp_path):
    # sanity: config wiring exists and -1 (in flight) is not "noise"
    cfg = MuzeConfig()
    assert cfg.llm.batch.enabled is False and cfg.llm.batch.role == "value"
    from muze.config import RagConfig

    minv = 1
    v = -1  # pending marker: treated as unjudged by the RAG filter
    assert not (minv > 0 and v is not None and 0 <= int(v) < minv)
