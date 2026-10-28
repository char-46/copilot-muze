"""Prompt drift guards.

Prompts are behavior: when a tool or a rule lands, the words that teach it
must land in the same commit. These tests make drift a build failure --
SYSTEM_PROMPT must mention EVERY tool in the spec list, and the key
behavioral rules (dense ids, acceptance-before-close, ...) must exist in
the prompts that enforce them. Driven by user questions 2026-10-28
(目前的 system prompt 是否落后于版本？)."""

from muze.ai.agent import SYSTEM_PROMPT
from muze.ai.tools import SPEC as TOOL_SPECS
from muze.digest import DIGEST_PROMPT, FOLLOWUP_CHECK_PROMPT, FOLLOWUP_SYNC_PROMPT
from muze.prompts import (
    DIGEST_PROMPT as VIA_REGISTRY,
    FOLLOWUP_CHECK_PROMPT as CHECK_VIA_REGISTRY,
    MEMORY_PROMPT,
    ROLLUP_PROMPT,
    SYSTEM_PROMPT as SYSTEM_VIA_REGISTRY,
    VALUE_PROMPT,
)


def test_registry_exposes_every_prompt():
    # one import site for auditing/tuning; identity proves no shadow copies
    assert SYSTEM_VIA_REGISTRY is SYSTEM_PROMPT
    assert VIA_REGISTRY is DIGEST_PROMPT
    assert CHECK_VIA_REGISTRY is FOLLOWUP_CHECK_PROMPT
    assert MEMORY_PROMPT and ROLLUP_PROMPT and VALUE_PROMPT


def test_system_prompt_teaches_every_tool():
    names = {t["function"]["name"] for t in TOOL_SPECS}
    missing = sorted(n for n in names if n not in SYSTEM_PROMPT)
    assert not missing, f"SYSTEM_PROMPT drifted behind the toolset, untaught: {missing}"


def test_acceptance_semantics_in_prompts():
    # 交付≠完成: delivery waits for the user's acceptance, never auto-closes
    assert "交付≠完成" in SYSTEM_PROMPT and "验收" in SYSTEM_PROMPT
    assert "deliver" in FOLLOWUP_CHECK_PROMPT and "reject" in FOLLOWUP_CHECK_PROMPT
    assert "验收" in FOLLOWUP_CHECK_PROMPT


def test_dense_numbering_language_in_prompts():
    # every surface the LLM touches must speak the dense-number dialect
    assert "序号" in SYSTEM_PROMPT
    assert "序号" in FOLLOWUP_SYNC_PROMPT
    assert "#N 是序号" in FOLLOWUP_CHECK_PROMPT


def test_digest_prompt_defers_to_owner_profile():
    assert "画像" in DIGEST_PROMPT
