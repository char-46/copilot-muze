"""muze entrypoint: `muze serve` (default), `muze cli`, `muze version`."""

from __future__ import annotations

import sys


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

        return run_daemon(rest)

    if cmd == "serve":
        from muze.main import run_main

        return run_main(rest)

    if cmd == "cli":
        from muze.cli.repl import run_cli

        return run_cli(rest)

    print(f"unknown command: {cmd} (expected: serve | daemon | cli | cfg | version)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
