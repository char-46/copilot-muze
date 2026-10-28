"""Single index of every LLM prompt in muze.

Prompts stay physically next to the feature they steer: code and the words
that command it change in the SAME commit, ship in the same wheel and are
covered by the same tests. Moving them to an external text file would
create exactly the failure mode the user worries about -- code advancing
while the prompt file lags behind (user question 2026-10-28: 统一到外部
文本？-> 不外置；单一注册表 + 漂移守卫).

Audit 2026-10-28 caught this registry being born incomplete (profiles and
auto-title prompts were missing) -- tests/test_prompts.py now pins the
full inventory. Audit/tune prompts HERE (one import site).
"""

from muze.ai.agent import AUTO_TITLE_PROMPT, SYSTEM_PROMPT
from muze.digest import (
    DIGEST_PROMPT,
    FOLLOWUP_CHECK_PROMPT,
    FOLLOWUP_SYNC_PROMPT,
    MEMORY_PROMPT,
    ROLLUP_PROMPT,
)
from muze.evolve import EVOLVE_PROMPT
from muze.perception import _VALUE_PROMPT as VALUE_PROMPT
from muze.profiles import _SYSTEM as PROFILE_PROMPT

__all__ = [
    "SYSTEM_PROMPT",
    "DIGEST_PROMPT",
    "MEMORY_PROMPT",
    "ROLLUP_PROMPT",
    "FOLLOWUP_SYNC_PROMPT",
    "FOLLOWUP_CHECK_PROMPT",
    "VALUE_PROMPT",
    "PROFILE_PROMPT",
    "AUTO_TITLE_PROMPT",
    "EVOLVE_PROMPT",
]
