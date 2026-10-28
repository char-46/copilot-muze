"""Single index of every LLM prompt in muze.

Prompts stay physically next to the feature they steer: code and the words
that command it change in the SAME commit, ship in the same wheel and are
covered by the same tests. Moving them to an external text file would
create exactly the failure mode the user worries about -- code advancing
while the prompt file lags behind (user question 2026-10-28: 统一到外部
文本？-> 不外置；单一注册表 + 漂移守卫).

Audit/tune prompts HERE (one import site); tests/test_prompts.py fails the
build the moment a tool exists that SYSTEM_PROMPT never teaches.
"""

from muze.ai.agent import SYSTEM_PROMPT
from muze.digest import (
    DIGEST_PROMPT,
    FOLLOWUP_CHECK_PROMPT,
    FOLLOWUP_SYNC_PROMPT,
    MEMORY_PROMPT,
    ROLLUP_PROMPT,
)
from muze.perception import _VALUE_PROMPT as VALUE_PROMPT

__all__ = [
    "SYSTEM_PROMPT",
    "DIGEST_PROMPT",
    "MEMORY_PROMPT",
    "ROLLUP_PROMPT",
    "FOLLOWUP_SYNC_PROMPT",
    "FOLLOWUP_CHECK_PROMPT",
    "VALUE_PROMPT",
]
