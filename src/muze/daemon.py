"""muze daemon: external supervisor for `muze serve`.

Purpose: code-level updates need a process restart; `/reload` (or /restart)
makes serve exit with RESTART_EXIT (=100) and the daemon brings a fresh
process up immediately, so `git pull && /reload` is a complete update.

Also survives crashes: unexpected non-zero exits are restarted with
exponential backoff (5s -> 60s), reset after a stable run.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time

log = logging.getLogger("muze.daemon")

RESTART_EXIT = 100
STABLE_SECONDS = 120


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="muze daemon", description="supervise muze serve (restart on /reload, /restart, crash)"
    )
    p.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml (passed to serve)")
    return p.parse_args(argv)


def run_daemon(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    serve_cmd = [sys.executable, "-m", "muze", "serve", "-c", args.config]
    log.info("daemon starting serve: %s", " ".join(serve_cmd))
    child_env = {**os.environ, "MUZE_SUPERVISED": "1", "MUZE_SUPERVISOR_PID": str(os.getpid())}

    current: dict[str, subprocess.Popen | None] = {"proc": None}

    def _terminate_child(_signum=None, _frame=None) -> None:
        """Never leave an orphaned serve holding the web port when the daemon
        itself is stopped (Ctrl-C / SIGTERM)."""
        p = current.get("proc")
        if p is not None and p.poll() is None:
            p.terminate()
        raise SystemExit(0)

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _terminate_child)
        except (ValueError, OSError):
            pass

    backoff = 5.0
    while True:
        started = time.monotonic()
        proc = subprocess.Popen(serve_cmd, env=child_env)
        current["proc"] = proc
        rc = proc.wait()
        current["proc"] = None
        ran = time.monotonic() - started
        if rc == 0:
            log.info("serve exited cleanly; daemon exiting")
            return 0
        if rc == RESTART_EXIT:
            log.info("serve requested restart (rc=100); relaunching now")
            backoff = 5.0
            time.sleep(0.5)  # let the old listener fully release the port
            continue
        if ran >= STABLE_SECONDS:
            backoff = 5.0
        log.warning(
            "serve exited rc=%s after %.0fs; restarting in %.0fs", rc, ran, backoff
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


if __name__ == "__main__":
    raise SystemExit(run_daemon(sys.argv[1:]))
