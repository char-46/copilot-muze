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
            self._device = ""
            log.warning("local embedding model load failed (%s): %s", device, e)

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

    def unload_if_idle(self, now: float | None = None) -> bool:
        if self._local is None or self.cfg.unload_idle_sec <= 0:
            return False
        now = now or time.time()
        if now - self._last_use < self.cfg.unload_idle_sec:
            return False
        self._local = None
        self._device = ""
        log.info("RAG embedder unloaded after idle")
        return True

    async def force_unload(self) -> bool:
        """Controller-requested unload (/embed unload): drop the local model
        immediately, freeing VRAM/RAM; it reloads on demand at the next
        embedding (with fresh device selection)."""
        async with self._lock:
            if self._local is None:
                return False
            self._local = None
            self._device = ""
            log.info("RAG embedder force-unloaded by command")
            return True

    # ------------------------------------------------- vram stewardship
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
                self._local = None
                self._device = ""
                action = "unloaded"
            else:
                await asyncio.to_thread(self._move_local, "cpu")
                action = "cpu"
            log.info("RAG embedder evicted from GPU (%s) -> %s", why, action)
            return action

    async def promote_to_gpu_if_fits(self, governor=None) -> bool:
        """The mirror of eviction: once a CPU-resident model fits the
        comfortable GPU tier again (game exited; headroom + util gates give
        natural hysteresis vs the evict threshold), reload it on the GPU —
        reload rather than .to() so dtype/thread choices stay fresh. An
        explicitly configured cpu device is never promoted."""
        if self._local is None or self._device != "cpu" or governor is None:
            return False
        if (self.cfg.device or "auto") not in ("auto", "cuda"):
            return False
        try:
            fits, why = governor.gpu_fits()
        except Exception:
            return False
        if not fits:
            return False
        async with self._lock:
            if self._local is None or self._device != "cpu":
                return False
            self._local = None
            self._device = ""
            try:
                await self._load_local("cuda", governor)
                log.info("RAG embedder promoted back to GPU (%s)", why)
                return True
            except Exception as e:
                log.warning("RAG embedder GPU promote failed: %s", e)
                return False

    def _move_local(self, device: str) -> None:
        self._local.to(device)
        if device == "cpu":
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
        self._device = device

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
    def _ok(self) -> None:
        self._fails = 0
        self._last_use = time.time()

    def _fail(self) -> None:
        self._fails += 1
        if self._fails >= max(1, int(self.cfg.max_retries)):
            self._fails = 0
            self._cooldown_until = time.time() + float(self.cfg.cooldown_sec)
            log.warning(
                "local embedder cooling down for %.0fs after repeated failures",
                self.cfg.cooldown_sec,
            )

    @property
    def cooling(self) -> bool:
        return time.time() < self._cooldown_until
