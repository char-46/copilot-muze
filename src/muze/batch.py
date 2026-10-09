"""Async Batch API runner (/v1/batch, OpenAI-compatible).

Batch jobs are ~50% cheaper and answered within a 24h completion window —
the right lane for delay-tolerant background loads (value scoring, nightly
digest, bulk OCR). Interactive turns never touch this path.

Lifecycle:
    submit(kind, items)   items = [{"custom_id", "messages", ...body}]
                          -> one JSONL uploaded, one batch created; the
                          pending job (kind + item contexts) persists to
                          data/batch_state.json, so a restart keeps polling
    poll loop             every batch.poll_interval_sec: retrieve each
                          pending batch; on completion, download the results
                          file, parse per custom_id, hand {custom_id: body}
                          to the registered KIND handler, record usage at
                          the batch price, drop the job from the state
    handlers              register_handler(kind, async fn(ctx_rows, results))
                          — the handler interprets results against the item
                          contexts it stored at submit time (the state file
                          carries them); a failing handler logs and drops
                          the job (callers retry naturally from their own
                          pending queues)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

from openai import AsyncOpenAI

from muze.ai.llm import LLMClient
from muze.config import BatchConfig, ModelEntry

log = logging.getLogger("muze.batch")

Handler = Callable[[list[dict], dict[str, dict]], Any]


def build_jsonl(model: str, items: list[dict]) -> str:
    """One request per line, the OpenAI batch file format."""
    lines = []
    for it in items:
        body = {"model": model, "messages": it["messages"], "stream": False}
        if it.get("temperature") is not None:
            body["temperature"] = it["temperature"]
        if it.get("tools"):
            body["tools"] = it["tools"]
            body["tool_choice"] = "auto"
        lines.append(
            json.dumps(
                {
                    "custom_id": str(it["custom_id"]),
                    "method": "POST",
                    "url": "/chat/completions",
                    "body": body,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + "\n"


def parse_results_jsonl(text: str) -> dict[str, dict]:
    """results file -> {custom_id: response.body}; failed requests carry
    {"error": ...} instead of a body."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = row.get("custom_id")
        if cid is None:
            continue
        if row.get("error"):
            out[str(cid)] = {"error": str(row["error"])}
        else:
            body = (row.get("response") or {}).get("body") or {}
            out[str(cid)] = body
    return out


class BatchRunner:
    def __init__(self, llm: LLMClient, cfg: BatchConfig, state_path: Path) -> None:
        self.llm = llm
        self.cfg = cfg
        self.state_path = state_path
        self._handlers: dict[str, Handler] = {}
        self._resets: dict[str, Handler] = {}  # kind -> fn(rowids) on drop
        self._clients: dict[str, AsyncOpenAI] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._state: dict[str, dict] = {}  # batch_id -> job
        self._load()

    # ------------------------------------------------------------- state
    def _load(self) -> None:
        if self.state_path.is_file():
            try:
                self._state = json.loads(self.state_path.read_text("utf-8"))
            except Exception:
                log.warning("batch state unreadable; starting fresh", exc_info=True)
                self._state = {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False), "utf-8")
        tmp.replace(self.state_path)

    # ---------------------------------------------------------- handlers
    def register_handler(self, kind: str, fn: Handler) -> None:
        self._handlers[kind] = fn

    def register_reset(self, kind: str, fn: Handler) -> None:
        """fn(rowids, None) awaited when a job of this kind dies (failed/
        expired/model gone) so callers can un-mark their in-flight rows."""
        self._resets[kind] = fn

    async def _drop(self, bid: str, why: str) -> None:
        job = self._state.pop(bid, None)
        self._save()
        if not job:
            return
        log.warning("batch %s dropped (%s)", bid, why)
        reset = self._resets.get(job.get("kind"))
        if reset is not None and job.get("rowids"):
            try:
                r = reset(job["rowids"], None)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("batch reset hook failed")

    def pending_rowids(self, kind: str) -> set[int]:
        """Item ids currently in flight for a kind (callers skip them when
        collecting new work, so a slow batch never gets resubmitted)."""
        out: set[int] = set()
        for job in self._state.values():
            if job.get("kind") == kind:
                out.update(int(r) for r in job.get("rowids", []))
        return out

    # ------------------------------------------------------------- submit
    async def submit(
        self, role: str, kind: str, items: list[dict], rowids: list[int]
    ) -> str | None:
        """Create one batch; returns its id (None = couldn't submit)."""
        chain = self.llm.resolve(role)
        if not chain:
            log.warning("batch submit skipped: role %s has no model", role)
            return None
        name, entry = chain[0]
        if len(items) > self.cfg.max_requests:
            items = items[: self.cfg.max_requests]
        jsonl = build_jsonl(entry.model, items)
        client = self._client(name, entry)
        try:
            f = await client.files.create(
                file=("batch.jsonl", jsonl.encode("utf-8"), "application/jsonl"),
                purpose="batch",
            )
            b = await client.batches.create(
                input_file_id=f.id,
                endpoint="/chat/completions",
                completion_window=self.cfg.completion_window,
                metadata={"kind": kind},
            )
        except Exception as e:
            log.warning("batch submit failed (%s/%s): %s", name, entry.model, e)
            return None
        self._state[b.id] = {
            "kind": kind,
            "model": name,
            "rowids": [int(r) for r in rowids],
            "items_ctx": [
                {k: v for k, v in it.items() if k not in ("messages", "tools")}
                for it in items
            ],
            "submitted_at": __import__("time").time(),
        }
        self._save()
        log.info(
            "batch submitted: %s items=%s kind=%s model=%s/%s",
            b.id, len(items), kind, name, entry.model,
        )
        return b.id

    def _client(self, name: str, entry: ModelEntry) -> AsyncOpenAI:
        from openai import Timeout

        if name not in self._clients:
            self._clients[name] = AsyncOpenAI(
                base_url=entry.base_url,
                api_key=entry.api_key,
                timeout=Timeout(connect=15, read=60, write=120, pool=15),
                max_retries=1,
            )
        return self._clients[name]

    # -------------------------------------------------------------- poll
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.poll_interval_sec
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("batch poll failed")

    async def poll_once(self) -> int:
        """Check every pending batch; apply and drop the finished ones.
        Returns how many jobs completed this pass."""
        done = 0
        for bid, job in list(self._state.items()):
            name = job.get("model") or ""
            entry = self.llm._entries().get(name)
            if entry is None:
                await self._drop(bid, f"model {name} gone from registry")
                continue
            client = self._client(name, entry)
            try:
                b = await client.batches.retrieve(bid)
            except Exception as e:
                log.warning("batch %s retrieve failed: %s", bid, e)
                continue
            status = getattr(b, "status", "")
            if status in ("failed", "cancelled", "expired"):
                await self._drop(bid, f"status={status}")
                continue
            if status != "completed":
                continue
            out_fid = getattr(b, "output_file_id", None)
            if not out_fid:
                await self._drop(bid, "completed without output file")
                continue
            try:
                content = await client.files.content(out_fid)
                text = content.text if hasattr(content, "text") else str(content)
            except Exception as e:
                log.warning("batch %s results download failed: %s", bid, e)
                continue
            results = parse_results_jsonl(text)
            self._record_usage(job, results)
            handler = self._handlers.get(job.get("kind"))
            if handler is None:
                log.warning("batch %s: no handler for kind=%s", bid, job.get("kind"))
            else:
                try:
                    await handler(job.get("items_ctx", []), results)
                except Exception:
                    log.exception("batch %s handler failed; dropping", bid)
            self._state.pop(bid, None)
            self._save()
            done += 1
            log.info("batch %s applied: %s results (kind=%s)", bid, len(results), job.get("kind"))
        return done

    def _record_usage(self, job: dict, results: dict[str, dict]) -> None:
        """Usage from the batch's reported totals; cost at the batch price
        (Pricing.batch_factor, e.g. 0.5 on DeepSeek/OpenAI)."""
        name = job.get("model") or ""
        p = c = 0
        for body in results.values():
            u = body.get("usage") or {}
            p += int(u.get("prompt_tokens") or 0)
            c += int(u.get("completion_tokens") or 0)
        if not (p or c) or self.llm.costs is None:
            return
        from muze.model_catalog import Pricing, Usage

        usage = Usage(prompt_tokens=p, completion_tokens=c, calls=len(results))
        pricing = self.llm._pricing.get(name)
        cost = None
        if isinstance(pricing, Pricing):
            cost = pricing.cost(usage, batch=True)
        elif callable(pricing):
            cost = float(pricing(usage, None))
        try:
            self.llm.costs.record(name, usage, cost)
        except Exception:
            log.debug("batch usage recording failed", exc_info=True)
