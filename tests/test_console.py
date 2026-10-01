"""Console: sticky bottom input line + key-level line editor."""

import io
import logging
import sys

import pytest

from muze import console
from muze.chatlog import ConsoleStreamHandler
from muze.console import LineEditor


def run_keys(keys, history=None, stop=lambda: False):
    seq = list(keys)

    def getkey(timeout=0.05):
        return seq.pop(0) if seq else None

    return LineEditor(getkey, history or []).readline(stop)


# ------------------------------------------------------------- editor
def test_editor_types_and_submits():
    assert run_keys(["h", "e", "l", "l", "o", "\r"]) == "hello"


def test_editor_backspace_esc_ctrlu_cjk():
    assert run_keys(["你好", "\x08", "\r"]) == "你"
    assert run_keys(["abc", "\x1b", "x", "\r"]) == "x"      # Esc clears
    assert run_keys(["abc", "\x15", "\r"]) == ""             # Ctrl-U clears
    assert run_keys(["a", None, "b", "\r"]) == "ab"          # None = poll timeout


def test_editor_history_navigation():
    hist = ["one", "two"]
    assert run_keys(["z", "UP", "UP", "\r"], history=hist) == "one"
    assert run_keys(["z", "UP", "UP", "DOWN", "\r"], history=hist) == "two"
    # UP then DOWN past the end restores the live buffer
    assert run_keys(["z", "UP", "DOWN", "\r"], history=hist) == "z"
    # DOWN at the live buffer is a no-op
    assert run_keys(["z", "DOWN", "\r"], history=hist) == "z"


def test_editor_ctrl_c_quits_only_on_empty_line():
    assert run_keys(["\x03"]) is None          # quit
    assert run_keys(["a", "\x03", "\r"]) == "" # clear then submit empty


def test_editor_stop_check_interrupts():
    assert run_keys(["a", "b"], stop=lambda: True) is None


# --------------------------------------------------- sticky input line
def test_write_above_pins_input_to_last_line(monkeypatch):
    monkeypatch.setattr(console, "_vt", True)
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    console.show_input("muze> ", "abc")
    console.write_above("LOG LINE")
    out = fake.getvalue()
    assert out == "muze> abc" + "\r\x1b[2K" + "LOG LINE\n" + "muze> abc"
    console.hide_input()


def test_write_above_plain_when_no_input_line(monkeypatch):
    monkeypatch.setattr(console, "_vt", True)
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    console.hide_input()
    console.write_above("LOG LINE")
    assert fake.getvalue() == "LOG LINE\n"


def test_update_buffer_redraws(monkeypatch):
    monkeypatch.setattr(console, "_vt", True)
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    console.show_input("muze> ", "a")
    console.update_buffer("abc")
    assert fake.getvalue() == "muze> a" + "\r\x1b[2K" + "muze> abc"
    console.hide_input()


def test_log_handler_inserts_above_input(monkeypatch):
    monkeypatch.setattr(console, "_vt", True)
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    console.show_input("muze> ", "typing")
    h = ConsoleStreamHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("muze.test", logging.INFO, __file__, 1, "hello %s", ("log",), None)
    h.emit(rec)
    out = fake.getvalue()
    assert "hello log\n" in out
    assert out.endswith("muze> typing")  # input line is STILL the last line
    console.hide_input()


# ------------------------------------------------- windows key decoding
class FakeMsvcrt:
    """Scripted console input: each queued string is delivered atomically."""

    def __init__(self, chunks: list[str]) -> None:
        self.q = list(chunks)

    def kbhit(self) -> bool:
        return any(self.q)

    def getwch(self) -> str:
        for i, s in enumerate(self.q):
            if s:
                self.q[i] = s[1:]
                return s[0]
        raise AssertionError("getwch called with empty queue")


def win_reader(chunks: list[str]):
    return console.make_key_reader(msvcrt_mod=FakeMsvcrt(chunks), platform="nt")


def test_key_reader_decodes_csi_and_legacy_arrows():
    r = win_reader(["\x1b[A", "\x1b[B", "\x1b[C", "a"])  # VT input mode
    assert r() == "UP" and r() == "DOWN" and r() == "RIGHT"
    assert r() == "a"
    r = win_reader(["\xe0H", "\x00P", "b"])  # legacy enhanced keys
    assert r() == "UP" and r() == "DOWN"
    assert r() == "b"


def test_key_reader_bare_esc_and_tilde_codes():
    r = win_reader(["\x1b", "x"])
    assert r() == "ESC" and r() == "x"
    r = win_reader(["\x1b[3~", "y"])
    assert r() == "DEL" and r() == "y"
    r = win_reader(["\x1b[Z", "z"])  # unknown CSI -> ignored
    assert r() == "IGN" and r() == "z"


def test_editor_accepts_esc_token_history():
    # ESC token (from the reader) clears; UP/DOWN tokens drive history
    hist = ["one"]
    assert run_keys(["a", "ESC", "b", "\r"], history=hist) == "b"
    assert run_keys(["UP", "\r"], history=hist) == "one"
