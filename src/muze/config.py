"""Configuration model and YAML loader.

All settings live in a single YAML file (see config.example.yaml). Secrets
(api keys, access tokens) should not be committed; `config.yaml` is gitignored.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError


class MilkyConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8080"
    access_token: str = ""
    reconnect_interval: float = 5.0
    request_timeout: float = 30.0


class StorageConfig(BaseModel):
    data_dir: Path = Path("./data")
    db_file: str = ""  # explicit sqlite path override; empty -> data_dir/muze.db

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return Path(self.db_file) if self.db_file else self.data_dir / "muze.db"


class Watchlist(BaseModel):
    friends: list[int] = Field(default_factory=list)
    groups: list[int] = Field(default_factory=list)


class BackfillConfig(BaseModel):
    on_start: bool = True
    max_pages_per_conv: int = 200
    page_limit: int = 30
    page_delay: float = 1.0
    conv_delay: float = 0.5
    rescan_interval_min: float = 30.0
    rescan_active_window_h: float = 24.0


class IngestConfig(BaseModel):
    scope: Literal["all", "watchlist"] = "all"
    watchlist: Watchlist = Field(default_factory=Watchlist)
    backfill: BackfillConfig = Field(default_factory=BackfillConfig)
    media_download_concurrency: int = 3


class ControlConfig(BaseModel):
    admin_users: list[int] = Field(default_factory=list)
    control_group: int = 0
    private_control: bool = True
    group_trigger: Literal["at_or_slash", "all"] = "at_or_slash"
    confirm_all_sends: bool = True
    confirm_timeout_min: float = 30.0


class LLMEndpoint(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None


class LLMConfig(BaseModel):
    chat: LLMEndpoint = Field(default_factory=LLMEndpoint)
    vision: LLMEndpoint = Field(default_factory=LLMEndpoint)
    agent_max_steps: int = 8
    asr_provider: str = ""  # reserved for a future ASR integration

    @property
    def vision_enabled(self) -> bool:
        vision_model = self.vision.model or self.chat.model
        return bool(vision_model)


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8300
    token: str = ""  # empty -> generated at startup and logged


class DigestConfig(BaseModel):
    """Nightly "dream": summarize the day's messages into a stored report."""

    enabled: bool = True
    at: str = "23:30"  # local time, daily
    window_hours: float = 24.0
    top_conversations: int = 6
    messages_per_conversation: int = 40
    deliver: bool = True  # send the report to the control conversation
    model_hint: str = ""  # optional override: use llm.chat as-is when empty


class LogConfig(BaseModel):
    level: str = "INFO"
    file: str = ""  # empty -> data_dir/logs/muze.log, appended forever


class DigestConfig(BaseModel):
    """Daily digest a.k.a. "dreaming": once per night, reflect on the passed
    day's archive, store the report, and grow long-term memories. Missing
    days are caught up automatically on the next run."""

    enabled: bool = True
    at: str = "01:00"  # local HH:MM (nightly)
    top_conversations: int = 6
    messages_per_conversation: int = 40
    deliver: bool = True
    extract_memories: bool = True
    max_memories_per_night: int = 8
    # hierarchical rollups for long-term context compression
    weekly_enabled: bool = True
    monthly_enabled: bool = True
    # inject dream reports + memories into every agent prompt so reports
    # actually shape how later messages are handled ("smarter over time")
    inject_context: bool = True
    inject_day_chars: int = 900
    inject_week_chars: int = 450
    inject_month_chars: int = 450
    inject_max_memories: int = 25


class MuzeConfig(BaseModel):
    milky: MilkyConfig = Field(default_factory=MilkyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)


def load_config(path: str | Path) -> MuzeConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"config file not found: {p} (copy config.example.yaml to config.yaml first)"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _validate_config(raw, p)
    if cfg.ingest.scope == "watchlist" and not (
        cfg.ingest.watchlist.friends or cfg.ingest.watchlist.groups
    ):
        raise ValueError("ingest.scope=watchlist but watchlist is empty")
    return cfg


def _validate_config(raw: dict, p) -> MuzeConfig:
    try:
        return MuzeConfig.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"invalid config {p}:\n{e}") from e


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class InstanceSpec:
    """One account stack: a name plus its fully-resolved MuzeConfig."""

    name: str
    config: MuzeConfig


def load_instances(path: str | Path) -> list[InstanceSpec]:
    """Load one or many account instances from a config file.

    Multi-account layout — top-level keys act as shared defaults, each
    `instances[]` entry deep-merges over them (per-account milky / storage /
    web / control ...)::

        llm: {...}                    # shared
        instances:
          - name: main
            milky: {base_url: "http://127.0.0.1:3010"}
            storage: {data_dir: ./data-a}
            web: {port: 8300}
          - name: alt
            milky: {base_url: "http://127.0.0.1:3011"}
            storage: {db_file: ./data-b/alt.db, data_dir: ./data-b}
            web: {port: 8301}

    Without `instances` the file is a classic single-account config.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"config file not found: {p} (copy config.example.yaml to config.yaml first)"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"invalid config {p}: not a mapping")

    entries = raw.get("instances")
    if isinstance(entries, list) and entries:
        base = {k: v for k, v in raw.items() if k != "instances"}
        specs: list[InstanceSpec] = []
        seen: set[str] = set()
        for i, inst in enumerate(entries):
            if not isinstance(inst, dict):
                raise ValueError(f"instances[{i}] must be a mapping")
            name = str(inst.get("name") or f"inst{i + 1}")
            if name in seen:
                raise ValueError(f"duplicate instance name: {name}")
            seen.add(name)
            merged = _deep_merge(base, inst)
            specs.append(InstanceSpec(name, _validate_config(merged, p)))
        return specs

    return [InstanceSpec("main", _validate_config(raw, p))]


def describe_watch(cfg: MuzeConfig, scene: str, peer_id: int) -> bool:
    """Should this conversation be ingested? Control conversations always are."""
    if is_control_conversation(cfg, scene, peer_id):
        return True
    if cfg.ingest.scope == "all":
        return True
    if scene == "friend":
        return peer_id in cfg.ingest.watchlist.friends
    if scene == "group":
        return peer_id in cfg.ingest.watchlist.groups
    return False


def is_control_conversation(cfg: MuzeConfig, scene: str, peer_id: int) -> bool:
    if scene == "group":
        return bool(cfg.control.control_group) and peer_id == cfg.control.control_group
    if scene == "friend":
        return cfg.control.private_control and peer_id in cfg.control.admin_users
    return False
