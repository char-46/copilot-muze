"""Configuration model and YAML loader.

All settings live in a single YAML file (see config.example.yaml). Secrets
(api keys, access tokens) should not be committed; `config.yaml` is gitignored.

Layout (breaking change since v0.1.0-alpha.27):

    application-level: web / log / storage.data_dir / ingest / llm / digest
    control:
      controller: [10000]              # authorized controller QQ ids (who)
      control_group: 0                 # group used as the control console (where)
      private_control: true
    accounts:
      milky:                           # one entry per protocol-side login
        - name: main                   # optional display name
          base_url: "http://127.0.0.1:3010"
          access_token: ""
          controller: []               # extra controllers for this account
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
    concurrency: int = 4
    rescan_interval_min: float = 30.0
    rescan_active_window_h: float = 24.0
    reconnect_gap_sec: float = 60.0  # min offline gap before a reconnect
    # triggers an immediate catch-up pull (smaller blips wait for rescan)


class IngestConfig(BaseModel):
    scope: Literal["all", "watchlist"] = "all"
    watchlist: Watchlist = Field(default_factory=Watchlist)
    backfill: BackfillConfig = Field(default_factory=BackfillConfig)
    media_download_concurrency: int = 3
    media_transcode_silk: bool = True
    # local QQ client cache dir (QQNT's nt_data etc.): best-effort media
    # dedup/adoption — file rows match by QQ's md5 file_hash, images/videos
    # by md5-embedded cache filenames; empty disables all of it
    qq_cache_dir: str = ""


class ControlConfig(BaseModel):
    controller: list[int] = Field(default_factory=list)  # authorized QQ ids (who)
    control_group: int = 0  # control-console group (where); 0 = disabled
    private_control: bool = True
    confirm_timeout_min: float = 30.0


class LLMEndpoint(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None
    timeout: float = 120.0  # stream stall timeout: max silence between chunks
    # (slow-but-producing output never times out; a stuck connection does)


class LLMConfig(BaseModel):
    chat: LLMEndpoint = Field(default_factory=LLMEndpoint)
    vision: LLMEndpoint = Field(default_factory=LLMEndpoint)
    agent_max_steps: int = 48
    turn_timeout_sec: float = 300.0  # STALL cap: a tool round stuck this long
    # is aborted; there is no wall-clock cap on slow-but-progressing turns
    # how many turns may talk to the model at once (per account); extra turns
    # wait for a slot, and beyond max_queued_turns they are refused with 忙
    max_concurrent_turns: int = 4
    max_queued_turns: int = 8
    auto_title: bool = True  # LLM-summarized session titles (/sess list)
    # in-turn progress feedback: "still waiting" heartbeats + tool call lines
    progress: bool = True
    progress_wait_sec: float = 20.0  # silence before the first heartbeat
    progress_interval_sec: float = 60.0  # between subsequent heartbeats
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


class ProfilesConfig(BaseModel):
    """Per-conversation identity docs ("who is who"), maintained by the LLM."""

    auto: bool = True  # periodically (re)build identity docs for active chats
    interval_min: float = 360.0  # how often the refresh pass runs
    active_window_h: float = 48.0  # only conversations active within this window
    messages_per_conv: int = 120  # messages fed to the summarizer
    max_chars: int = 1200  # max length of one identity doc
    inject: bool = True  # add a compact "who is who" index to the system prompt
    inject_chars: int = 80  # per-profile snippet injected


class RagEmbedding(BaseModel):
    """Embedding backend. Local-first (Qwen3-Embedding), remote only as a
    configured fallback. One model id = one vector space: switching models
    rebuilds the index (see VecStore)."""

    provider: str = "auto"  # auto (local-first, remote fallback if configured) | local | openai
    backend: str = "st"  # st (sentence-transformers/torch, supports Qwen) | fastembed
    model: str = "Qwen/Qwen3-Embedding-0.6B"  # local model id
    query_instruction: str = ""  # optional instruction prefix for queries
    base_url: str = ""  # remote fallback endpoint (empty -> reuse llm.chat.*)
    api_key: str = ""
    remote_model: str = ""  # set to ENABLE the remote fallback
    dims: int = 0  # 0 = infer from the first embedding
    batch: int = 16
    min_interval_sec: float = 0.05  # throttle between local batches
    max_retries: int = 3  # local failures before the cooldown kicks in
    cooldown_sec: float = 300.0  # back-off after repeated failure
    device: str = "auto"  # auto (tiered pick, see ResourceGovernor) | cuda | cpu
    gpu_weight_gb: float = 1.6  # VRAM the model needs (0.6B fp16 + overhead)
    gpu_reserve_gb: float = 0.8  # comfortable tier headroom (squeeze tier eats it)
    gpu_max_util: float = 50.0  # use GPU only while utilisation is below this
    vram_evict_pct: float = 85.0  # system-wide VRAM usage >= this evicts a GPU-resident model to RAM (0=off)
    cpu_max_threads: int = 4  # cap for CPU inference (dynamic below this)
    unload_idle_sec: float = 1800.0  # free the local model after this idle (0=never)


class RagConfig(BaseModel):
    """Vector retrieval over summaries (profiles/memories/digests) and,
    opt-in, chat message windows. Indexing is idle-scheduled (AC power,
    quiet CPU, no very recent activity; a busy GPU just forces CPU) and
    interruptible. Every incoming message can auto-inject semantic hits
    (``auto_inject``) so recall works without the agent calling tools."""

    enabled: bool = False  # master switch (needs a usable rag.embedding)
    chat: bool = False  # ALSO index message windows (opt-in, default off)
    embedding: RagEmbedding = Field(default_factory=RagEmbedding)
    top_k: int = 6
    window_messages: int = 12  # chat window size (messages)
    window_chars: int = 900  # and/or chars per chat window
    max_doc_chars: int = 2000  # truncate a doc before embedding
    index_interval_min: float = 5.0  # background indexing cadence (still gated)
    # --- auto-inject: semantic hits for each incoming message ---
    auto_inject: bool = True  # retrieve per message and add hits to the prompt
    inject_k: int = 5  # hits per message
    inject_chars: int = 300  # chars per hit
    # --- resource governor (best-effort; skipped when psutil/pynvml missing) ---
    require_ac_power: bool = True  # only run on AC power (needs psutil battery)
    idle_cpu_pct: float = 40.0  # run only while CPU busy% is below this
    min_idle_sec: float = 30.0  # and no user activity for this long


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
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    rag: RagConfig = Field(default_factory=RagConfig)


ENDPOINT_KEYS = tuple(MilkyEndpoint.model_fields)
ACCOUNT_ENTRY_RESERVED = ("name", "controller", "web") + ENDPOINT_KEYS


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
    access_token, ...), `name`, `controller` (extra controllers, unioned
    with the global `control.controller` list) and any section override
    except `web` (the webui is app-level, one frontend for all accounts).
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
    base_control = dict(base.get("control") or {})
    # controller ids: new control.controller, plus the legacy top-level
    # `authority:` (dropped from the schema) — unioned for a soft migration.
    global_controller = {
        int(u)
        for u in (base_control.get("controller") or [])
    } | {int(u) for u in (base.pop("authority", None) or [])}
    base_control["controller"] = sorted(global_controller)
    base["control"] = base_control

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
        ent_control = dict(ent.get("control") or {})
        extra_controller = {
            int(u) for u in (ent.pop("controller", None) or [])
        } | {int(u) for u in (ent.pop("authority", None) or [])}
        extra_controller |= {int(u) for u in (ent_control.pop("controller", None) or [])}
        if ent_control:
            ent["control"] = ent_control
        endpoint_kwargs = {k: ent.pop(k) for k in list(ent) if k in ENDPOINT_KEYS}

        merged = _deep_merge(base, ent)
        merged.setdefault("control", {})
        merged["control"]["controller"] = sorted(
            global_controller | extra_controller
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
        return cfg.control.private_control and peer_id in cfg.control.controller
    return False
