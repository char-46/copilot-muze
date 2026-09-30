"""Resource governor: decide WHEN local embedding may run and on WHAT.

Best-effort and fully degradable — if ``psutil`` / ``pynvml`` are missing the
corresponding signals are simply "unknown" and don't block. Probes are plain
methods so tests can override them.

Policy (see rag.embedding / rag):
- only on AC power (``rag.require_ac_power`` + psutil battery),
- only while CPU busy% and GPU util% are below their idle thresholds,
- only after ``rag.min_idle_sec`` without user activity,
- GPU is used only when enough free VRAM exists and the GPU is quiet.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("muze.rag")


def _try_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


class ResourceGovernor:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._psutil = _try_import("psutil")
        self._nvml = None
        self._nvml_ready = False
        self._cpu_pct: float | None = None

    # ------------------------------------------------------------- probes
    def power_plugged(self) -> bool | None:
        if self._psutil is None:
            return None
        try:
            bat = self._psutil.sensors_battery()
            return None if bat is None else bool(bat.power_plugged)
        except Exception:
            return None

    def cpu_busy(self) -> float | None:
        if self._psutil is None:
            return None
        try:
            pct = self._psutil.cpu_percent(interval=None)
            return pct if pct > 0 else (self._cpu_pct or 0.0)
        except Exception:
            return None

    def _ensure_nvml(self) -> bool:
        if self._nvml_ready:
            return self._nvml is not None
        self._nvml_ready = True
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
        except Exception:
            self._nvml = None
        return self._nvml is not None

    def gpu_util(self) -> float | None:
        if not self._ensure_nvml():
            return None
        try:
            h = self._nvml.nvmlDeviceGetHandleByIndex(0)
            return float(self._nvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            return None

    def gpu_free_gb(self) -> float | None:
        if not self._ensure_nvml():
            return None
        try:
            h = self._nvml.nvmlDeviceGetHandleByIndex(0)
            return self._nvml.nvmlDeviceGetMemoryInfo(h).free / (1024 ** 3)
        except Exception:
            return None

    # ------------------------------------------------------------- policy
    def idle(self, last_activity: float = 0.0) -> tuple[bool, str]:
        now = time.time()
        if self.cfg.require_ac_power:
            p = self.power_plugged()
            if p is False:
                return False, "on battery"
        cpu = self.cpu_busy()
        if cpu is not None and cpu > self.cfg.idle_cpu_pct:
            return False, f"cpu {cpu:.0f}%"
        gpu = self.gpu_util()
        if gpu is not None and gpu > self.cfg.idle_gpu_pct:
            return False, f"gpu {gpu:.0f}%"
        if last_activity and now - last_activity < self.cfg.min_idle_sec:
            return False, "recent activity"
        return True, "idle"

    def gpu_fits(self) -> tuple[bool, str]:
        """Is the GPU fit AND quiet enough to host the embedding model?"""
        free = self.gpu_free_gb()
        if free is None:
            return False, "no nvml"
        need = self.cfg.embedding.gpu_weight_gb + self.cfg.embedding.gpu_reserve_gb
        if free < need:
            return False, f"vram free {free:.1f}G < {need:.1f}G"
        util = self.gpu_util()
        if util is not None and util > self.cfg.embedding.gpu_max_util:
            return False, f"gpu busy {util:.0f}%"
        return True, "ok"

    def choose_device(self) -> str:
        want = self.cfg.embedding.device
        if want in ("cuda", "cpu"):
            return want
        ok, why = self.gpu_fits()
        if ok:
            return "cuda"
        log.debug("RAG device=cpu (%s)", why)
        return "cpu"

    def cpu_threads(self) -> int:
        cores = os.cpu_count() or 2
        busy = self.cpu_busy() or 0.0
        free = max(0.0, 1.0 - busy / 100.0)
        cap = max(1, int(self.cfg.embedding.cpu_max_threads))
        return max(1, min(cap, int(round(cores * free))))

    def snapshot(self) -> dict:
        return {
            "power": self.power_plugged(),
            "cpu_pct": self.cpu_busy(),
            "gpu_util": self.gpu_util(),
            "gpu_free_gb": self.gpu_free_gb(),
        }
