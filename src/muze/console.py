"""Interactive console front shared by `muze serve` (embedded thread) and
`muze cli`.

Layout: the input line is PINNED to the last terminal line. Log lines and
command output are always inserted ABOVE it (second-to-last and up): before
writing we wipe the input line, print the text, then redraw
``muze> <half-typed buffer>`` on the fresh last line. Implemented with a tiny
key-level line editor (chars / backspace / Ctrl-U / Esc / Up-Down history)
talking to the console API directly — no ``input()`` from a secondary thread
(a known Windows CRT failure, which is what made the console unresponsive
after a daemon relaunch) and no prompt_toolkit dependency.

Piped (non-tty) stdin keeps a plain ``input()`` loop: no redraw coordination
is needed there because nobody is typing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

from muze.core.service import CommandOutcome, ControlContext, ControlService

log = logging.getLogger("muze.console")

BANNER = (
    "muze console: /help 命令列表 · /shutdown 停止服务 · /exit 关闭控制台 · "
    "↑/↓ 历史 · Esc/Ctrl-U 清行 · Ctrl-C 空行=停服"
)


# ===========================================================================
# sticky bottom input line — shared with the logging stream handler
# ===========================================================================
_io_lock = threading.Lock()
_active = False          # an input line is currently sitting on the last line
_prompt = "muze> "
_buffer = ""
_vt = False              # terminal understands ANSI clear-line


def io_lock() -> threading.Lock:
    return _io_lock


def _clr() -> str:
    """Wipe the current line and put the cursor at column 0."""
    if _vt:
        return "\r\x1b[2K"
    import shutil

    cols = shutil.get_terminal_size((80, 24)).columns
    return "\r" + " " * max(0, cols - 1) + "\r"


def enable_ansi() -> None:
    """Enable VT processing on legacy Windows consoles (no-op elsewhere)."""
    global _vt
    if os.name != "nt":
        _vt = True
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            _vt = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        else:
            _vt = False
    except Exception:
        _vt = False


def set_current_prompt(prompt: str) -> None:
    global _prompt
    with _io_lock:
        _prompt = prompt
        if _active:
            sys.stdout.write(_clr() + _prompt + _buffer)
            sys.stdout.flush()


def current_prompt() -> str:
    return _prompt


def show_input(prompt: str | None = None, buffer: str = "") -> None:
    """Draw the input line on the current (fresh, empty) last line."""
    global _active, _prompt, _buffer
    with _io_lock:
        if prompt is not None:
            _prompt = prompt
        _buffer = buffer
        _active = True
        sys.stdout.write(_prompt + _buffer)
        sys.stdout.flush()


def hide_input() -> None:
    global _active
    with _io_lock:
        _active = False


def update_buffer(buf: str) -> None:
    """Redraw the input line with a new buffer (cursor stays at the end)."""
    global _buffer
    with _io_lock:
        _buffer = buf
        if _active:
            sys.stdout.write(_clr() + _prompt + _buffer)
            sys.stdout.flush()


def write_above(text: str) -> None:
    """Print (multi-line) text ABOVE the input line; the input line stays last.

    This is the ONE function every background writer (logging handler,
    command output) must go through while the console is active.
    """
    with _io_lock:
        out = sys.stdout
        if _active:
            out.write(_clr())
        if text and not text.endswith("\n"):
            text += "\n"
        out.write(text)
        if _active:
            out.write(_prompt + _buffer)
        out.flush()


def print_outcome(outcome: CommandOutcome) -> None:
    text = f"[x] {outcome.text}" if outcome.is_error else outcome.text
    lines = ([text] if text else []) + [f"  [media] {p}" for p in outcome.media_paths]
    if lines:
        write_above("\n".join(lines))


# ===========================================================================
# key readers (blocking-free polling so stop/shutdown stays responsive)
# ===========================================================================
_WIN_SPECIAL = {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT", "S": "DEL", "G": "HOME", "O": "END"}
_CSI_SPECIAL = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}
_CSI_TILDE = {"1": "HOME", "3": "DEL", "4": "END"}


def make_key_reader(msvcrt_mod=None, platform: str | None = None):
    """A poll_key(timeout) -> str|None callable, or None when unavailable.

    Returned tokens: single characters ("a", "你", "\\r", "\\x03" for Ctrl-C,
    "\\x08"/"\\x7f" backspace, "ESC" for a bare Esc) or "UP"/"DOWN"/"LEFT"/
    "RIGHT"/"HOME"/"END"/"DEL"/"IGN".

    Arrow keys arrive in TWO encodings on Windows: the legacy console sends
    \\x00/\\xe0 + a code byte, while terminals in VT input mode (Windows
    Terminal / ConPTY hosts) send ANSI CSI sequences (\\x1b[A ...). Both are
    decoded. `msvcrt_mod`/`platform` exist for tests.
    """
    if (platform or os.name) == "nt":
        try:
            msvcrt = msvcrt_mod or __import__("msvcrt")

            def _next(deadline: float) -> str | None:
                while True:
                    if msvcrt.kbhit():
                        return msvcrt.getwch()
                    if time.monotonic() >= deadline:
                        return None
                    time.sleep(0.002)

            pending: list[str] = []  # char read ahead during ESC disambiguation

            def poll_key(timeout: float = 0.05):
                if pending:
                    return pending.pop(0)
                ch = _next(time.monotonic() + timeout)
                if ch is None:
                    return None
                if ch in ("\x00", "\xe0"):  # legacy enhanced-key prefix
                    nxt = _next(deadline=time.monotonic() + 0.03)
                    return _WIN_SPECIAL.get(nxt, "IGN") if nxt else "IGN"
                if ch == "\x1b":  # VT input mode: CSI sequence or bare Esc
                    nxt = _next(time.monotonic() + 0.03)
                    if nxt is None:
                        return "ESC"
                    if nxt != "[":  # e.g. ESC+x alt-style: keep the x
                        pending.append(nxt)
                        return "ESC"
                    fin = _next(time.monotonic() + 0.03)
                    if fin in _CSI_SPECIAL:
                        return _CSI_SPECIAL[fin]
                    if fin in _CSI_TILDE:
                        _next(time.monotonic() + 0.02)  # consume the '~'
                        return _CSI_TILDE[fin]
                    return "IGN"
                return ch

            return poll_key

        except Exception:
            return None

    try:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        arrows = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "UP", "F": "DOWN"}
        pending = bytearray()

        def _read(deadline: float) -> str | None:
            while True:
                r, _, _ = select.select([fd], [], [], 0.01)
                if r:
                    pending.extend(os.read(fd, 1))
                    try:
                        return pending.decode("utf-8")
                    except UnicodeDecodeError:
                        continue  # wait for the rest of a multibyte char
                if time.monotonic() >= deadline:
                    return None

        def poll_key(timeout: float = 0.05):
            deadline = time.monotonic() + timeout
            ch = _read(deadline)
            if ch is None:
                return None
            if ch != "\x1b":
                return ch
            nxt = _read(time.monotonic() + 0.02)
            if nxt == "[":
                code = _read(time.monotonic() + 0.02)
                return arrows.get(code or "", "IGN")
            return "ESC"

        poll_key.restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)  # type: ignore[attr-defined]
        return poll_key

    except Exception:
        return None


# ===========================================================================
# line editor (pure logic; getkey injectable for tests)
# ===========================================================================
class LineEditor:
    def __init__(self, getkey, history: list[str]) -> None:
        self.getkey = getkey
        self.history = history

    def readline(self, stop_check) -> str | None:
        """Read one line. Returns the submitted text, or None when the user
        quit (Ctrl-C on an empty line) or stop_check() became true."""
        buf = ""
        saved = ""
        idx = len(self.history)  # history cursor; == len -> "live" buffer
        while not stop_check():
            key = self.getkey()
            if key is None:
                continue
            if key in ("\r", "\n"):
                return buf
            if key == "UP":
                if idx > 0:
                    if idx == len(self.history):
                        saved = buf
                    idx -= 1
                    buf = self.history[idx]
                    update_buffer(buf)
                continue
            if key == "DOWN":
                if idx < len(self.history):
                    idx += 1
                    buf = self.history[idx] if idx < len(self.history) else saved
                    update_buffer(buf)
                continue
            if key in ("\x08", "\x7f"):
                buf = buf[:-1]
            elif key == "\x03":  # Ctrl-C
                if buf:
                    buf = ""
                else:
                    return None  # empty line Ctrl-C = quit/stop
            elif key in ("\x1b", "ESC", "\x15"):  # Esc / Ctrl-U: clear the line
                buf = ""
            elif key in ("IGN", "LEFT", "RIGHT", "DEL", "HOME", "END", "\t"):
                continue
            else:
                buf += key
            update_buffer(buf)
        return None


def load_history(path) -> list[str]:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return [l for l in lines if l.strip()][-500:]
    except Exception:
        return []


def save_history(path, history: list[str]) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(history[-500:]) + "\n", encoding="utf-8")
    except Exception:
        log.debug("history save failed", exc_info=True)


# ===========================================================================
# command execution (shared by both fronts)
# ===========================================================================
async def handle_line(service: ControlService, ctx: ControlContext, line: str) -> str:
    """Execute one console line. Returns 'continue' | 'close' | 'shutdown'."""
    line = (line or "").strip()
    if line.startswith(".") and len(line) > 1:  # "." is an alias for "/"
        line = "/" + line[1:]
    if not line:
        return "continue"
    if line in ("/exit", "/quit", "/q"):
        write_above("console closed (service keeps running)")
        return "close"
    if line == "/shutdown":
        return "shutdown"
    try:
        outcome = await service.execute(line, ctx)
    except Exception as e:
        write_above(f"[x] {e}")
        return "continue"
    print_outcome(outcome)
    return "continue"


class InstanceSwitch:
    """Tracks the currently-controlled instance in multi-account mode."""

    def __init__(self, service: ControlService, registry: dict | None = None):
        self.registry = registry or {}
        self.current = service
        if registry:
            self.name = next((n for n, s in registry.items() if s is service), "?")
        else:
            self.name = ""
        set_current_prompt(self.prompt())

    @property
    def multi(self) -> bool:
        return bool(self.registry)

    def prompt(self) -> str:
        return f"muze[{self.name}]> " if self.multi else "muze> "

    def handle_use(self, line: str) -> str:
        """Returns output text if the line was /use, else ''."""
        parts = line.strip().split()
        if not parts or parts[0].lower() != "/use":
            return ""
        if len(parts) == 1:
            listing = "\n".join(
                f"- {n}{' (当前)' if n == self.name else ''}" for n in sorted(self.registry)
            )
            return f"实例列表:\n{listing}\n/use <name> 切换"
        target = parts[1]
        svc = self.registry.get(target)
        if svc is None:
            return f"实例 {target} 不存在；可用: {', '.join(sorted(self.registry))}"
        self.current = svc
        self.name = target
        set_current_prompt(self.prompt())
        return f"已切换到实例 {target}（提示符 muze[{target}]>）"


def _run_on_loop(loop, service, ctx, line) -> str:
    fut = asyncio.run_coroutine_threadsafe(handle_line(service, ctx, line), loop)
    try:
        return fut.result(timeout=600)
    except Exception as e:
        write_above(f"[x] {e}")
        return "continue"


def _console_line(loop, switcher, ctx, line) -> str:
    use_out = switcher.handle_use(line)
    if use_out:
        write_above(use_out)
        return "continue"
    action = _run_on_loop(loop, switcher.current, ctx, line)
    # /help is auto-generated from the @slash registry, which cannot see the
    # console-front built-ins — append them so they're discoverable here.
    low = line.strip().lower()
    if low in ("/help", ".help", "help") and action == "continue":
        extra = ["", "console 专属（仅本控制台，不在 /help 注册表）:"]
        if switcher.multi:
            extra.append(f"  /use <name> 切换实例（当前 {switcher.name}，/use 列出全部）")
        extra.append("  /exit|/quit|/q 关闭控制台（服务继续运行）")
        write_above("\n".join(extra))
    return action


def _submit_and_run(loop, switcher, ctx, stop_event, line: str, prompt: str) -> str | None:
    """Settle the command line into the scrollback, run it, handle actions."""
    write_above(prompt + line)
    action = _console_line(loop, switcher, ctx, line)
    if action == "shutdown":
        if stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
        write_above("shutting down...")
        return "shutdown"
    if action == "close":
        return "close"
    return "continue"


# ===========================================================================
# serve-embedded console (daemon thread)
# ===========================================================================
def repl_thread(
    loop: asyncio.AbstractEventLoop,
    service: ControlService,
    ctx: ControlContext,
    history_path,
    stop_event: asyncio.Event,
    registry: dict | None = None,
) -> None:
    switcher = InstanceSwitch(service, registry)
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    reader = make_key_reader() if tty else None
    write_above(BANNER)
    if switcher.multi:
        write_above(f"多实例模式: {', '.join(sorted(switcher.registry))}，/use <name> 切换")
    history = load_history(history_path)
    try:
        if reader is not None:
            enable_ansi()
            editor = LineEditor(reader, history)
            while not stop_event.is_set():
                show_input(switcher.prompt())
                line = editor.readline(stop_event.is_set)
                hide_input()
                if line is None:
                    if stop_event.is_set():
                        return
                    # Ctrl-C on an empty line: graceful stop, like Ctrl-C in fg
                    loop.call_soon_threadsafe(stop_event.set)
                    write_above("shutting down...")
                    return
                if not line.strip():
                    continue
                if history and history[-1] != line:
                    history.append(line)
                elif not history:
                    history.append(line)
                save_history(history_path, history)
                action = _submit_and_run(loop, switcher, ctx, stop_event, line, switcher.prompt())
                if action in ("shutdown", "close"):
                    return
        else:
            _plain_loop(loop, switcher, ctx, stop_event)
    except Exception:
        log.exception("console thread crashed")
    finally:
        hide_input()
        restore = getattr(reader, "restore", None)
        if callable(restore):
            try:
                restore()
            except Exception:
                pass


def _plain_loop(loop, switcher, ctx, stop_event) -> None:
    """Non-tty stdin (piped / hidden): plain input(), no sticky line."""
    failures = 0
    while not stop_event.is_set():
        try:
            sys.stdout.write(switcher.prompt())
            sys.stdout.flush()
            line = input()
        except KeyboardInterrupt:
            loop.call_soon_threadsafe(stop_event.set)
            return
        except EOFError:
            log.info("console stdin closed; service keeps running")
            return
        except Exception:
            failures += 1
            if failures >= 3:
                log.warning("console read failing repeatedly; console closed")
                return
            continue
        action = _submit_and_run(loop, switcher, ctx, stop_event, line, switcher.prompt())
        if action in ("shutdown", "close"):
            return


# ===========================================================================
# standalone cli console
# ===========================================================================
async def repl_async(
    service: ControlService,
    ctx: ControlContext,
    history_path,
    on_shutdown=None,
) -> None:
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    reader = make_key_reader() if tty else None
    write_above(BANNER)
    history = load_history(history_path)
    if reader is not None:
        enable_ansi()
        editor = LineEditor(reader, history)
        while True:
            show_input("muze> ")
            line = await asyncio.to_thread(editor.readline, lambda: False)
            hide_input()
            if line is None:  # Ctrl-C on an empty line: exit the cli
                write_above("bye")
                break
            if not line.strip():
                continue
            if not history or history[-1] != line:
                history.append(line)
            save_history(history_path, history)
            action = await handle_line(service, ctx, line)
            if action == "shutdown":
                if on_shutdown is not None:
                    on_shutdown()
                break
            if action == "close":
                break
        restore = getattr(reader, "restore", None)
        if callable(restore):
            try:
                restore()
            except Exception:
                pass
        return

    while True:
        try:
            line = await asyncio.to_thread(input)
        except (EOFError, KeyboardInterrupt):
            break
        action = await handle_line(service, ctx, line)
        if action == "shutdown":
            if on_shutdown is not None:
                on_shutdown()
            break
        if action == "close":
            break
