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
    status          TEXT NOT NULL DEFAULT 'active',  -- active|done|cancelled|error
    created_by      TEXT NOT NULL DEFAULT '',
    note            TEXT,
    last_result     TEXT,
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
"""


async def open_db(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys=ON")
    await migrate(conn)
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


async def migrate(conn: aiosqlite.Connection) -> None:
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'") as cur:
        has_meta = await cur.fetchone()
    if not has_meta:
        await conn.executescript(SCHEMA_V1)
        await _migrate_v8_bf_watermarks(conn)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '8')")
        await conn.commit()
        log.info("database created (schema v8)")
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
