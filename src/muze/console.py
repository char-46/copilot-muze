"""Interactive console front shared by `muze serve` (embedded thread) and
`muze cli`.

Two terminal strategies:

* prompt_toolkit + tty: the REPL runs inside ``patch_stdout()``, which
  transparently clears/redraws the prompt line when other threads (logging,
  prints) write to stdout, preserving the user's half-typed input.
* fallback (piped stdin, missing PT): manual coordination with the logging
  stream handler — before each log line we clear the current line and return
  the cursor to column 0, print the log line, then redraw a bare ``muze> ``
  prompt; the prompt itself always starts on a fresh line.

Known Windows pitfall this module avoids: calling the builtin input() from a
secondary thread repeatedly can fail after the first read (CRT stdin quirk).
prompt_toolkit talks to the console API directly and is immune to it.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import threading

from muze.core.service import CommandOutcome, ControlContext, ControlService

log = logging.getLogger("muze.console")

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.patch_stdout import patch_stdout

    _PT = True
except Exception:  # pragma: no cover - optional dependency
    _PT = False

BANNER = "muze console: /help 命令列表 · /shutdown 停止服务 · /exit 关闭控制台 · ↑/↓ 翻历史"

# ---- terminal line coordination (fallback mode) ----------------------------
_io_lock = threading.Lock()
_prompt_shown = False  # a "muze> " prompt is sitting on the current line


def clear_line_seq() -> str:
    """CR + spaces + CR: wipe the current line, cursor back to column 0."""
    cols = shutil.get_terminal_size((80, 24)).columns
    return "\r" + " " * max(0, cols - 1) + "\r"


def prompt_shown() -> bool:
    return _prompt_shown


def io_lock() -> threading.Lock:
    return _io_lock


def _show_prompt() -> None:
    """Start an input line: newline first so the prompt never trails a log."""
    global _prompt_shown
    with _io_lock:
        sys.stdout.write("\nmuze> ")
        sys.stdout.flush()
        _prompt_shown = True


def _hide_prompt() -> None:
    global _prompt_shown
    with _io_lock:
        _prompt_shown = False


def make_session(history_path) -> "PromptSession | None":
    if not _PT:
        return None
    try:
        return PromptSession(history=FileHistory(str(history_path)))
    except Exception as e:
        log.debug("prompt_toolkit session unavailable: %s", e)
        return None


def print_outcome(outcome: CommandOutcome) -> None:
    if outcome.is_error:
        text = f"[x] {outcome.text}"
    elif outcome.text:
        text = outcome.text
    else:
        text = ""
    with _io_lock:
        if text:
            sys.stdout.write(text + "\n")
        for p in outcome.media_paths:
            sys.stdout.write(f"  [media] {p}\n")
        sys.stdout.flush()


async def handle_line(service: ControlService, ctx: ControlContext, line: str) -> str:
    """Execute one console line. Returns 'continue' | 'close' | 'shutdown'."""
    line = (line or "").strip()
    if not line:
        return "continue"
    if line in ("/exit", "/quit", "/q"):
        print("console closed (service keeps running)", flush=True)
        return "close"
    if line == "/shutdown":
        return "shutdown"
    try:
        outcome = await service.execute(line, ctx)
    except Exception as e:
        print(f"[x] {e}", flush=True)
        return "continue"
    print_outcome(outcome)
    return "continue"


# --------------------------------------------------------------------------
# serve-embedded console: runs in a daemon thread, executes on the main loop
# --------------------------------------------------------------------------
def repl_thread(
    loop: asyncio.AbstractEventLoop,
    service: ControlService,
    ctx: ControlContext,
    history_path,
    stop_event: asyncio.Event,
) -> None:
    session = make_session(history_path) if sys.stdin.isatty() else None
    print(BANNER, flush=True)
    failures = 0
    try:
        if session is not None:
            with patch_stdout(raw=True):
                while not stop_event.is_set():
                    try:
                        line = session.prompt("muze> ")
                    except KeyboardInterrupt:
                        loop.call_soon_threadsafe(stop_event.set)
                        return
                    except EOFError:
                        log.info("console stdin closed; service keeps running")
                        return
                    action = _run_on_loop(loop, service, ctx, line)
                    if action in ("shutdown", "close"):
                        if action == "shutdown":
                            loop.call_soon_threadsafe(stop_event.set)
                            print("shutting down...", flush=True)
                        return
        else:
            _fallback_loop(loop, service, ctx, stop_event)
    except Exception:
        log.exception("console thread crashed")
    finally:
        _hide_prompt()


def _fallback_loop(loop, service, ctx, stop_event) -> None:
    global _prompt_shown
    failures = 0
    while not stop_event.is_set():
        _show_prompt()
        try:
            line = input()
        except KeyboardInterrupt:
            loop.call_soon_threadsafe(stop_event.set)
            return
        except EOFError:
            _hide_prompt()
            log.info("console stdin closed; service keeps running")
            return
        except Exception as e:
            _hide_prompt()
            failures += 1
            if failures >= 3:
                log.warning("console read failing repeatedly; console closed")
                return
            continue
        _hide_prompt()  # user pressed Enter; cursor is on a fresh line now
        action = _run_on_loop(loop, service, ctx, line)
        if action == "shutdown":
            loop.call_soon_threadsafe(stop_event.set)
            print("shutting down...", flush=True)
            return
        if action == "close":
            return


def _run_on_loop(loop, service, ctx, line) -> str:
    fut = asyncio.run_coroutine_threadsafe(handle_line(service, ctx, line), loop)
    try:
        return fut.result(timeout=600)
    except Exception as e:
        print(f"[x] {e}", flush=True)
        return "continue"


# --------------------------------------------------------------------------
# standalone cli console: prompt_toolkit native async loop
# --------------------------------------------------------------------------
async def repl_async(
    service: ControlService,
    ctx: ControlContext,
    history_path,
    on_shutdown=None,
) -> None:
    session = make_session(history_path)
    print(BANNER, flush=True)
    while True:
        try:
            if session is not None:
                with patch_stdout(raw=True):
                    line = await session.prompt_async("muze> ")
            else:
                _show_prompt()
                line = await asyncio.to_thread(input)
                _hide_prompt()
        except (EOFError, KeyboardInterrupt):
            _hide_prompt()
            print(flush=True)
            return
        action = await handle_line(service, ctx, line)
        if action == "shutdown":
            if on_shutdown is not None:
                on_shutdown()
            return
        if action == "close":
            return
