"""Embedding backend for vector RAG.

Local-first (``Qwen/Qwen3-Embedding-0.6B`` by default via sentence-transformers)
with an optional remote OpenAI-compatible fallback. Designed to coexist with
the rest of the process:

- **device**: GPU only when it fits (free VRAM) and is quiet, else CPU
  (decided by :class:`~muze.rag_resource.ResourceGovernor` per indexing pass);
- **single-flight + throttle**: one embed at a time, small batches, a pause
  between batches;
- **back-off**: repeated failures trip a cooldown so we never hammer a broken
  backend; while cooling, a configured remote fallback is used;
- **idle unload**: the local model can be released after inactivity;
- **VRAM stewardship**: under system-wide VRAM pressure a GPU-resident model
  is evicted (moved to RAM, or dropped for fixed-device backends) and
  promoted back once the GPU is comfortable again.

One model id == one vector space; callers must rebuild the index when it
changes (VecStore enforces this).
"""

from __future__ import annotations

import asyncio
import logging
import time

from openai import AsyncOpenAI

from muze.config import RagEmbedding

log = logging.getLogger("muze.llm")


def _rss_gb() -> float:
    """Current process RSS in GiB (-1 when unknown); logged on load failures
    so allocator growth is visible in the log next time."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1024**3
    except Exception:
        return -1.0


class Embedder:
    def __init__(self, cfg: RagEmbedding) -> None:
        self.cfg = cfg
        self._client: AsyncOpenAI | None = None
        self._local = None  # SentenceTransformer | fastembed TextEmbedding
        self._device = ""
        self._dims = int(cfg.dims or 0)
        self._lock = asyncio.Lock()
        self._fails = 0
        self._cooldown_until = 0.0
        self._last_use = 0.0
        self._last_busy_unload = 0.0  # debounce for busy-unload vs reload churn
        self._BUSY_RELOAD_GRACE = 180.0  # seconds a busy-unloaded model stays down
        # anti promote/evict ping-pong (live 2026-11-02): after a VRAM
        # eviction, promotion is refused for promote_cooldown_sec (doubling
        # per repeat, capped 1h); a promote that survives 30min resets the streak
        self._last_vram_evict = 0.0
        self._evict_streak = 0
        self._promoted_at = 0.0
        self._PROMOTE_STABLE_SEC = 1800.0

    # ------------------------------------------------------------ status
    @property
    def provider(self) -> str:
        return self.cfg.provider or "auto"

    @property
    def _local_enabled(self) -> bool:
        return self.provider in ("auto", "local") and bool(self.cfg.model)

    @property
    def _remote_configured(self) -> bool:
        return (
            self.provider in ("auto", "openai")
            and bool(self.cfg.remote_model)
            and bool(self.cfg.api_key or self.cfg.base_url)
        )

    @property
    def available(self) -> bool:
        return self._local_enabled or self._remote_configured

    @property
    def configured(self) -> bool:
        return self.available

    @property
    def local_ready(self) -> bool:
        return self._local_enabled and time.time() >= self._cooldown_until

    @property
    def healthy(self) -> bool:
        return self.local_ready or self._remote_configured

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def model_id(self) -> str:
        return self.cfg.model if self._local_enabled else self.cfg.remote_model

    # ------------------------------------------------------------ prepare
    async def prepare(self, governor=None, force: bool = False) -> None:
        """Load the local model if it isn't loaded yet (device chosen via the
        governor when auto). STICKY: an already-loaded model is never
        reloaded onto another device — the probe reads instantaneous
        utilisation, and the embedder's own bursts would otherwise flip the
        device back and forth (load thrash). `force` (an explicit user-run
        indexing pass) re-picks the device right now: unload and reload on
        the governor's current best. Re-evaluation otherwise happens after
        an idle unload."""
        if not self._local_enabled:
            return
        if self._local is not None and not force:
            return
        if time.monotonic() - self._last_busy_unload < self._BUSY_RELOAD_GRACE:
            # freshly unloaded under load: don't reload into the same storm
            return
        device = self.cfg.device
        if device == "auto":
            device = governor.choose_device() if governor is not None else "cpu"
        try:
            async with self._lock:
                if force:  # drop the old instance: reload on the new device
                    self._local = None
                    self._device = ""
                await self._load_local(device, governor, full_threads=force)
        except Exception as e:  # surfaced again by embed(); keep it non-fatal
            log.warning(
                "local embedding model load failed (%s): %s (rss=%.2fG)",
                device, e, _rss_gb(),
            )
            # a failed load must tear down completely: half-built models and
            # allocator-cached blocks survived dozens of retries and grew the
            # process to 8G RSS while every probe said "not loaded" (live
            # 2026-11-10/11)
            self._teardown_local()

    async def _load_local(self, device: str, governor=None, full_threads: bool = False) -> None:
        if governor is not None:
            threads = governor.cpu_threads(force=full_threads)
        else:
            threads = int(self.cfg.cpu_max_threads)
        if self.cfg.backend == "fastembed":
            providers = ["CPUExecutionProvider"]
            if device == "cuda":
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                device = "cuda"
            else:
                device = "cpu"
            await asyncio.to_thread(self._load_fastembed, providers, threads)
        else:
            device = "cuda" if device == "cuda" else "cpu"
            await asyncio.to_thread(self._load_st, device, threads)
        self._device = device

    def _load_st(self, device: str, threads: int) -> None:
        import torch  # noqa: F401  (clear error if torch missing)
        from huggingface_hub import try_to_load_from_cache
        from sentence_transformers import SentenceTransformer

        if device == "cpu":
            torch.set_num_threads(max(1, threads))
        kwargs = {}
        if device == "cuda":
            try:
                kwargs["model_kwargs"] = {"torch_dtype": torch.float16}
            except Exception:
                pass
        # Cached model? Load OFFLINE: skips the HF HEAD etag revalidation on
        # every reload (an idle-governor unload + reload must not depend on
        # huggingface.co being reachable). First download stays online.
        cached = try_to_load_from_cache(self.cfg.model, "modules.json")
        if cached is not None:
            kwargs["local_files_only"] = True
        try:
            self._local = SentenceTransformer(self.cfg.model, device=device, **kwargs)
        except Exception:
            if cached is None:
                raise
            # stale/incomplete cache: retry online once
            kwargs.pop("local_files_only", None)
            self._local = SentenceTransformer(self.cfg.model, device=device, **kwargs)
        log.info("RAG embedder | st model=%s device=%s threads=%s%s",
                 self.cfg.model, device, threads if device == "cpu" else "-",
                 " (offline cache)" if cached is not None else "")

    def _load_fastembed(self, providers: list[str], threads: int) -> None:
        from fastembed import TextEmbedding

        self._local = TextEmbedding(
            model_name=self.cfg.model, providers=providers, threads=max(1, threads)
        )
        log.info("RAG embedder | fastembed model=%s providers=%s",
                 self.cfg.model, providers)

    # ---------------------------------------------------------- teardown
    def _teardown_local(self) -> None:
        """Drop the model object and return torch caches to the OS. Setting
        _local=None alone leaves allocator-cached host blocks behind (torch's
        CPU allocator rarely returns them), so repeated load/unload cycles
        ratchet RSS upward."""
        self._local = None
        self._device = ""
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def unload_if_idle(self, now: float | None = None) -> bool:
        if self._local is None or self.cfg.unload_idle_sec <= 0:
            return False
        now = now or time.time()
        if now - self._last_use < self.cfg.unload_idle_sec:
            return False
        self._teardown_local()
        log.info("RAG embedder unloaded after idle")
        return True

    async def force_unload(self) -> bool:
        """Controller-requested unload (/embed unload): drop the local model
        immediately, freeing VRAM/RAM; it reloads on demand at the next
        embedding (with fresh device selection)."""
        async with self._lock:
            if self._local is None:
                return False
            self._teardown_local()
            log.info("RAG embedder force-unloaded by command")
            return True

    # ------------------------------------------------- vram stewardship
    async def unload_if_busy(self, governor=None) -> str | None:
        """Sustained machine load (user gaming / compiling / another model):
        unlike the idle gates — which merely skip indexing — this FREES the
        resident model's RAM/VRAM entirely. It reloads on demand after a
        grace period once pressure passes (prepare's debounce). Returns the
        "why" string when an unload happened."""
        if self._local is None or governor is None:
            return None
        why: list[str] = []
        cpu_thr = float(self.cfg.busy_unload_cpu_pct or 0)
        gpu_thr = float(self.cfg.busy_unload_gpu_util or 0)
        if cpu_thr > 0:
            try:
                cpu = governor.cpu_busy()
            except Exception:
                cpu = None
            if cpu is not None and cpu >= cpu_thr:
                why.append(f"cpu {cpu:.0f}%>={cpu_thr:.0f}%")
        if gpu_thr > 0:
            try:
                gpu = governor.gpu_util()
            except Exception:
                gpu = None
            if gpu is not None and gpu >= gpu_thr:
                why.append(f"gpu {gpu:.0f}%>={gpu_thr:.0f}%")
        if not why:
            return None
        async with self._lock:
            if self._local is None:
                return None
            self._teardown_local()
            self._last_busy_unload = time.monotonic()
            log.info("RAG embedder unloaded under load (%s)", " & ".join(why))
            return " & ".join(why)

    async def evict_if_vram_pressure(self, governor=None) -> str | None:
        """System-wide VRAM pressure (a game started, another app's model,
        ...): get OUR model off the GPU right now. sentence-transformers
        moves to system RAM and stays usable (slower); fastembed's ONNX
        session is device-fixed, so it is dropped and reloads on demand.
        Returns "cpu" | "unloaded" | None (nothing to do)."""
        if self._local is None or self._device != "cuda" or governor is None:
            return None
        try:
            pressed, why = governor.vram_pressure()
        except Exception:
            return None
        if not pressed:
            return None
        async with self._lock:
            if self._local is None or self._device != "cuda":
                return None
            if self.cfg.backend == "fastembed":
                self._teardown_local()
                action = "unloaded"
            else:
                await asyncio.to_thread(self._move_local, "cpu")
                action = "cpu"
            now = time.monotonic()
            if self._promoted_at and now - self._promoted_at >= self._PROMOTE_STABLE_SEC:
                self._evict_streak = 0  # that promotion had a good run
            self._evict_streak += 1
            self._last_vram_evict = now
            log.info(
                "RAG embedder evicted from GPU (%s) -> %s [streak %s, promote cooldown %.0fs]",
                why, action, self._evict_streak,
                self._promote_cooldown(),
            )
            return action

    def _promote_cooldown(self) -> float:
        """Seconds promotion stays refused after the last eviction."""
        base = float(self.cfg.promote_cooldown_sec or 0)
        if base <= 0:
            return 0.0
        return min(base * (2 ** max(0, self._evict_streak - 1)), 3600.0)

    async def unload_if_on_battery(self, governor=None) -> str | None:
        """No AC power -> the resident model shouldn't hold RAM/VRAM at all
        (user direction 2026-11-03: 无ac电源卸载模型). Desktops report None
        (no battery sensor) and are never touched. Reloads on demand once
        power returns. Returns "battery" when a drop happened."""
        if not self.cfg.battery_unload or self._local is None or governor is None:
            return None
        try:
            plugged = governor.power_plugged()
        except Exception:
            return None
        if plugged is not False:
            return None
        async with self._lock:
            if self._local is None:
                return None
            self._teardown_local()
            self._last_busy_unload = time.monotonic()
            log.info("RAG embedder unloaded (on battery, no AC power)")
            return "battery"

    async def promote_to_gpu_if_fits(self, governor=None) -> bool:
        """The mirror of eviction: once a CPU-resident model fits the
        comfortable GPU tier again (game exited; headroom + util gates give
        natural hysteresis vs the evict threshold), reload it on the GPU —
        reload rather than .to() so dtype/thread choices stay fresh. An
        explicitly configured cpu device is never promoted.

        Three extra guards (live 2026-11-02 ping-pong + user direction
        2026-11-03): no promotion on battery, none during the post-eviction
        cooldown, and used% must sit below the promote cap (evict-gap)."""
        if self._local is None or self._device != "cpu" or governor is None:
            return False
        if (self.cfg.device or "auto") not in ("auto", "cuda"):
            return False
        if self.cfg.battery_unload:
            try:
                if governor.power_plugged() is False:
                    return False
            except Exception:
                pass
        cd = self._promote_cooldown()
        if cd > 0 and self._last_vram_evict and time.monotonic() - self._last_vram_evict < cd:
            return False
        try:
            fits, why = governor.gpu_fits()
        except Exception:
            return False
        if not fits:
            return False
        try:
            below_line, why2 = governor.gpu_promote_ok()
        except Exception:
            below_line = True  # governor without the newer probe
        if not below_line:
            return False
        async with self._lock:
            if self._local is None or self._device != "cpu":
                return False
            self._local = None
            self._device = ""
            try:
                await self._load_local("cuda", governor)
                self._promoted_at = time.monotonic()
                log.info("RAG embedder promoted back to GPU (%s)", why)
                return True
            except Exception as e:
                log.warning("RAG embedder GPU promote failed: %s", e)
                return False

    def _move_local(self, device: str) -> None:
        self._local.to(device)
        if device == "cpu":
            # empty_cache only matters when torch state exists — i.e. torch
            # was already imported by a real model load. Importing it here
            # just to clear a cache that cannot exist costs seconds.
            import sys

            torch = sys.modules.get("torch")
            if torch is not None:
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
        self._device = device

    # --------------------------------------------- manual model control
    # (/model load|unload|ram|vram)
    @property
    def loaded(self) -> bool:
        return self._local is not None

    @property
    def device_label(self) -> str:
        return ("gpu" if self._device == "cuda" else "ram") if self._local is not None else ""

    async def move(self, device: str, governor=None) -> str:
        """'cpu' | 'cuda'. A loaded model MOVES in place (sentence-transformers
        sessions survive .to()); an unloaded one just loads where asked."""
        dev = "cuda" if device == "cuda" else "cpu"
        if dev == "cuda":
            from muze.perception import _cuda_available

            if not _cuda_available():
                return "无可用 CUDA"
        async with self._lock:
            if self._local is None:
                try:
                    await self._load_local(dev, governor)
                    return f"已加载到 {'GPU' if dev == 'cuda' else '内存'}"
                except Exception as e:
                    return f"加载失败: {e}"
            try:
                await asyncio.to_thread(self._move_local, dev)
                return f"已迁移到 {'GPU(vram)' if dev == 'cuda' else '内存(ram)'}"
            except Exception as e:
                return f"迁移失败: {e}"

    # -------------------------------------------------------------- embed
    async def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        if query and self.cfg.query_instruction:
            texts = [f"{self.cfg.query_instruction}\n{t}" for t in texts]
        err: Exception | None = None
        if self.local_ready:
            try:
                async with self._lock:
                    vecs = await asyncio.to_thread(self._embed_local_sync, texts)
                self._ok()
                return vecs
            except Exception as e:
                err = e
                self._fail()
                log.warning("local embedding failed: %s", e)
        if self._remote_configured:
            vecs = await self._embed_openai(texts)
            self._ok()
            return vecs
        if err is not None:
            raise err
        raise RuntimeError("embedding 未配置（rag.embedding）")

    def _embed_local_sync(self, texts: list[str]) -> list[list[float]]:
        if self._local is None:
            # no prepare() happened: load on CPU to stay safe
            if self.cfg.backend == "fastembed":
                self._load_fastembed(["CPUExecutionProvider"], int(self.cfg.cpu_max_threads))
            else:
                self._load_st("cpu", int(self.cfg.cpu_max_threads))
            self._device = "cpu"
        if self.cfg.backend == "fastembed":
            vecs = [list(v) for v in self._local.embed(texts)]
        else:
            arr = self._local.encode(
                texts, batch_size=max(1, int(self.cfg.batch)),
                normalize_embeddings=True, convert_to_numpy=True,
                show_progress_bar=False,
            )
            vecs = [v.tolist() for v in arr]
        if vecs and not self._dims:
            self._dims = len(vecs[0])
        return vecs

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.cfg.base_url or None,
                api_key=self.cfg.api_key or "not-needed",
                max_retries=1,
            )
        out: list[list[float]] = []
        batch = max(1, int(self.cfg.batch))
        for i in range(0, len(texts), batch):
            resp = await self._client.embeddings.create(
                model=self.cfg.remote_model, input=texts[i : i + batch]
            )
            out.extend([list(d.embedding) for d in resp.data])
        if out and not self._dims:
            self._dims = len(out[0])
        log.info("embed | remote model=%s n=%s", self.cfg.remote_model, len(texts))
        return out

    # ---------------------------------------------------------- back-off
    COOLDOWN_MAX_SEC = 6 * 3600.0  # escalating backoff ceiling

    def _ok(self) -> None:
        self._fails = 0
        self._cooldown_streak = 0
        self._last_use = time.time()

    def _fail(self) -> None:
        self._fails += 1
        if self._fails >= max(1, int(self.cfg.max_retries)):
            self._fails = 0
            # ESCALATING cooldown: consecutive cooldowns double, up to 6h.
            # The old flat 300s turned a day-long network outage into a
            # 13-minute retry storm -- dozens of failed loads, each leaving
            # allocator residue (live 2026-11-10: hf-mirror unreachable all
            # day, load retried ~80 times, RSS 8G by morning).
            streak = getattr(self, "_cooldown_streak", 0) + 1
            self._cooldown_streak = streak
            cd = min(
                float(self.cfg.cooldown_sec) * (2 ** (streak - 1)),
                self.COOLDOWN_MAX_SEC,
            )
            self._cooldown_until = time.time() + cd
            log.warning(
                "local embedder cooling down for %.0fs after repeated failures (streak %s)",
                cd, streak,
            )

    @property
    def cooling(self) -> bool:
        return time.time() < self._cooldown_until
