"""Python model catalog: pricing functions, catalog loading, registry
merge, and the cost ledger."""

from __future__ import annotations

import datetime as dt
import json

from muze.ai.llm import LLMClient
from muze.config import LLMConfig, LLMEndpoint, ModelCaps, ModelEntry
from muze.model_catalog import (
    CostTracker,
    ModelSpec,
    PeakOffPeak,
    Pricing,
    Usage,
    load_user_catalog,
)


# ------------------------------------------------------------ peak/offpeak
def test_peak_window_and_midnight_wrap():
    p = PeakOffPeak(off_start="00:30", off_end="08:30", off_factor=0.5)
    assert p.factor_at(dt.datetime(2026, 10, 9, 3, 0)) == 0.5  # inside
    assert p.factor_at(dt.datetime(2026, 10, 9, 12, 0)) == 1.0  # outside
    assert p.factor_at(dt.datetime(2026, 10, 9, 0, 29)) == 1.0  # just before
    assert p.factor_at(dt.datetime(2026, 10, 9, 8, 30)) == 1.0  # end exclusive
    # window wrapping midnight
    w = PeakOffPeak(off_start="22:00", off_end="06:30", off_factor=0.5)
    assert w.factor_at(dt.datetime(2026, 10, 9, 23, 0)) == 0.5
    assert w.factor_at(dt.datetime(2026, 10, 9, 3, 0)) == 0.5
    assert w.factor_at(dt.datetime(2026, 10, 9, 12, 0)) == 1.0


def test_pricing_math_all_modalities():
    when = dt.datetime(2026, 10, 9, 12, 0)  # peak hours
    pr = Pricing(
        input_per_mtok=2.0, output_per_mtok=8.0,
        image_per_call=0.01, audio_per_minute=0.05,
    )
    u = Usage(prompt_tokens=1_000_000, completion_tokens=500_000, images=3, audio_sec=120)
    assert abs(pr.cost(u, when) - (2.0 + 4.0 + 0.03 + 0.1)) < 1e-9
    # off-peak: token prices halved, modality flats untouched
    night = dt.datetime(2026, 10, 9, 3, 0)
    pr2 = Pricing(
        input_per_mtok=2.0, output_per_mtok=8.0,
        image_per_call=0.01, audio_per_minute=0.05,
        peak=PeakOffPeak(off_start="00:30", off_end="08:30", off_factor=0.5),
    )
    assert abs(pr2.cost(u, night) - (3.0 + 0.03 + 0.1)) < 1e-9


def test_modelspec_custom_price_function():
    # pricing can be a plain function (subscription tiers etc.)
    spec = ModelSpec(
        base_url="https://x", model="m",
        pricing=lambda usage, when: 0.0 if usage.calls < 100 else 1.0,
    )
    assert spec.price_of(Usage(calls=5), None) == 0.0
    assert spec.price_of(Usage(calls=200), None) == 1.0
    # dataclass Pricing goes through cost()
    spec2 = ModelSpec(base_url="https://x", model="m", pricing=Pricing(output_per_mtok=1.0))
    assert spec2.price_of(Usage(completion_tokens=1_000_000)) == 1.0
    assert ModelSpec(base_url="https://x", model="m").price_of(Usage()) is None


# ----------------------------------------------------------- catalog load
def test_load_user_catalog_valid_missing_broken(tmp_path):
    # missing -> {}
    assert load_user_catalog(tmp_path / "nope.py") == {}
    # valid file
    good = tmp_path / "models.py"
    good.write_text(
        "from muze.model_catalog import ModelSpec, Pricing\n"
        "MODELS = {'a': ModelSpec(base_url='https://x', model='m',"
        " pricing=Pricing(input_per_mtok=1.0))}\n",
        encoding="utf-8",
    )
    cat = load_user_catalog(good)
    assert set(cat) == {"a"} and cat["a"].pricing.input_per_mtok == 1.0
    # broken file -> {} (never takes the assistant down)
    bad = tmp_path / "bad.py"
    bad.write_text("raise RuntimeError('user typo')\n", encoding="utf-8")
    assert load_user_catalog(bad) == {}
    wrong = tmp_path / "wrong.py"
    wrong.write_text("MODELS = {'a': 42}\n", encoding="utf-8")
    assert load_user_catalog(wrong) == {}


# ------------------------------------------------------- registry merge
def _catalog_file(tmp_path, body: str) -> str:
    p = tmp_path / "models.py"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_client_merges_catalog_with_yaml_keys(tmp_path):
    path = _catalog_file(
        tmp_path,
        "from muze.model_catalog import ModelSpec, ModelCaps\n"
        "from muze.config import ModelCaps as MC\n"
        "MODELS = {\n"
        "  'mimo-vl': ModelSpec(base_url='https://cat', model='mimo-vl-7b',"
        " caps=MC(vision=True), rpm=99),\n"
        "  'keyless': ModelSpec(base_url='https://cat', model='x'),\n"
        "}\n",
    )
    cfg = LLMConfig(
        models={
            "mimo-vl": ModelEntry(api_key="yamlkey"),
            "keyless": ModelEntry(api_key=""),
        },
        catalog=path,
    )
    llm = LLMClient(cfg, data_dir=str(tmp_path))
    entries = llm._entries()
    assert "mimo-vl" in entries and "keyless" not in entries  # keyless stays dormant
    e = entries["mimo-vl"]
    assert e.base_url == "https://cat" and e.model == "mimo-vl-7b"
    assert e.api_key == "yamlkey" and e.caps.vision and e.rpm == 99
    # role routing reaches catalog entries
    cfg.roles = {"chat": ["mimo-vl"]}
    llm.apply_config(cfg)
    assert [n for n, _ in llm.resolve("chat")] == ["mimo-vl"]


def test_catalog_env_var_key(tmp_path, monkeypatch):
    path = _catalog_file(
        tmp_path,
        "from muze.model_catalog import ModelSpec\n"
        "MODELS = {'envy': ModelSpec(base_url='https://e', model='m')}\n",
    )
    monkeypatch.setenv("MUZE_KEY_ENVY", "envkey")
    llm = LLMClient(LLMConfig(catalog=path), data_dir=str(tmp_path))
    assert llm._entries()["envy"].api_key == "envkey"


def test_yaml_caps_override_catalog(tmp_path):
    path = _catalog_file(
        tmp_path,
        "from muze.config import ModelCaps\n"
        "from muze.model_catalog import ModelSpec\n"
        "MODELS = {'m': ModelSpec(base_url='https://c', model='m',"
        " caps=ModelCaps(vision=True))}\n",
    )
    cfg = LLMConfig(models={"m": ModelEntry(api_key="k")}, catalog=path)
    llm = LLMClient(cfg, data_dir=str(tmp_path))
    assert llm._entries()["m"].caps.vision is True
    # an explicitly non-default yaml caps replaces the catalog's
    cfg.models["m"].caps = ModelCaps(vision=False, tools=True, cheap=True)
    llm.apply_config(cfg)
    c = llm._entries()["m"].caps
    assert c.vision is False and c.cheap is True


# ------------------------------------------------------------- ledger
def test_cost_tracker_record_and_summary(tmp_path):
    import pathlib

    t = CostTracker(pathlib.Path(tmp_path) / "costs.json")
    when = dt.datetime(2026, 10, 9, 3, 0)
    t.record("dpsk", Usage(prompt_tokens=500_000, completion_tokens=250_000),
             1.5, when)
    t.record("dpsk", Usage(prompt_tokens=500_000), 0.5, when)
    t.record("glm", Usage(images=2), None, when)  # unpriced model
    day = t.summary("2026-10-09")
    assert day["dpsk"][:2] == [1_000_000, 250_000]
    assert abs(day["dpsk"][5] - 2.0) < 1e-9
    assert day["glm"][2] == 2 and day["glm"][5] == 0.0
    assert t.summary("2026-09") == {}  # other days filtered
    # persistence: a fresh tracker reads the same ledger
    t2 = CostTracker(pathlib.Path(tmp_path) / "costs.json")
    assert t2.summary("2026-10")["dpsk"][4] == 2  # calls


async def test_client_records_usage_with_catalog_pricing(tmp_path):
    path = _catalog_file(
        tmp_path,
        "from muze.model_catalog import ModelSpec, Pricing\n"
        "MODELS = {'m': ModelSpec(base_url='https://c', model='mm',"
        " pricing=Pricing(input_per_mtok=2.0))}\n",
    )
    cfg = LLMConfig(models={"m": ModelEntry(api_key="k")}, catalog=path,
                    roles={"chat": ["m"]})
    llm = LLMClient(cfg, data_dir=str(tmp_path))

    class _FakeCompletions:
        async def create(self, **kw):
            async def gen():
                from types import SimpleNamespace as NS

                yield NS(usage=None, choices=[
                    NS(finish_reason=None,
                       delta=NS(content="ok", reasoning_content=None, tool_calls=None))])
                yield NS(usage=NS(model_dump=lambda: {
                    "prompt_tokens": 1_000_000, "completion_tokens": 0}),
                    choices=[NS(finish_reason="stop", delta=NS(
                        content=None, reasoning_content=None, tool_calls=None))])

            return gen()

    from types import SimpleNamespace as NS

    llm._clients = {"m": NS(chat=NS(completions=_FakeCompletions()))}
    msg = await llm.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok"
    row = llm.costs.summary(dt.date.today().isoformat())["m"]
    assert row[0] == 1_000_000
    assert abs(row[5] - 2.0) < 1e-6  # priced via the catalog function
