"""Daemon supervisor: passthrough args, interruptible waits, child stop."""

import os
import subprocess
import sys
import time

import muze.daemon as daemon_mod
from muze.daemon import Supervisor, _make_kill_on_close_job, parse_args

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def test_parse_args_default_and_passthrough():
    a = parse_args([])
    assert a.config == "config.yaml" and a.serve_args == []
    a = parse_args(["-c", "my.yaml", "--", "--account", "main"])
    assert a.config == "my.yaml"
    assert a.serve_args == ["--account", "main"]


def test_sleep_returns_immediately_when_stop_requested():
    sup = Supervisor(["nope"])
    sup.stop_requested = True
    t0 = time.monotonic()
    sup._sleep(30)
    assert time.monotonic() - t0 < 1  # did NOT sleep


def test_wait_polling_never_blocks_on_stop_request():
    """The old daemon blocked forever in wait(); the new one must notice the
    stop flag within one poll interval."""
    sup = Supervisor([sys.executable, "-c", "import time; time.sleep(60)"])
    sup.proc = subprocess.Popen(sup.serve_cmd, creationflags=CREATE_NEW_PROCESS_GROUP)
    sup.stop_requested = True
    t0 = time.monotonic()
    rc = sup._wait_polling()
    assert rc is None and time.monotonic() - t0 < 3
    sup.proc.kill()
    sup.proc.wait()


def test_stop_child_escalates_to_kill(monkeypatch):
    monkeypatch.setattr(daemon_mod, "GRACEFUL_STOP_TIMEOUT", 0.5)
    monkeypatch.setattr(daemon_mod, "KILL_TIMEOUT", 2.0)
    # a child that ignores graceful stops as hard as an OS allows
    code = "import signal, time; signal.signal(signal.SIGBREAK, signal.SIG_IGN) " \
           "if hasattr(signal, 'SIGBREAK') else None; time.sleep(60)"
    sup = Supervisor([sys.executable, "-c", code])
    sup.proc = subprocess.Popen(sup.serve_cmd, creationflags=CREATE_NEW_PROCESS_GROUP)
    t0 = time.monotonic()
    sup._stop_child()
    assert sup.proc.poll() is not None  # dead either way
    assert time.monotonic() - t0 < 8  # escalated instead of hanging


def test_job_object_created_on_windows():
    if os.name != "nt":
        return
    h = _make_kill_on_close_job()
    assert h is not None
    import ctypes

    ctypes.windll.kernel32.CloseHandle(h)


def test_spawn_assigns_job_and_env(tmp_path):
    marker = tmp_path / "env.txt"
    code = (
        "import os; "
        f"open(r'{marker}', 'w').write("
        "os.environ.get('MUZE_SUPERVISED','') + ' ' + "
        "os.environ.get('MUZE_SUPERVISOR_PID',''))"
    )
    sup = Supervisor([sys.executable, "-c", code])
    if os.name == "nt":
        sup._job = _make_kill_on_close_job()
    proc = sup._spawn()
    try:
        for _ in range(40):  # up to ~4s for the child to write the marker
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists()
        assert marker.read_text().strip() == f"1 {os.getpid()}"
    finally:
        proc.kill()
        proc.wait()
        if sup._job:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(sup._job)
