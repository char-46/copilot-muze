"""Data access layer over the message archive."""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

import aiosqlite

from muze.provider.milky.models import IncomingMessage


def _now() -> int:
    return int(time.time())


class Repository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------ owner binding
    async def bind_owner(self, uin: int) -> int | None:
        """Bind this database to one QQ account (multi-account isolation).

        First call records the owner and returns None. Later calls return
        the conflicting owner uin when a different account tries to use the
        same data dir — the caller must refuse to start in that case.
        """
        async with self.conn.execute(
            "SELECT value FROM meta WHERE key='owner_uin'"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('owner_uin', ?)", (str(int(uin)),)
            )
            await self.conn.commit()
            return None
        stored = int(row[0])
        if stored == int(uin):
            return None
        return stored

    async def owner_uin(self) -> int | None:
        async with self.conn.execute(
            "SELECT value FROM meta WHERE key='owner_uin'"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    # ------------------------------------------------------------ messages
    async def upsert_message(
        self,
        msg: IncomingMessage,
        text_content: str,
        source: str,
        conv_name: str = "",
    ) -> bool:
        """Insert a message; returns True when it was new (not a duplicate).

        conv_name is the conversation's display name (group name / friend
        remark), stored on conversations.name when non-empty."""
        raw = msg.model_dump(mode="json", exclude_none=True)
        cur = await self.conn.execute(
            """
            INSERT INTO messages
                (scene, peer_id, message_seq, sender_id, time, raw_json,
                 text_content, source, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (scene, peer_id, message_seq) DO NOTHING
            """,
            (
                msg.message_scene,
                msg.peer_id,
                msg.message_seq,
                msg.sender_id,
                msg.time or _now(),  # timestamp; fallback if the protocol sent 0
                json.dumps(raw, ensure_ascii=False),
                text_content,
                source,
                _now(),
            ),
        )
        inserted = cur.rowcount > 0
        if inserted:
            await self._touch_conversation(msg, conv_name)
        await self.conn.commit()
        return inserted

    async def _touch_conversation(self, msg: IncomingMessage, name: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO conversations (scene, peer_id, name, last_seq, last_active_at, last_op_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (scene, peer_id) DO UPDATE SET
                last_seq = MAX(last_seq, excluded.last_seq),
                last_active_at = MAX(COALESCE(last_active_at, 0), excluded.last_active_at),
                last_op_at = excluded.last_op_at,
                name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END
            """,
            (msg.message_scene, msg.peer_id, name, msg.message_seq, msg.time, _now()),
        )

    async def set_recalled(self, scene: str, peer_id: int, message_seq: int) -> bool:
        cur = await self.conn.execute(
            "UPDATE messages SET recalled = 1 WHERE scene = ? AND peer_id = ? AND message_seq = ?",
            (scene, peer_id, message_seq),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_watermark(self, scene: str, peer_id: int) -> int:
        async with self.conn.execute(
            "SELECT last_seq FROM conversations WHERE scene = ? AND peer_id = ?",
            (scene, peer_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------------------------------------------- backfill resume state
    async def get_bf_state(self, scene: str, peer_id: int) -> dict:
        """Multi-watermark pull state: cursor (where an unfinished deep pull
        stopped), target (its seq lower bound) and since (its time lower
        bound, for `/sync 2d`-style pulls). Empty dict when none pending."""
        async with self.conn.execute(
            "SELECT bf_cursor_seq, bf_target_seq, bf_since_ts FROM conversations "
            "WHERE scene = ? AND peer_id = ?",
            (scene, peer_id),
        ) as cur:
            row = await cur.fetchone()
        if not row or (row["bf_target_seq"] is None and row["bf_since_ts"] is None):
            return {}
        return {
            "cursor": row["bf_cursor_seq"],
            "target": row["bf_target_seq"],
            "since": row["bf_since_ts"],
        }

    async def set_bf_state(
        self,
        scene: str,
        peer_id: int,
        cursor: int | None,
        target: int | None,
        since: float | None = None,
    ) -> None:
        """Persist resume progress (called after every completed page so a
        crash never loses more than one page of work)."""
        await self.conn.execute(
            """
            INSERT INTO conversations
                (scene, peer_id, bf_cursor_seq, bf_target_seq, bf_since_ts, bf_last_run_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (scene, peer_id) DO UPDATE SET
                bf_cursor_seq = excluded.bf_cursor_seq,
                bf_target_seq = excluded.bf_target_seq,
                bf_since_ts = excluded.bf_since_ts,
                bf_last_run_at = excluded.bf_last_run_at
            """,
            (scene, peer_id, cursor, target, since, _now()),
        )
        await self.conn.commit()

    async def clear_bf_state(self, scene: str, peer_id: int) -> None:
        await self.conn.execute(
            "UPDATE conversations SET bf_cursor_seq = NULL, bf_target_seq = NULL, "
            "bf_since_ts = NULL, bf_last_run_at = ? WHERE scene = ? AND peer_id = ?",
            (_now(), scene, peer_id),
        )
        await self.conn.commit()

    async def pending_bf_conversations(self) -> list[tuple[str, int]]:
        """Conversations with an unfinished (hole-filling) pull pending."""
        async with self.conn.execute(
            "SELECT scene, peer_id FROM conversations "
            "WHERE bf_target_seq IS NOT NULL OR bf_since_ts IS NOT NULL"
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    # ------------------------------------------------------------ meta kv
    async def get_meta(self, key: str) -> str | None:
        async with self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def get_message_row(self, scene: str, peer_id: int, message_seq: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE scene = ? AND peer_id = ? AND message_seq = ?",
            (scene, peer_id, message_seq),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_messages(
        self,
        scene: str,
        peer_id: int,
        before_seq: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Newest-first window; pass before_seq to page backwards in time."""
        limit = max(1, min(int(limit), 200))
        if before_seq is not None:
            sql = (
                "SELECT * FROM messages WHERE scene=? AND peer_id=? AND message_seq<? "
                "ORDER BY message_seq DESC LIMIT ?"
            )
            args: Iterable[Any] = (scene, peer_id, before_seq, limit)
        else:
            sql = (
                "SELECT * FROM messages WHERE scene=? AND peer_id=? "
                "ORDER BY message_seq DESC LIMIT ?"
            )
            args = (scene, peer_id, limit)
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_messages(
        self,
        query: str,
        scene: str | None = None,
        peer_id: int | None = None,
        limit: int = 30,
    ) -> list[dict]:
        esc = query.replace("/", "//").replace("%", "/%").replace("_", "/_")
        pattern = f"%{esc}%"
        sql = "SELECT * FROM messages WHERE text_content LIKE ? ESCAPE '/'"
        args: list[Any] = [pattern]
        if scene:
            sql += " AND scene = ?"
            args.append(scene)
        if peer_id is not None:
            sql += " AND peer_id = ?"
            args.append(peer_id)
        sql += " ORDER BY time DESC, message_seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 100)))
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- conversations
    async def list_conversations(
        self, active_within_h: float | None = None, limit: int = 200
    ) -> list[dict]:
        sql = "SELECT * FROM conversations"
        args: list[Any] = []
        # a row may exist only to hold backfill resume state (no messages yet):
        # those are surfaced via pending_bf_conversations(), not as chats
        sql += " WHERE last_seq > 0"
        if active_within_h:
            sql += " AND last_active_at >= ?"
            args.append(int(time.time()) - int(active_within_h * 3600))
        sql += " ORDER BY last_active_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            async with self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE scene=? AND peer_id=?",
                (d["scene"], d["peer_id"]),
            ) as cur:
                d["message_count"] = (await cur.fetchone())[0]
            out.append(d)
        return out

    async def get_conversation(self, scene: str, peer_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM conversations WHERE scene=? AND peer_id=?", (scene, peer_id)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # --------------------------------------------------------------- media
    async def add_media(self, row: dict) -> int | None:
        row = {
            "file_path": None,
            "file_size": None,
            "content_hash": None,
            "time": None,
            **row,
        }
        cur = await self.conn.execute(
            """
            INSERT INTO media
                (scene, peer_id, message_seq, seg_index, kind, resource_id, file_id,
                 file_name, file_hash, width, height, duration, summary,
                 status, file_path, file_size, content_hash, time, created_at, updated_at)
            VALUES (:scene, :peer_id, :message_seq, :seg_index, :kind, :resource_id,
                    :file_id, :file_name, :file_hash, :width, :height, :duration,
                    :summary, :status, :file_path, :file_size, :content_hash, :time,
                    :created_at, :updated_at)
            ON CONFLICT (scene, peer_id, message_seq, seg_index) DO NOTHING
            """,
            {
                **row,
                "created_at": _now(),
                "updated_at": _now(),
            },
        )
        await self.conn.commit()
        if cur.rowcount > 0:
            async with self.conn.execute("SELECT last_insert_rowid()") as c2:
                return (await c2.fetchone())[0]
        return None

    async def update_media(
        self, media_id: int, *, status: str, file_path: str | None = None,
        file_size: int | None = None, error: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE media SET status=?, file_path=COALESCE(?, file_path),
                             file_size=COALESCE(?, file_size),
                             content_hash=COALESCE(?, content_hash),
                             error=?, updated_at=?
            WHERE id=?
            """,
            (status, file_path, file_size, content_hash, error, _now(), media_id),
        )
        await self.conn.commit()

    async def media_by_id(self, media_id: int) -> dict | None:
        async with self.conn.execute("SELECT * FROM media WHERE id=?", (media_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def media_for_message(self, scene: str, peer_id: int, message_seq: int) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM media WHERE scene=? AND peer_id=? AND message_seq=? ORDER BY seg_index",
            (scene, peer_id, message_seq),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def media_pending(self, limit: int = 100) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM media WHERE status IN ('pending','failed') ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def retire_gone_media(self) -> int:
        """One-shot: historical failed rows whose error says the resource is
        gone (HTTP 404/410) become status='gone' so nothing retries them."""
        cur = await self.conn.execute(
            """
            UPDATE media SET status='gone', updated_at=?
            WHERE status='failed'
              AND (error LIKE '%HTTP 404%' OR error LIKE '%HTTP 410%'
                   OR (error LIKE '%url:%' AND error LIKE '%expired%'))
            """,
            (_now(),),
        )
        await self.conn.commit()
        return cur.rowcount

    async def media_stats(self) -> dict:
        out: dict[str, int] = {}
        for status in ("done", "failed", "pending", "skipped", "gone"):
            async with self.conn.execute(
                "SELECT COUNT(*) FROM media WHERE status=?", (status,)
            ) as cur:
                out[status] = (await cur.fetchone())[0]
        async with self.conn.execute(
            "SELECT COALESCE(SUM(file_size),0) FROM media WHERE status='done'"
        ) as cur:
            out["bytes"] = (await cur.fetchone())[0]
        return out

    # ------------------------------------------------------ pending actions
    async def create_action(
        self,
        kind: str,
        scene: str,
        peer_id: int,
        payload: dict,
        control_scene: str,
        control_peer_id: int,
        created_by: str,
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO pending_actions
                (kind, scene, peer_id, payload_json, control_scene, control_peer_id,
                 status, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                kind,
                scene,
                peer_id,
                json.dumps(payload, ensure_ascii=False),
                control_scene,
                control_peer_id,
                created_by,
                _now(),
                _now(),
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def get_action(self, action_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM pending_actions WHERE id=?", (action_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_pending_actions(self, scene: str | None = None, peer_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM pending_actions WHERE status='pending'"
        args: list[Any] = []
        if scene:
            sql += " AND control_scene=?"
            args.append(scene)
        if peer_id is not None:
            sql += " AND control_peer_id=?"
            args.append(peer_id)
        sql += " ORDER BY id"
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def set_action_status(self, action_id: int, status: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE pending_actions SET status=?, updated_at=? WHERE id=? AND status='pending'",
            (status, _now(), action_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def expire_actions(self, older_than_min: float) -> int:
        cur = await self.conn.execute(
            """
            UPDATE pending_actions SET status='expired', updated_at=?
            WHERE status='pending' AND created_at < ?
            """,
            (_now(), _now() - int(older_than_min * 60)),
        )
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------ scheduled tasks
    async def create_task(
        self,
        kind: str,
        prompt: str,
        control_scene: str,
        control_peer_id: int,
        next_run_at: int,
        interval_sec: int | None,
        created_by: str = "",
        note: str = "",
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO scheduled_tasks
                (kind, prompt, control_scene, control_peer_id, next_run_at,
                 interval_sec, status, created_by, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (kind, prompt, control_scene, control_peer_id, next_run_at,
             interval_sec, created_by, note, _now(), _now()),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def due_tasks(self, now_ts: int | None = None) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE status='active' AND next_run_at <= ? ORDER BY next_run_at",
            (now_ts or _now(),),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_tasks(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM scheduled_tasks"
        if active_only:
            sql += " WHERE status='active'"
        sql += " ORDER BY next_run_at DESC LIMIT 100"
        async with self.conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_task(self, task_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def finish_task(self, task_id: int, status: str, last_result: str = "",
                          next_run_at: int | None = None) -> None:
        await self.conn.execute(
            """
            UPDATE scheduled_tasks
            SET status=?, last_result=?, updated_at=?,
                next_run_at=COALESCE(?, next_run_at)
            WHERE id=?
            """,
            (status, last_result[:1000], _now(), next_run_at, task_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------ digests
    async def upsert_digest(
        self, date: str, report: str, window_hours: float, stats: dict,
        period: str = "day",
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO daily_digests (date, period, created_at, window_hours, report, stats_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (date) DO UPDATE SET
                created_at=excluded.created_at, window_hours=excluded.window_hours,
                report=excluded.report, stats_json=excluded.stats_json
            """,
            (date, period, _now(), window_hours, report, json.dumps(stats, ensure_ascii=False)),
        )
        await self.conn.commit()

    async def get_digest(self, date: str) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM daily_digests WHERE date=?", (date,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_digests(
        self, limit: int = 30, period: str | None = None
    ) -> list[dict]:
        if period:
            sql = (
                "SELECT date, period, created_at, window_hours, length(report) AS report_len "
                "FROM daily_digests WHERE period=? ORDER BY date DESC LIMIT ?"
            )
            args: list = [period, limit]
        else:
            sql = (
                "SELECT date, period, created_at, window_hours, length(report) AS report_len "
                "FROM daily_digests ORDER BY date DESC LIMIT ?"
            )
            args = [limit]
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def latest_digest(self, period: str = "day") -> dict | None:
        rows = await self.list_digests(limit=1, period=period)
        return rows[0] if rows else None

    async def earliest_day_digest_date(self) -> str | None:
        async with self.conn.execute(
            "SELECT MIN(date) FROM daily_digests WHERE period='day'"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row and row[0] else None

    async def conversations_active_between(
        self, start_ts: int, end_ts: int, limit: int = 6
    ) -> list[dict]:
        """Busiest conversations whose messages fall in [start_ts, end_ts)."""
        async with self.conn.execute(
            """
            SELECT scene, peer_id, COUNT(*) AS cnt
            FROM messages
            WHERE time >= ? AND time < ? AND recalled = 0
            GROUP BY scene, peer_id
            ORDER BY cnt DESC LIMIT ?
            """,
            (start_ts, end_ts, limit),
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            conv = await self.get_conversation(r["scene"], r["peer_id"])
            d["name"] = (conv or {}).get("name", "")
            out.append(d)
        return out

    async def messages_between(
        self, scene: str, peer_id: int, start_ts: int, end_ts: int, limit: int = 50
    ) -> list[dict]:
        """Newest-first window within [start_ts, end_ts); returns chronological."""
        async with self.conn.execute(
            """
            SELECT * FROM messages
            WHERE scene=? AND peer_id=? AND time >= ? AND time < ? AND recalled = 0
            ORDER BY time DESC, message_seq DESC LIMIT ?
            """,
            (scene, peer_id, start_ts, end_ts, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def coverage_stats(self, scene: str, peer_id: int) -> dict:
        """Local archive coverage for one conversation."""
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS n, MIN(message_seq) AS min_seq, MAX(message_seq) AS max_seq,
                   MIN(time) AS min_time, MAX(time) AS max_time
            FROM messages WHERE scene=? AND peer_id=?
            """,
            (scene, peer_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def day_counts(self, start_ts: int, end_ts: int, limit: int = 40) -> list[dict]:
        """Per-conversation message counts in a window, busiest first."""
        async with self.conn.execute(
            """
            SELECT m.scene, m.peer_id, COUNT(*) AS n, c.name AS name
            FROM messages m
            LEFT JOIN conversations c ON c.scene = m.scene AND c.peer_id = m.peer_id
            WHERE m.time >= ? AND m.time < ?
            GROUP BY m.scene, m.peer_id
            ORDER BY n DESC LIMIT ?
            """,
            (start_ts, end_ts, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def top_conversations(self, since_ts: int, limit: int = 15) -> list[dict]:
        """Lifetime message counts since a timestamp, busiest first."""
        async with self.conn.execute(
            """
            SELECT m.scene, m.peer_id, COUNT(*) AS n, c.name AS name
            FROM messages m
            LEFT JOIN conversations c ON c.scene = m.scene AND c.peer_id = m.peer_id
            WHERE m.time >= ?
            GROUP BY m.scene, m.peer_id
            ORDER BY n DESC LIMIT ?
            """,
            (since_ts, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def sender_counts(
        self, start_ts: int, end_ts: int, scene: str, peer_id: int, limit: int = 20
    ) -> list[dict]:
        async with self.conn.execute(
            """
            SELECT sender_id, COUNT(*) AS n FROM messages
            WHERE time >= ? AND time < ? AND scene = ? AND peer_id = ?
            GROUP BY sender_id ORDER BY n DESC LIMIT ?
            """,
            (start_ts, end_ts, scene, peer_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- memories
    async def add_memory(
        self, kind: str, subject: str, content: str, source: str = "",
        confidence: float = 1.0,
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO memories (kind, subject, content, source, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, subject, content, source, confidence, _now(), _now()),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def list_memories(
        self, subject: str | None = None, active_only: bool = True, limit: int = 200
    ) -> list[dict]:
        sql = "SELECT * FROM memories"
        conds, args = [], []
        if active_only:
            conds.append("active=1")
        if subject:
            conds.append("subject=?")
            args.append(subject)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_memories(self, query: str, subject: str | None = None, limit: int = 30) -> list[dict]:
        sql = "SELECT * FROM memories WHERE active=1 AND (content LIKE ? OR subject LIKE ?)"
        args: list = [f"%{query}%", f"%{query}%"]
        if subject:
            sql += " AND subject=?"
            args.append(subject)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_memory(self, memory_id: int, content: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE memories SET content=?, updated_at=? WHERE id=? AND active=1",
            (content, _now(), memory_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def retire_memory(self, memory_id: int) -> bool:
        cur = await self.conn.execute(
            "UPDATE memories SET active=0, updated_at=? WHERE id=? AND active=1",
            (_now(), memory_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------- ai sessions
    async def upsert_ai_session(self, row: dict) -> None:
        """Insert or replace one agent session (id-keyed)."""
        await self.conn.execute(
            """
            INSERT INTO ai_sessions
                (id, chat_key, prompt, title, status, step, created, updated,
                 result_preview, messages_json, events_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                chat_key=excluded.chat_key, prompt=excluded.prompt,
                title=excluded.title, status=excluded.status, step=excluded.step,
                created=excluded.created, updated=excluded.updated,
                result_preview=excluded.result_preview,
                messages_json=excluded.messages_json,
                events_json=excluded.events_json
            """,
            (
                row["id"], row["chat_key"], row["prompt"], row.get("title", ""),
                row.get("status", "running"), int(row.get("step", 0)),
                float(row["created"]), float(row["updated"]),
                row.get("result_preview", ""),
                row.get("messages_json", "[]"), row.get("events_json", "[]"),
            ),
        )
        await self.conn.commit()

    async def get_ai_session(self, sid: str) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM ai_sessions WHERE id=?", (sid,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_ai_sessions(
        self, limit: int = 50, chat_key: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM ai_sessions"
        args: list = []
        if chat_key:
            sql += " WHERE chat_key=?"
            args.append(chat_key)
        sql += " ORDER BY updated DESC LIMIT ?"
        args.append(max(1, int(limit)))
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_ai_session(self, sid: str) -> None:
        await self.conn.execute("DELETE FROM ai_sessions WHERE id=?", (sid,))
        await self.conn.commit()

    # --------------------------------------------------------- profiles
    async def upsert_profile(self, subject: str, name: str, content: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO profiles (subject, name, content, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subject) DO UPDATE SET
                name=excluded.name, content=excluded.content,
                updated_at=excluded.updated_at
            """,
            (subject, name or "", content or "", time.time()),
        )
        await self.conn.commit()

    async def get_profile(self, subject: str) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM profiles WHERE subject=?", (subject,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_profiles(self, limit: int = 300) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM profiles ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- rag
    async def upsert_rag_doc(
        self,
        kind: str,
        ref: str,
        text: str,
        *,
        scene: str | None = None,
        peer_id: int | None = None,
        time_start: int | None = None,
        time_end: int | None = None,
        model: str = "",
    ) -> int:
        """Insert/replace one RAG document (kind+ref keyed); returns its id."""
        await self.conn.execute(
            """
            INSERT INTO rag_docs
                (kind, ref, scene, peer_id, time_start, time_end, text, model, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, ref) DO UPDATE SET
                scene=excluded.scene, peer_id=excluded.peer_id,
                time_start=excluded.time_start, time_end=excluded.time_end,
                text=excluded.text, model=excluded.model, updated_at=excluded.updated_at
            """,
            (kind, ref, scene, peer_id, time_start, time_end, text, model, time.time()),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM rag_docs WHERE kind=? AND ref=?", (kind, ref)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0])

    async def get_rag_doc(self, kind: str, ref: str) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM rag_docs WHERE kind=? AND ref=?", (kind, ref)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def rag_docs_by_ids(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        async with self.conn.execute(
            f"SELECT * FROM rag_docs WHERE id IN ({marks})", ids
        ) as cur:
            rows = await cur.fetchall()
        return {int(r["id"]): dict(r) for r in rows}

    async def rag_doc_counts(self) -> dict[str, int]:
        """rag_docs row count per kind (for /status and the webui)."""
        async with self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM rag_docs GROUP BY kind"
        ) as cur:
            return {r["kind"]: int(r["n"]) for r in await cur.fetchall()}

    async def rag_chat_progress(self, limit: int = 8) -> dict:
        """Chat-indexing progress.

        Seq cursors are POSITIONS, not counts: a conversation holding a
        sparse slice of its seq space (backfill pulled recent pages only)
        jumps its cursor by millions while covering few messages. So the
        authoritative metric is message COVERAGE — archived messages whose
        seq is <= the conversation's cursor (i.e. already folded into
        windows); the per-conversation cursor/last_seq lines are just a
        catch-up view against the newest known seq."""
        async with self.conn.execute(
            "SELECT c.scene, c.peer_id, c.last_seq, c.last_active_at, "
            " CAST(COALESCE((SELECT value FROM rag_state "
            "  WHERE key='chat:'||c.scene||':'||c.peer_id), '0') AS INTEGER) AS cursor "
            "FROM conversations c WHERE c.last_seq > 0 "
            "ORDER BY c.last_active_at DESC LIMIT ?",
            (max(1, min(int(limit), 50)),),
        ) as cur:
            convs = [dict(r) for r in await cur.fetchall()]
        async with self.conn.execute(
            "SELECT COUNT(*) AS covered, (SELECT COUNT(*) FROM messages) AS total "
            "FROM messages m WHERE m.message_seq <= CAST(COALESCE("
            " (SELECT value FROM rag_state "
            "  WHERE key='chat:'||m.scene||':'||m.peer_id), '0') AS INTEGER)"
        ) as cur:
            cov = dict(await cur.fetchone())
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE last_seq > 0"
        ) as cur:
            n = int((await cur.fetchone())[0])
        return {
            "conversations": convs,
            "total_conversations": n,
            "messages_covered": int(cov["covered"] or 0),
            "messages_total": int(cov["total"] or 0),
        }

    async def list_rag_docs(self, kind: str | None = None, limit: int = 1000) -> list[dict]:
        sql = "SELECT * FROM rag_docs"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def rag_get_state(self, key: str) -> str | None:
        async with self.conn.execute(
            "SELECT value FROM rag_state WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def rag_set_state(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO rag_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def messages_after(
        self, scene: str, peer_id: int, seq: int, limit: int = 200
    ) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE scene=? AND peer_id=? AND message_seq>? "
            "ORDER BY message_seq ASC LIMIT ?",
            (scene, peer_id, int(seq), max(1, int(limit))),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- stats
    async def stats(self) -> dict:
        out: dict[str, Any] = {}
        async with self.conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE last_seq > 0"
        ) as cur:
            out["conversations"] = (await cur.fetchone())[0]
        async with self.conn.execute("SELECT COUNT(*) FROM messages") as cur:
            out["messages"] = (await cur.fetchone())[0]
        async with self.conn.execute(
            "SELECT scene, COUNT(*) FROM messages GROUP BY scene"
        ) as cur:
            out["messages_by_scene"] = {r[0]: r[1] for r in await cur.fetchall()}
        async with self.conn.execute(
            "SELECT MIN(time), MAX(time) FROM messages"
        ) as cur:
            row = await cur.fetchone()
        out["oldest_ts"], out["newest_ts"] = (row[0], row[1]) if row else (None, None)
        out["media"] = await self.media_stats()
        out["pending_actions"] = len(await self.list_pending_actions())
        return out
