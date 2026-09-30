"""Configuration model and YAML loader.

All settings live in a single YAML file (see config.example.yaml). Secrets
(api keys, access tokens) should not be committed; `config.yaml` is gitignored.

Layout (breaking change since v0.1.0-alpha.27):

    application-level: web / log / storage.data_dir / ingest / llm / digest
    authority: [10000]                 # authorized controller QQ ids (global)
    accounts:
      milky:                           # one entry per protocol-side login
        - name: main                   # optional display name
          base_url: "http://127.0.0.1:3010"
          access_token: ""
          authority: []                # extra controllers for this account
          digest: {enabled: false}     # optional per-account overrides
      ob11: []                         # reserved, not implemented yet
      lagrange: []                     # reserved, not implemented yet

Top-level sections act as shared defaults; each account entry deep-merges its
overrides over them. Per account, data dirs are derived automatically from
the logged-in QQ number: <data_dir>/<uin>/{muze.db, media/}.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError


class MilkyEndpoint(BaseModel):
    """Connection to one Milky-protocol side (LLBot / Lagrange.Milky)."""

    base_url: str = "http://127.0.0.1:8080"
    access_token: str = ""
    reconnect_interval: float = 5.0
    request_timeout: float = 30.0


class AccountsConfig(BaseModel):
    """Account sources, grouped by protocol implementation.

    Only `milky` is wired up today; ob11/lagrange are reserved so configs
    can be written ahead of the adapters.
    """

    milky: list[dict] = Field(default_factory=list)
    ob11: list[dict] = Field(default_factory=list)
    lagrange: list[dict] = Field(default_factory=list)


class StorageConfig(BaseModel):
    # root dir; per-account dirs are <data_dir>/<uin>/ (auto after login)
    data_dir: Path = Path("./data")

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def account_dir(self, uin: int) -> Path:
        """Per-account data dir (created): the db lives here; media bytes go
        to the shared app-level blob store instead."""
        acct = self.data_dir / str(uin)
        acct.mkdir(parents=True, exist_ok=True)
        return acct


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
    control_group: int = 0
    private_control: bool = True
    confirm_timeout_min: float = 30.0


class LLMEndpoint(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None


class LLMConfig(BaseModel):
    chat: LLMEndpoint = Field(default_factory=LLMEndpoint)
    vision: LLMEndpoint = Field(default_factory=LLMEndpoint)
    agent_max_steps: int = 12
    asr_provider: str = ""  # reserved for a future ASR integration

    @property
    def vision_enabled(self) -> bool:
        vision_model = self.vision.model or self.chat.model
        return bool(vision_model)


class WebConfig(BaseModel):
    """App-level webui: one web frontend serves every account in the process."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8300
    token: str = ""  # empty -> generated at startup and logged


class LogConfig(BaseModel):
    level: str = "INFO"
    file: str = ""  # empty -> data_dir/logs/muze.log, appended forever


class ResearchConfig(BaseModel):
    """Web search backend for the agent's research closure."""

    provider: str = ""  # tavily | brave | serper | searxng | duckduckgo | ""
    api_key: str = ""
    base_url: str = ""  # searxng instance / override
    max_results: int = 5
    fetch_chars: int = 4000  # per-page extract kept in research()
    timeout: float = 20.0


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
    accounts: AccountsConfig = Field(default_factory=AccountsConfig)
    authority: list[int] = Field(default_factory=list)  # controller QQ ids
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)


ENDPOINT_KEYS = tuple(MilkyEndpoint.model_fields)
ACCOUNT_ENTRY_RESERVED = ("name", "authority", "web") + ENDPOINT_KEYS


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
class AccountSpec:
    """One account stack: display name, protocol endpoint, resolved config."""

    name: str
    protocol: str  # "milky"
    endpoint: MilkyEndpoint
    config: MuzeConfig


def load_accounts(path: str | Path) -> list[AccountSpec]:
    """Resolve every configured account into a fully-merged AccountSpec.

    Top-level sections are shared defaults; each `accounts.milky[]` entry
    deep-merges over them. Entry keys: endpoint fields (base_url,
    access_token, ...), `name`, `authority` (extra controllers, unioned
    with the global list) and any section override except `web` (the webui
    is app-level, one frontend for all accounts).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"config file not found: {p} (copy config.example.yaml to config.yaml first)"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"invalid config {p}: not a mapping")

    if "milky" in raw or "instances" in raw:
        raise ValueError(
            "配置格式已变更（不兼容旧版）：顶层 milky:/instances: 已移除，"
            "请改用 accounts.milky 列表，见 config.example.yaml"
        )

    accounts = raw.get("accounts") or {}
    if not isinstance(accounts, dict):
        raise ValueError(f"invalid config {p}: accounts must be a mapping")
    for proto in ("ob11", "lagrange"):
        if accounts.get(proto):
            raise ValueError(
                f"accounts.{proto} 尚未支持（当前仅 milky），请从配置中移除或留空"
            )
    entries = accounts.get("milky")
    if not entries:
        raise ValueError(
            f"invalid config {p}: accounts.milky 至少需要一项（base_url 指向协议端）"
        )

    base = {k: v for k, v in raw.items() if k != "accounts"}
    global_authority = base.get("authority") or []

    specs: list[AccountSpec] = []
    seen: set[str] = set()
    for i, ent in enumerate(entries):
        if not isinstance(ent, dict):
            raise ValueError(f"accounts.milky[{i}] must be a mapping")
        ent = dict(ent)
        name = str(ent.pop("name", "") or f"milky-{i + 1}")
        if name in seen:
            raise ValueError(f"duplicate account name: {name}")
        seen.add(name)
        if "web" in ent:
            raise ValueError(
                f"accounts.milky[{i}] ({name}): web 是应用级配置，不能按账号覆盖"
            )
        extra_authority = ent.pop("authority", None) or []
        endpoint_kwargs = {k: ent.pop(k) for k in list(ent) if k in ENDPOINT_KEYS}

        merged = _deep_merge(base, ent)
        merged["authority"] = sorted(
            {int(u) for u in global_authority} | {int(u) for u in extra_authority}
        )
        cfg = _validate_config(merged, p)
        if cfg.ingest.scope == "watchlist" and not (
            cfg.ingest.watchlist.friends or cfg.ingest.watchlist.groups
        ):
            raise ValueError("ingest.scope=watchlist but watchlist is empty")

        try:
            endpoint = MilkyEndpoint.model_validate(endpoint_kwargs)
        except ValidationError as e:
            raise ValueError(f"accounts.milky[{i}] ({name}) endpoint invalid:\n{e}") from e
        specs.append(AccountSpec(name, "milky", endpoint, cfg))
    return specs


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
        return cfg.control.private_control and peer_id in cfg.authority
    return False
