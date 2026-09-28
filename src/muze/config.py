"""Configuration model and YAML loader.

All settings live in a single YAML file (see config.example.yaml). Secrets
(api keys, access tokens) should not be committed; `config.yaml` is gitignored.
"""

from __future__ import annotations

from pathlib import Path
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

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "muze.db"


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


class LogConfig(BaseModel):
    level: str = "INFO"
    file: str = ""  # empty -> data_dir/logs/muze.log, appended forever


class MuzeConfig(BaseModel):
    milky: MilkyConfig = Field(default_factory=MilkyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    log: LogConfig = Field(default_factory=LogConfig)


def load_config(path: str | Path) -> MuzeConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"config file not found: {p} (copy config.example.yaml to config.yaml first)"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        cfg = MuzeConfig.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"invalid config {p}:\n{e}") from e
    if cfg.ingest.scope == "watchlist" and not (
        cfg.ingest.watchlist.friends or cfg.ingest.watchlist.groups
    ):
        raise ValueError("ingest.scope=watchlist but watchlist is empty")
    return cfg


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
