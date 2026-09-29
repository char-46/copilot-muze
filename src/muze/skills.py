"""Skills: drop-in instruction documents for the agent.

Layout (both scanned, repo skills ship via git; user skills stay local):
    skills/<name>/SKILL.md
    data/skills/<name>/SKILL.md

SKILL.md format:
    ---
    name: doc-sources          # optional, defaults to dir name
    description: one line      # required-ish; powers the prompt index
    ---
    markdown body …the actual instructions…

Context strategy: only "name: description" lines live in the system prompt;
the body is fetched on demand via the read_skill tool.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("muze.skills")

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path

    def index_line(self) -> str:
        return f"- {self.name}: {self.description}" if self.description else f"- {self.name}"


class SkillStore:
    def __init__(self, dirs: list[Path]) -> None:
        self.dirs = [Path(d) for d in dirs]
        self._skills: dict[str, Skill] = {}

    def reload(self) -> int:
        skills: dict[str, Skill] = {}
        for base in self.dirs:
            if not base.is_dir():
                continue
            for sk_dir in sorted(base.iterdir()):
                if not sk_dir.is_dir():
                    continue
                md = sk_dir / "SKILL.md"
                if not md.is_file():
                    continue
                try:
                    sk = self._parse(md, fallback_name=sk_dir.name)
                except Exception as e:
                    log.warning("bad skill %s: %s", md, e)
                    continue
                # user-dir entries (later dirs) override repo ones
                skills[sk.name] = sk
        self._skills = skills
        log.info("skills loaded: %s from %s", sorted(skills), [str(d) for d in self.dirs])
        return len(skills)

    @staticmethod
    def _parse(md: Path, fallback_name: str) -> Skill:
        raw = md.read_text(encoding="utf-8", errors="replace")
        name, desc = fallback_name, ""
        body = raw
        m = FM_RE.match(raw)
        if m:
            meta = m.group(1)
            body = raw[m.end():]
            for line in meta.splitlines():
                k, _, v = line.partition(":")
                k, v = k.strip().lower(), v.strip()
                if k == "name" and v:
                    name = v
                elif k == "description":
                    desc = v
        return Skill(name=name, description=desc, body=body.strip(), path=md)

    def list(self) -> list[Skill]:
        return [self._skills[k] for k in sorted(self._skills)]

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def index_lines(self) -> list[str]:
        return [s.index_line() for s in self.list()]
