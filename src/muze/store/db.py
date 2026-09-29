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
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE (scene, peer_id, message_seq, seg_index)
);
CREATE INDEX IF NOT EXISTS idx_media_status ON media (status);

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
    date         TEXT PRIMARY KEY,        -- local YYYY-MM-DD
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


async def migrate(conn: aiosqlite.Connection) -> None:
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'") as cur:
        has_meta = await cur.fetchone()
    if not has_meta:
        await conn.executescript(SCHEMA_V1)
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')")
        await conn.commit()
        log.info("database created (schema v4)")
        return
    async with conn.execute("SELECT value FROM meta WHERE key='schema_version'") as cur:
        row = await cur.fetchone()
    version = int(row[0]) if row else 0
    if version < 4:
        await conn.executescript(SCHEMA_V1)  # CREATE IF NOT EXISTS: applies missing tables
        await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')")
        await conn.commit()
        log.info("database migrated to schema v4")
