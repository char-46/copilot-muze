"""SQLite database bootstrap and migrations."""

from __future__ import annotations

import logging

import aiosqlite

log = logging.getLogger("muze.store")

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    scene         TEXT    NOT NULL,
    peer_id       INTEGER NOT NULL,
    name          TEXT    NOT NULL DEFAULT '',
    last_seq      INTEGER NOT NULL DEFAULT 0,
    last_active_at INTEGER,
    -- multi-watermark backfill state (schema v8):
    bf_cursor_seq INTEGER,          -- resume cursor: next_message_seq of the
                                    -- last page of an UNFINISHED deep pull
    bf_target_seq INTEGER,          -- resume lower bound: pull until oldest
                                    -- seq <= this (NULL = no pending pull)
    bf_since_ts   REAL,             -- time-bounded pull: stop at messages older
                                    -- than this (NULL = seq-bounded pull)
    bf_last_run_at REAL,            -- wall clock of the last backfill run
    last_op_at    REAL,             -- wall clock of the last stored message
    PRIMARY KEY (scene, peer_id)
);

CREATE TABLE IF NOT EXISTS messages (
    scene        TEXT    NOT NULL,
    peer_id      INTEGER NOT NULL,
    message_seq  INTEGER NOT NULL,
    sender_id    INTEGER NOT NULL,
    time         INTEGER NOT NULL,
    raw_json     TEXT    NOT NULL,
    text_content TEXT    NOT NULL DEFAULT '',
    recalled     INTEGER NOT NULL DEFAULT 0,
    source       TEXT    NOT NULL DEFAULT 'event',
    ingested_at  INTEGER NOT NULL,
    PRIMARY KEY (scene, peer_id, message_seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages (time);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages (sender_id);
CREATE INDEX IF NOT EXISTS idx_messages_content ON messages (text_content);

CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene       TEXT    NOT NULL,
    peer_id     INTEGER NOT NULL,
    message_seq INTEGER NOT NULL,
    seg_index   INTEGER NOT NULL,
    kind        TEXT    NOT NULL,             -- image/record/video/file
    resource_id TEXT,
    file_id     TEXT,
    file_name   TEXT,
    file_hash   TEXT,
    file_path   TEXT,
    file_size   INTEGER,
    width       INTEGER,
    height      INTEGER,
    duration    INTEGER,
    summary     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/downloading/done/failed/skipped
    error       TEXT,
    content_hash TEXT,                             -- sha256 of the blob (dedup)
    time        INTEGER,                           -- message timestamp (unix sec)
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE (scene, peer_id, message_seq, seg_index)
);
CREATE INDEX IF NOT EXISTS idx_media_status ON media (status);
CREATE INDEX IF NOT EXISTS idx_media_hash ON media (content_hash);

CREATE TABLE IF NOT EXISTS pending_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,            -- send
    scene           TEXT NOT NULL,
    peer_id         INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    control_scene   TEXT NOT NULL,
    control_peer_id INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/executed/cancelled/expired
    created_by      TEXT NOT NULL DEFAULT '',         -- command/agent/web/cli
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,            -- once | interval
    prompt          TEXT NOT NULL,            -- handed to the agent at fire time
    control_scene   TEXT NOT NULL,            -- where the result is delivered
    control_peer_id INTEGER NOT NULL,
    next_run_at     INTEGER NOT NULL,         -- unix seconds
    interval_sec    INTEGER,
    status          TEXT NOT NULL DEFAULT 'active',  -- active|running|done|cancelled|error
    created_by      TEXT NOT NULL DEFAULT '',
    note            TEXT,
    last_result     TEXT,
    lease_until     INTEGER,                  -- running: claim expiry (owner died mid-run)
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON scheduled_tasks (status, next_run_at);

CREATE TABLE IF NOT EXISTS daily_digests (
    date         TEXT PRIMARY KEY,        -- day: YYYY-MM-DD | week: YYYY-Www | month: YYYY-MM
    period       TEXT NOT NULL DEFAULT 'day',  -- day | week | month
    created_at   INTEGER NOT NULL,
    window_hours REAL,
    report       TEXT NOT NULL,
    stats_json   TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- identity | preference | relation | fact
    subject     TEXT NOT NULL,            -- 'owner' | '<uin>' | 'group/<gid>'
    content     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '', -- dream:<date> | chat | command
    confidence  REAL NOT NULL DEFAULT 1.0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories (subject, active);

CREATE TABLE IF NOT EXISTS ai_sessions (
    id            TEXT PRIMARY KEY,           -- short hex id (#a1b2c3)
    chat_key      TEXT NOT NULL,              -- control conversation it belongs to
    prompt        TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',   -- auto/LLM title for /sess list
    status        TEXT NOT NULL DEFAULT 'running',  -- running|done|error|suspend
    step          INTEGER NOT NULL DEFAULT 0, -- model rounds completed (resume)
    created       REAL NOT NULL,              -- wall clock
    updated       REAL NOT NULL,
    result_preview TEXT NOT NULL DEFAULT '',
    messages_json TEXT NOT NULL DEFAULT '[]', -- tool-loop messages, system stripped
    events_json   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_chat ON ai_sessions (chat_key, updated);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_status ON ai_sessions (status, updated);

CREATE TABLE IF NOT EXISTS profiles (
    subject    TEXT PRIMARY KEY,           -- friend/<uin> | group/<gid>
    name       TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL DEFAULT '',   -- markdown identity doc
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_docs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,              -- profile|memory|digest|chat
    ref        TEXT NOT NULL,              -- stable key
    scene      TEXT,
    peer_id    INTEGER,
    time_start INTEGER,
    time_end   INTEGER,
    text       TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    UNIQUE (kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_rag_docs_kind ON rag_docs (kind);
CREATE INDEX IF NOT EXISTS idx_rag_docs_conv ON rag_docs (scene, peer_id);

CREATE TABLE IF NOT EXISTS rag_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def open_db(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=30000")
    await conn.execute("PRAGMA foreign_keys=ON")
    await migrate(conn)

    # connection-level BUSY resilience: every write through this connection
    # (any repo method) retries on transient "database is locked" instead of
    # killing its caller. busy_timeout already absorbs normal contention;
    # this catches the starved tail (e.g. an orphaned process finishing its
    # last writes). A BUSY statement never applied, so retrying is safe.
    import asyncio as _asyncio

    class _RetryingExecute:
        """Awaitable AND async-with (mirrors aiosqlite's execute protocol)."""

        __slots__ = ("_conn",)

        def __init__(self, conn):
            self._conn = conn

        async def _acquire(self, sql, params):
            delay = 0.5
            for attempt in range(4):
                if params is None:
                    cm = self._conn._muze_orig_execute(sql)
                else:
                    cm = self._conn._muze_orig_execute(sql, params)
                try:
                    return await cm
                except aiosqlite.OperationalError as e:
                    msg = str(e).lower()
                    if "locked" not in msg and "busy" not in msg:
                        raise
                    if attempt == 3:
                        raise
                    log.warning(
                        "sqlite busy (attempt %d) on %.40s...; retry in %.1fs",
                        attempt + 1, str(sql).strip(), delay,
                    )
                    await _asyncio.sleep(delay)
                    delay *= 2
            raise RuntimeError("unreachable")

        def __await__(self):
            raise TypeError("call conn.execute(sql, params) first")

        def __call__(self, sql, params=None):
            # params pass through UNTOUCHED: dict (named :params), tuple,
            # list — sqlite3 accepts them all as given
            return _BoundRetry(self, sql, params)

    class _BoundRetry:
        __slots__ = ("_owner", "_sql", "_params", "_cur")

        def __init__(self, owner, sql, params):
            self._owner = owner
            self._sql = sql
            self._params = params
            self._cur = None

        def __await__(self):
            return self._owner._acquire(self._sql, self._params).__await__()

        async def __aenter__(self):
            self._cur = await self._owner._acquire(self._sql, self._params)
            return self._cur

        async def __aexit__(self, *exc):
            if self._cur is not None:
                try:
                    await self._cur.close()
                except Exception:
                    pass
            return None

    conn._muze_orig_execute = conn.execute  # real method; test seam
    conn.execute = _RetryingExecute(conn)  # type: ignore[method-assign]

    # sqlite-vec powers semantic (vector) retrieval; best-effort — RAG just
    # stays unavailable if the extension can't be loaded.
    try:
        import sqlite_vec

        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
    except Exception as e:  # pragma: no cover - environment dependent
        log.warning("sqlite-vec not loaded (%s); vector RAG disabled", e)
    return conn


async def _migrate_v5_add_period(conn: aiosqlite.Connection) -> None:
    """daily_digests gained a `period` column in v5 (existing rows are days)."""
    async with conn.execute("PRAGMA table_info(daily_digests)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    if "period" not in cols:
        await conn.execute(
            "ALTER TABLE daily_digests ADD COLUMN period TEXT NOT NULL DEFAULT 'day'"
        )
        await conn.commit()


async def _migrate_v6_media_hash(conn: aiosqlite.Connection) -> None:
    """media gained a content_hash column in v6 (cross-account blob dedup)."""
    async with conn.execute("PRAGMA table_info(media)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    if "content_hash" not in cols:
        await conn.execute("ALTER TABLE media ADD COLUMN content_hash TEXT")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_media_hash ON media (content_hash)")
    await conn.commit()


async def _migrate_v7_media_time(conn: aiosqlite.Connection) -> None:
    """media gained the message timestamp in v7 (time column)."""
    async with conn.execute("PRAGMA table_info(media)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    if "time" not in cols:
        await conn.execute("ALTER TABLE media ADD COLUMN time INTEGER")
        await conn.commit()


_V8_CONVERSATION_COLUMNS = (
    ("bf_cursor_seq", "ALTER TABLE conversations ADD COLUMN bf_cursor_seq INTEGER"),
    ("bf_target_seq", "ALTER TABLE conversations ADD COLUMN bf_target_seq INTEGER"),
    ("bf_last_run_at", "ALTER TABLE conversations ADD COLUMN bf_last_run_at REAL"),
    ("last_op_at", "ALTER TABLE conversations ADD COLUMN last_op_at REAL"),
)


async def _migrate_v8_bf_watermarks(conn: aiosqlite.Connection) -> None:
    """v8: conversations gained multi-watermark backfill state + op timestamps.

    bf_cursor_seq/bf_target_seq let an interrupted (or page-capped, or
    crash-killed) pull resume exactly where it stopped instead of leaving a
    silent hole below last_seq that only /sync force could recover.
    """
    async with conn.execute("PRAGMA table_info(conversations)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    changed = False
    for col, ddl in _V8_CONVERSATION_COLUMNS:
        if col not in cols:
            await conn.execute(ddl)
            changed = True
    if changed:
        await conn.commit()


async def _migrate_v9_bf_since(conn: aiosqlite.Connection) -> None:
    """v9: conversations gained bf_since_ts for time-bounded pulls (/sync 2d)."""
    async with conn.execute("PRAGMA table_info(conversations)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    if "bf_since_ts" not in cols:
        await conn.execute("ALTER TABLE conversations ADD COLUMN bf_since_ts REAL")
        await conn.commit()


async def _migrate_v10_ai_sessions(conn: aiosqlite.Connection) -> None:
    """v10: agent sessions are persisted (survive restart / resumable)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_sessions (
            id            TEXT PRIMARY KEY,
            chat_key      TEXT NOT NULL,
            prompt        TEXT NOT NULL,
            title         TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'running',
            step          INTEGER NOT NULL DEFAULT 0,
            created       REAL NOT NULL,
            updated       REAL NOT NULL,
            result_preview TEXT NOT NULL DEFAULT '',
            messages_json TEXT NOT NULL DEFAULT '[]',
            events_json   TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_sessions_chat ON ai_sessions (chat_key, updated)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_sessions_status ON ai_sessions (status, updated)"
    )
    await conn.commit()


async def _migrate_v11_rag(conn: aiosqlite.Connection) -> None:
    """v11: identity profiles + RAG doc/state tables (vectors via sqlite-vec)."""
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            subject    TEXT PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT '',
            content    TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rag_docs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,
            ref        TEXT NOT NULL,
            scene      TEXT,
            peer_id    INTEGER,
            time_start INTEGER,
            time_end   INTEGER,
            text       TEXT NOT NULL,
            model      TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            UNIQUE (kind, ref)
        );
        CREATE INDEX IF NOT EXISTS idx_rag_docs_kind ON rag_docs (kind);
        CREATE INDEX IF NOT EXISTS idx_rag_docs_conv ON rag_docs (scene, peer_id);
        CREATE TABLE IF NOT EXISTS rag_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    await conn.commit()


async def migrate(conn: aiosqlite.Connection) -> None:
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'") as cur:
        has_meta = await cur.fetchone()
    if not has_meta:
        await conn.executescript(SCHEMA_V1)
        await _migrate_v8_bf_watermarks(conn)
        await _migrate_v9_bf_since(conn)
        await _migrate_v10_ai_sessions(conn)
        await _migrate_v11_rag(conn)
        await _migrate_v12_value(conn)
        await _migrate_v13_media_asr_ts(conn)
        await _migrate_v14_tasks_lease(conn)
        await _migrate_v15_followups(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '15')")
        await conn.commit()
        log.info("database created (schema v15)")
        return
    async with conn.execute("SELECT value FROM meta WHERE key='schema_version'") as cur:
        row = await cur.fetchone()
    version = int(row[0]) if row else 0
    if version < 7:
        await conn.executescript(SCHEMA_V1)  # CREATE IF NOT EXISTS: applies missing tables
        await _migrate_v5_add_period(conn)
        await _migrate_v6_media_hash(conn)
        await _migrate_v7_media_time(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '7')")
        await conn.commit()
        log.info("database migrated to schema v7")
        version = 7
    if version < 8:
        await _migrate_v8_bf_watermarks(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '8')")
        await conn.commit()
        log.info("database migrated to schema v8 (backfill resume watermarks)")
        version = 8
    if version < 9:
        await _migrate_v9_bf_since(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '9')")
        await conn.commit()
        log.info("database migrated to schema v9 (time-bounded backfill)")
        version = 9
    if version < 10:
        await _migrate_v10_ai_sessions(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '10')")
        await conn.commit()
        log.info("database migrated to schema v10 (persistent agent sessions)")
        version = 10
    if version < 11:
        await _migrate_v11_rag(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '11')")
        await conn.commit()
        log.info("database migrated to schema v11 (profiles + RAG)")
        version = 11
    if version < 12:
        await _migrate_v12_value(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '12')")
        await conn.commit()
        log.info("database migrated to schema v12 (message value scores)")
        version = 12
    if version < 13:
        await _migrate_v13_media_asr_ts(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '13')")
        await conn.commit()
        log.info("database migrated to schema v13 (media asr timestamps)")
        version = 13
    if version < 14:
        await _migrate_v14_tasks_lease(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '14')")
        await conn.commit()
        log.info("database migrated to schema v14 (scheduled task claim leases)")
        version = 14
    if version < 15:
        await _migrate_v15_followups(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '15')")
        await conn.commit()
        log.info("database migrated to schema v15 (structured follow-up tracking)")
        version = 15


async def _migrate_v15_followups(conn: aiosqlite.Connection) -> None:
    """followups: structured TODO state distilled from dream reports.

    Pending items used to live only as prose inside past digests, and the
    prompt forced the LLM to re-derive their status from each day's
    (budget-sampled) transcript — an item whose completion signal never
    appeared in QQ stayed "仍悬置" forever (user feedback 2026-10-24).
    The table is the single source of truth now: the dream syncs it, the
    user can close items explicitly (/done, followup_close), and stale
    items auto-expire."""
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS followups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            subject     TEXT NOT NULL DEFAULT '',
            title       TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'open',
            opened_date TEXT NOT NULL DEFAULT '',
            last_date   TEXT NOT NULL DEFAULT '',
            closed_date TEXT NOT NULL DEFAULT '',
            close_source TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_followups_status ON followups (status, updated_at);
        """
    )
    await conn.commit()


async def _migrate_v14_tasks_lease(conn: aiosqlite.Connection) -> None:
    """scheduled_tasks.lease_until + 'running' status: task lifetime is
    decoupled from the serve process via a claim/lease protocol (a restart
    re-runs interrupted tasks from scratch instead of losing them)."""
    async with conn.execute("PRAGMA table_info(scheduled_tasks)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if not cols or "lease_until" in cols:
        return
    await conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN lease_until INTEGER")


async def _migrate_v13_media_asr_ts(conn: aiosqlite.Connection) -> None:
    """media.asr_ts: JSON [[word, start, end], ...] from Qwen3-ForcedAligner."""
    async with conn.execute("PRAGMA table_info(media)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if not cols or "asr_ts" in cols:
        return
    await conn.execute("ALTER TABLE media ADD COLUMN asr_ts TEXT")


async def _migrate_v12_value(conn: aiosqlite.Connection) -> None:
    """messages.value: NULL = unjudged, 0..3 = cheap-model value score
    (0 noise, 1 small talk, 2 informational, 3 important)."""
    async with conn.execute("PRAGMA table_info(messages)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if not cols or "value" in cols:
        # a synthetic/partial old DB may lack messages entirely; other
        # migrations create it with the column via SCHEMA_V1
        return
    await conn.execute("ALTER TABLE messages ADD COLUMN value INTEGER")
    await conn.commit()
