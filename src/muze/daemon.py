"""muze daemon: external supervisor for `muze serve`.

Purpose: code-level updates need a process restart; `/reload` (or /restart)
makes serve exit with RESTART_EXIT (=100) and the daemon brings a fresh
process up immediately, so `git pull && /reload` is a complete update.

Also survives crashes: unexpected non-zero exits are restarted with
exponential backoff (5s -> 60s), reset after a stable run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
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

    backoff = 5.0
    while True:
        started = time.monotonic()
        try:
            proc = subprocess.run(serve_cmd)
        except KeyboardInterrupt:
            log.info("daemon interrupted; exiting")
            return 0
        rc = proc.returncode
        ran = time.monotonic() - started
        if rc == 0:
            log.info("serve exited cleanly; daemon exiting")
            return 0
        if rc == RESTART_EXIT:
            log.info("serve requested restart (rc=100); relaunching now")
            backoff = 5.0
            continue
        if ran >= STABLE_SECONDS:
            backoff = 5.0
        log.warning(
            "serve exited rc=%s after %.0fs; restarting in %.0fs", rc, ran, backoff
        )
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            return 0
        backoff = min(backoff * 2, 60.0)


if __name__ == "__main__":
    raise SystemExit(run_daemon(sys.argv[1:]))
