"""Vector retrieval (RAG).

Two corpora:
- **summaries** (always): identity profiles, long-term memories, dream digests;
- **chat windows** (opt-in, ``rag.chat``): consecutive archived messages.

Vectors live in a ``sqlite-vec`` virtual table (``rag_vec``); documents and
their metadata live in ``rag_docs``. Retrieval is metadata-filtered KNN.

Indexing is **idle-scheduled and interruptible**: it only runs when the
resource governor says the machine is quiet (AC power, low CPU/GPU, no recent
activity) and stops promptly when that stops being true, keeping per-
conversation cursors so the next pass resumes. One embedding model id == one
vector space, so changing the model rebuilds the index.
"""

from __future__ import annotations

import logging
import time

from muze.ai.embeddings import Embedder
from muze.store.repo import Repository

log = logging.getLogger("muze.rag")


class VecStore:
    """Thin wrapper over the sqlite-vec virtual table (+ model guard)."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.conn = repo.conn
        self.available = False
        self.dims = 0
        self.model_id = ""

    async def setup(self) -> bool:
        try:
            import sqlite_vec  # noqa: F401

            self.available = True
        except Exception:  # pragma: no cover - env dependent
            self.available = False
        return self.available

    async def _table_exists(self) -> bool:
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rag_vec'"
        ) as cur:
            return await cur.fetchone() is not None

    async def ensure(self, dims: int, model_id: str) -> None:
        if not self.available or dims <= 0:
            return
        cur_dims = int(await self.repo.rag_get_state("vec_dims") or 0)
        cur_model = await self.repo.rag_get_state("vec_model") or ""
        if cur_dims == dims and cur_model == model_id and await self._table_exists():
            self.dims, self.model_id = dims, model_id
            return
        # model or dimension changed -> the old vectors are meaningless
        await self.conn.execute("DROP TABLE IF EXISTS rag_vec")
        await self.conn.execute(
            f"CREATE VIRTUAL TABLE rag_vec USING vec0(embedding float[{int(dims)}])"
        )
        log.warning(
            "RAG index rebuilt (model %s->%s dims %s->%s)",
            cur_model or "-", model_id, cur_dims or "-", dims,
        )
        await self.conn.execute("DELETE FROM rag_docs")
        await self.conn.execute("DELETE FROM rag_state WHERE key LIKE 'chat:%'")
        await self.conn.commit()
        await self.repo.rag_set_state("vec_dims", str(int(dims)))
        await self.repo.rag_set_state("vec_model", model_id)
        self.dims, self.model_id = dims, model_id

    async def upsert(self, rowid: int, vector: list[float]) -> None:
        if not self.available or self.dims <= 0:
            return
        import sqlite_vec

        blob = sqlite_vec.serialize_float32([float(x) for x in vector])
        await self.conn.execute("DELETE FROM rag_vec WHERE rowid=?", (rowid,))
        await self.conn.execute(
            "INSERT INTO rag_vec(rowid, embedding) VALUES (?, ?)", (rowid, blob)
        )

    async def search(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        if not self.available or self.dims <= 0:
            return []
        import sqlite_vec

        blob = sqlite_vec.serialize_float32([float(x) for x in vector])
        async with self.conn.execute(
            "SELECT rowid, distance FROM rag_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (blob, max(1, int(k))),
        ) as cur:
            rows = await cur.fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]


class RagIndexer:
    def __init__(
        self,
        repo: Repository,
        vec: VecStore,
        embedder: Embedder,
        cfg,
        governor=None,
        activity_fn=None,
    ) -> None:
        self.repo = repo
        self.vec = vec
        self.embedder = embedder
        self.cfg = cfg
        self.governor = governor
        self.activity_fn = activity_fn or (lambda: 0.0)
        self.running = False
        self.last_index_at = 0.0
        self.last_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.rag.enabled and self.embedder.available and self.vec.available)

    # ----------------------------------------------------------- scheduling
    def _idle(self) -> bool:
        if self.governor is None:
            return True
        try:
            return self.governor.idle(self.activity_fn())[0]
        except Exception:
            return True

    async def _prepare(self) -> None:
        try:
            await self.embedder.prepare(self.governor)
        except Exception:
            log.exception("embedder prepare failed")
        self.embedder.unload_if_idle()

    # ----------------------------------------------------------- indexing
    async def run_once(self) -> int:
        if not self.enabled or self.running:
            return 0
        if not self.embedder.healthy:
            log.info("RAG index skipped: embedder unavailable")
            return 0
        if not self._idle():
            log.debug("RAG index skipped: not idle")
            return 0
        self.running = True
        total = 0
        try:
            await self._prepare()
            total += await self._index_docs(await self._collect_summaries())
            if self.cfg.rag.chat and self._idle():
                total += await self._index_chat()
            self.last_index_at = time.time()
            self.last_count = total
            if total:
                log.info("RAG index: %s documents", total)
            return total
        except Exception:
            log.exception("RAG indexing failed")
            return total
        finally:
            self.running = False

    async def _index_docs(self, docs: list[dict]) -> int:
        if not docs:
            return 0
        cap = int(self.cfg.rag.max_doc_chars)
        batch = max(1, int(self.cfg.rag.embedding.batch))
        ensured = False
        done = 0
        for i in range(0, len(docs), batch):
            if not self._idle():
                log.info("RAG index interrupted after %s docs (busy)", done)
                break
            chunk = docs[i : i + batch]
            try:
                vecs = await self.embedder.embed([(d["text"] or "")[:cap] for d in chunk], query=False)
            except Exception as e:
                log.warning("RAG embed failed, stopping this pass: %s", e)
                break
            if not vecs:
                break
            if not ensured:
                await self.vec.ensure(len(vecs[0]), self.embedder.model_id)
                ensured = True
                if not self.vec.available or self.vec.dims <= 0:
                    return done
            for d, v in zip(chunk, vecs):
                rid = await self.repo.upsert_rag_doc(
                    d["kind"], d["ref"], d["text"],
                    scene=d.get("scene"), peer_id=d.get("peer_id"),
                    time_start=d.get("time_start"), time_end=d.get("time_end"),
                    model=self.embedder.model_id,
                )
                await self.vec.upsert(rid, v)
            await self.repo.conn.commit()
            done += len(chunk)
            await _sleep(self.cfg.rag.embedding.min_interval_sec)
        return done

    async def _collect_summaries(self) -> list[dict]:
        docs: list[dict] = []
        for p in await self.repo.list_profiles():
            docs.append({
                "kind": "profile", "ref": p["subject"],
                "text": f"身份档案 {p['subject']}（{p['name']}）\n{p['content']}",
            })
        for m in await self.repo.list_memories(active_only=True, limit=500):
            docs.append({
                "kind": "memory", "ref": f"memory:{m['id']}",
                "text": f"[{m['kind']}] {m['subject']}: {m['content']}",
            })
        for d in await self.repo.list_digests(limit=120):
            full = await self.repo.get_digest(d["date"])
            text = ((full or {}).get("report") or "").strip()
            if text:
                docs.append({"kind": "digest", "ref": f"digest:{d['date']}", "text": text})
        return docs

    async def _index_chat(self) -> int:
        wm = int(self.cfg.rag.window_messages)
        wc = int(self.cfg.rag.window_chars)
        done = 0
        for conv in await self.repo.list_conversations(limit=300):
            if not self._idle():
                break
            scene, peer = conv["scene"], conv["peer_id"]
            key = f"chat:{scene}:{peer}"
            cursor = int(await self.repo.rag_get_state(key) or 0)
            msgs = await self.repo.messages_after(scene, peer, cursor, limit=wm * 4)
            if not msgs:
                continue
            docs: list[dict] = []
            window: list[dict] = []
            size = 0
            for m in msgs:
                text = (m.get("text_content") or "").strip()
                if not text:
                    continue
                window.append(m)
                size += len(text)
                if len(window) >= wm or size >= wc:
                    docs.append(self._chat_window(scene, peer, window))
                    window, size = [], 0
            if window:
                docs.append(self._chat_window(scene, peer, window))
            n = await self._index_docs(docs)
            done += n
            if n < len(docs):
                break  # interrupted mid-conversation: leave cursor for retry
            await self.repo.rag_set_state(key, str(int(msgs[-1]["message_seq"])))
        return done

    @staticmethod
    def _chat_window(scene: str, peer: int, window: list[dict]) -> dict:
        first, last = window[0], window[-1]
        lines = [
            f"{m.get('sender_id')}: {(m.get('text_content') or '').strip()}" for m in window
        ]
        return {
            "kind": "chat",
            "ref": f"chat:{scene}:{peer}:{first['message_seq']}-{last['message_seq']}",
            "scene": scene, "peer_id": peer,
            "time_start": first.get("time"), "time_end": last.get("time"),
            "text": f"会话 {scene}/{peer}\n" + "\n".join(lines),
        }

    # ----------------------------------------------------------- status
    async def status(self) -> dict:
        """Snapshot for /status and the webui (never raises)."""
        try:
            counts = await self.repo.rag_doc_counts()
        except Exception:
            counts = {}
        emb = self.embedder
        return {
            "enabled": self.enabled,
            "chat_enabled": bool(self.cfg.rag.chat),
            "running": self.running,
            "docs": sum(counts.values()),
            "docs_by_kind": counts,
            "vec_model": await self.repo.rag_get_state("vec_model") or "",
            "vec_dims": int(await self.repo.rag_get_state("vec_dims") or 0),
            "embedding_model": getattr(emb, "model_id", ""),
            "embedding_available": bool(getattr(emb, "available", False)),
            "embedding_healthy": bool(getattr(emb, "healthy", True)),
            "last_index_at": self.last_index_at,
            "last_count": self.last_count,
        }

    # ----------------------------------------------------------- retrieval
    async def search(
        self, query: str, k: int | None = None, *, kind: str | None = None,
        scene: str | None = None, peer_id: int | None = None,
    ) -> list[dict]:
        if not self.enabled:
            return []
        try:
            qv = (await self.embedder.embed([query], query=True))[0]
        except Exception:
            log.exception("query embedding failed")
            return []
        want = k or self.cfg.rag.top_k
        hits = await self.vec.search(qv, want * 6)
        if not hits:
            return []
        docs = await self.repo.rag_docs_by_ids([rid for rid, _ in hits])
        out: list[dict] = []
        for rid, dist in hits:
            d = docs.get(rid)
            if not d:
                continue
            if kind and d["kind"] != kind:
                continue
            if scene and d.get("scene") != scene:
                continue
            if peer_id is not None and d.get("peer_id") != peer_id:
                continue
            out.append({
                "kind": d["kind"], "ref": d["ref"], "text": d["text"],
                "scene": d.get("scene"), "peer_id": d.get("peer_id"),
                "time_start": d.get("time_start"), "time_end": d.get("time_end"),
                "score": round(1.0 / (1.0 + dist), 4),
            })
            if len(out) >= want:
                break
        return out


async def _sleep(seconds: float) -> None:
    if seconds and seconds > 0:
        import asyncio

        await asyncio.sleep(seconds)
