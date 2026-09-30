"""muze entrypoint: `muze serve` (default), `muze cli`, `muze version`."""

from __future__ import annotations

import logging
import os
import sys


def _exit_now(rc: int) -> None:
    """Deterministic process exit.

    Cleanup (DB, adapter, webui) has already run inside run_main; exiting via
    os._exit guarantees no lingering non-daemon thread (console reader, http
    helper) can wedge the process after a /restart — a wedged serve holds the
    web port and the daemon can never relaunch.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        logging.shutdown()
    except Exception:
        pass
    os._exit(rc)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    rest = argv[1:]

    if cmd in ("-V", "version", "--version"):
        from muze import get_version

        print(get_version())
        return 0

    if cmd == "cfg":
        from muze.cfgcli import run_cfg

        return run_cfg(rest)

    if cmd == "daemon":
        from muze.daemon import run_daemon

        rc = run_daemon(rest)
        _exit_now(rc)

    if cmd == "serve":
        from muze.main import run_main

        rc = run_main(rest)
        _exit_now(rc)

    if cmd == "cli":
        from muze.cli.repl import run_cli

        return run_cli(rest)

    print(f"unknown command: {cmd} (expected: serve | daemon | cli | cfg | version)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
