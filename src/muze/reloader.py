"""/reload hot-update:

    git pull && /reload

1. Code check: if any module under src/muze changed → tell the user and
   trigger a daemon-managed restart (exit 100) so the new code loads.
2. config.yaml re-applied in place (LLM endpoints, control policy, scope…).
3. skills/ + data/skills/ rescanned.
4. AI conversation contexts cleared.

Everything that does NOT require a process restart is applied immediately.
"""

from __future__ import annotations

import logging
from pathlib import Path

import muze
from muze.config import MilkyEndpoint, MuzeConfig, load_accounts
from muze.core.service import CommandOutcome
from muze.skills import SkillStore

log = logging.getLogger("muze.reload")


class CodeWatcher:
    """Detects .py changes under the muze package since the last snapshot."""

    def __init__(self) -> None:
        self.root = Path(muze.__file__).parent
        self.snapshot = self._take()

    def _take(self) -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        for p in self.root.rglob("*.py"):
            try:
                st = p.stat()
                out[str(p.relative_to(self.root))] = (st.st_mtime, st.st_size)
            except OSError:
                continue
        return out

    def changed(self) -> list[str]:
        current = self._take()
        old = self.snapshot
        diff = [
            k
            for k in current
            if k not in old or current[k] != old[k]
        ] + [k for k in old if k not in current]
        # refresh snapshot so the next /reload compares against now
        self.snapshot = current
        return sorted(diff)


class Reloader:
    def __init__(
        self,
        *,
        config_path: str,
        cfg: MuzeConfig,
        endpoint: MilkyEndpoint,
        skills: SkillStore,
        llm,
        agent,
        runtime,
    ) -> None:
        self.config_path = config_path
        self.cfg = cfg
        self.endpoint = endpoint
        self.skills = skills
        self.llm = llm
        self.agent = agent
        self.runtime = runtime
        self.code = CodeWatcher()

    async def reload(self) -> CommandOutcome:
        lines: list[str] = []

        # 1) code changes -> restart via daemon
        changed = self.code.changed()
        if changed:
            lines.append(
                f"检测到 {len(changed)} 个源码文件变更（如 {', '.join(changed[:3])}…），"
                "代码需要重启才能生效"
            )
            if self.runtime.request_restart is not None:
                self.runtime.request_restart()
                lines.append("已请求重启：进程将重新启动以加载新代码")
                return CommandOutcome("\n".join(lines))
            lines.append("（未装配重启钩子，请手动重启 serve）")
        else:
            lines.append("源码无变更，热重载继续")

        # 2) config (re-resolve this account's spec by name)
        try:
            name = getattr(self.runtime, "instance_name", "")
            specs = load_accounts(self.config_path)
            spec = next((s for s in specs if s.name == name), None)
            if spec is None:
                raise ValueError(
                    f"账号 {name!r} 不在配置中（现有: {', '.join(s.name for s in specs)}）"
                )
            for field in MuzeConfig.model_fields:
                setattr(self.cfg, field, getattr(spec.config, field))
            endpoint_changed = False
            for f in MilkyEndpoint.model_fields:
                new_v = getattr(spec.endpoint, f)
                if getattr(self.endpoint, f) != new_v:
                    endpoint_changed = True
                setattr(self.endpoint, f, new_v)
            note = "（milky 端点变更重启后完全生效）" if endpoint_changed else ""
            lines.append(f"config.yaml 已重载{note}（storage.data_dir/web 监听地址变更需重启）")
        except Exception as e:
            lines.append(f"config 重载失败（沿用旧配置）: {e}")

        # 3) skills
        n = self.skills.reload()
        lines.append(f"skills: {n} 个已加载")

        # 4) llm + agent contexts
        self.llm.apply_config(self.cfg.llm)
        if self.agent is not None:
            self.agent.reset_all()
        lines.append("LLM 配置已应用，AI 对话上下文已清空")

        log.info("reload done: %s", " | ".join(lines))
        return CommandOutcome("\n".join(lines))
