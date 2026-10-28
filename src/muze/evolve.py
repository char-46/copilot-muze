"""One-shot self-evolution at version change.

Cost model (user direction 2026-10-28): never carry update knowledge in
every LLM call -- it inflates each request and breaks prompt-cache reuse of
the fixed prefix. Instead, exactly ONCE per deployed version (at boot, or
via /evolve), the agent compares the release notes against its own
long-term memories + habit notes and rewrites them:

  - retire memories that teach behavior a shipped version replaced
    (e.g. the pre-alpha.148 raw-id /todo syntax);
  - add kind='habit', subject='muze' entries -- the agent's own operating
    habits for the NEW capabilities, injected later as a small static
    block (static within a deployment => cache-friendly).

The hot path (_system_prompt) stays byte-identical across turns.
"""

import json
import logging
import re

from muze.changelog import RELEASE_NOTES
from muze.version import BASE_VERSION

log = logging.getLogger("muze.evolve")

EVOLVE_PROMPT = """你是 muze（个人 QQ copilot）的自进化模块。系统刚完成一次版本更新。请对照【变更日志】检查你的现有记忆与操作习惯，让未来的你按新行为工作。

# 规则
- retire：记忆/习惯描述的用法已被变更替换（教了废弃的命令写法、旧流程、旧限制），输出 {{"action":"retire","id":该记忆id,"content":"被 <版本> 替换：一句话"}}。
- add：为新能力沉淀操作习惯，subject 固定 'muze'、kind 固定 'habit'，内容写成给未来的自己的操作指令（何时用、怎么用、注意什么），一条一个主题。
- 与变更无关的记忆（人际关系、偏好、事实）一律不动。
- 最多输出 {maxn} 条；没有要改的就输出 []。

# 变更日志（{old} -> {new}）
{changes}

# 现有记忆与习惯
{existing}

# 输出（严格 JSON 数组，不要其他文字）
[{{"action":"retire","id":1,"content":"..."}},
 {{"action":"add","subject":"muze","kind":"habit","content":"待办引用一律用 /todo 里的稠密序号，不用数据库 id"}}]"""


def _notes_since(old_version: str, cap: int = 8) -> str:
    """Release notes from `old_version` (exclusive) to newest, newest
    first. An unknown/missing old version yields everything (capped)."""
    out: list[dict] = []
    for e in RELEASE_NOTES:
        out.append(e)
        if e["version"] == old_version:
            break
    return "\n\n".join(
        f"[{e['version']} / {e['date']}]\n" + "\n".join(f"- {c}" for c in e["changes"])
        for e in out[:cap]
    )


async def evolve_if_due(repo, llm, cfg, chatlog=None, *, force: bool = False) -> int:
    """Run the self-evolution pass once per version. Returns the number of
    memory changes applied (0 = not due / nothing to do / failed safely)."""
    current = (await repo.get_meta("evolved_version") or "").strip()
    if current == BASE_VERSION and not force:
        return 0
    if llm is None or not llm.configured:
        return 0  # try again next boot

    existing = await repo.list_memories(limit=100)
    existing_txt = "\n".join(
        f"- #{m['id']} [{m['subject']}/{m['kind']}] {m['content']}" for m in existing
    ) or "(暂无)"
    prompt = EVOLVE_PROMPT.format(
        old=current or "(首次)", new=BASE_VERSION,
        changes=_notes_since(current) if current else _notes_since("", cap=8),
        existing=existing_txt[:8000],
        maxn=12,
    )
    try:
        msg = await llm.chat([{"role": "user", "content": prompt}])
    except Exception as e:
        log.warning("evolve LLM call failed (will retry next boot): %s", e)
        return 0
    raw = (msg.get("content") or "").strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        log.warning("evolve returned non-JSON, skipped: %.200s", raw)
        return 0
    try:
        items = json.loads(m.group(0))
    except ValueError:
        log.warning("evolve JSON parse failed, skipped")
        return 0

    changed = 0
    for item in items[:12]:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if action == "retire":
            mid = item.get("id")
            if isinstance(mid, int):
                try:
                    if await repo.retire_memory(mid):
                        changed += 1
                        if chatlog:
                            chatlog.log.info(
                                "EVOLVE     | - #%s retired: %s", mid,
                                str(item.get("content", ""))[:80],
                            )
                except Exception:
                    pass
        elif action == "add":
            content = str(item.get("content", "")).strip()
            if content:
                await repo.add_memory(
                    "habit", "muze", content, source=f"evolve:{BASE_VERSION}"
                )
                changed += 1
                if chatlog:
                    chatlog.log.info("EVOLVE     | + [muze/habit] %s", content[:80])
    await repo.set_meta("evolved_version", BASE_VERSION)
    log.info("self-evolution applied %s change(s) for %s", changed, BASE_VERSION)
    return changed
