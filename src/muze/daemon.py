"""muze daemon: external supervisor for `muze serve`.

Why a supervisor: code updates need a process restart; `/reload` and
`/restart` make serve exit with RESTART_EXIT (=100) and the daemon brings a
fresh process up immediately, so `git pull && /reload` is a complete update.
Crashes get exponential backoff (5s -> 60s, reset after a stable run).

Design rules (learned the hard way on Windows):

1. NEVER block in an uninterruptible wait. `Popen.wait()` without a timeout
   parks the main thread inside WaitForSingleObject where Python signal
   handlers cannot run — the daemon then ignores Ctrl-C forever. Everything
   here polls with short timeouts.
2. Signal handlers only set a flag; the supervised loop does the work.
   Raising SystemExit from a handler is fragile (it fires at a random
   bytecode boundary, often mid-relaunch).
3. The child runs in its own process group (Windows) so console Ctrl-C hits
   the daemon alone, which forwards a graceful stop to serve.
4. On Windows the child is placed in a Job Object with
   KILL_ON_JOB_CLOSE: if the daemon dies for ANY reason (taskkill /F,
   crash, logoff) the kernel kills serve instantly — an orphaned serve
   holding the web port can no longer happen. (serve additionally polls
   the supervisor pid as a second layer.)
5. The serve command line is passed through verbatim (see `--`), so any
   extra serve flags survive restarts.
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
INITIAL_BACKOFF = 5.0
MAX_BACKOFF = 60.0
POLL_INTERVAL = 0.5  # wait() slices: keeps signal handling responsive
GRACEFUL_STOP_TIMEOUT = 20.0  # give serve this long to exit gracefully
KILL_TIMEOUT = 10.0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="muze daemon",
        description="supervise muze serve (restart on /reload, /restart, crash)",
        epilog="extra serve arguments may follow `--`, e.g. "
               "muze daemon -c config.yaml -- --account main",
    )
    p.add_argument("-c", "--config", default="config.yaml",
                   help="path to config.yaml (passed to serve)")
    p.add_argument("serve_args", nargs="*",
                   help="additional arguments forwarded to serve (after --)")
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# Windows: job object so the child dies with the daemon, always
# --------------------------------------------------------------------------
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JBLI(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(wintypes.ULONG)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JELI(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JBLI),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _k32 = ctypes.windll.kernel32

    def _make_kill_on_close_job():
        """Job handle the OS closes (and kills) when this process dies."""
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _JELI()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
            h, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            _k32.CloseHandle(h)
            return None
        return h

    def _assign_to_job(job_handle, popen: subprocess.Popen) -> bool:
        """Put a (suspended) child into the job, then resume it."""
        if not job_handle:
            return False
        ok = _k32.AssignProcessToJobObject(job_handle, popen._handle)
        return bool(ok)
else:
    def _make_kill_on_close_job():
        return None

    def _assign_to_job(job_handle, popen) -> bool:
        return False


class Supervisor:
    def __init__(self, serve_cmd: list[str]) -> None:
        self.serve_cmd = serve_cmd
        self.proc: subprocess.Popen | None = None
        self.stop_requested = False
        self._job = None
        self.attempts = 0

    # ------------------------------------------------------------- signals
    def install_signal_handlers(self) -> None:
        def _flag(_signum=None, _frame=None) -> None:
            self.stop_requested = True

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None),
                    getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _flag)
            except (ValueError, OSError):
                pass

    # ------------------------------------------------------------- helpers
    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep: returns early once a stop was requested."""
        deadline = time.monotonic() + seconds
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(POLL_INTERVAL, remaining))

    def _spawn(self) -> subprocess.Popen:
        creationflags = 0
        if os.name == "nt":
            # own process group: console Ctrl-C reaches the daemon only;
            # the daemon forwards a graceful stop (CTRL_BREAK) to serve
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        env = {
            **os.environ,
            "MUZE_SUPERVISED": "1",
            "MUZE_SUPERVISOR_PID": str(os.getpid()),
        }
        proc = subprocess.Popen(
            self.serve_cmd, env=env, creationflags=creationflags
        )
        if self._job is not None:
            _assign_to_job(self._job, proc)
        return proc

    def _wait_polling(self) -> int | None:
        """Wait for the child, but return None as soon as a stop is requested
        (so signal handling can never be starved by a blocking wait)."""
        assert self.proc is not None
        while True:
            try:
                return self.proc.wait(timeout=POLL_INTERVAL)
            except subprocess.TimeoutExpired:
                if self.stop_requested:
                    return None

    def _stop_child(self) -> None:
        """Graceful stop, escalating to a hard kill."""
        p = self.proc
        if p is None or p.poll() is not None:
            return
        log.info("stopping serve (pid %s)...", p.pid)
        try:
            if os.name == "nt":
                p.send_signal(signal.CTRL_BREAK_EVENT)  # graceful SIGBREAK
            else:
                p.terminate()
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + GRACEFUL_STOP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                p.wait(timeout=POLL_INTERVAL)
                log.info("serve stopped cleanly")
                return
            except subprocess.TimeoutExpired:
                if p.poll() is not None:
                    return
        log.warning("serve did not stop in %.0fs; killing", GRACEFUL_STOP_TIMEOUT)
        try:
            p.kill()
            p.wait(timeout=KILL_TIMEOUT)
        except Exception:
            pass

    # ---------------------------------------------------------------- loop
    def run(self) -> int:
        self._job = _make_kill_on_close_job()
        if self._job is None and os.name == "nt":
            log.warning("job object unavailable; relying on serve's "
                        "supervisor-watchdog alone")
        log.info("daemon supervising: %s", " ".join(self.serve_cmd))
        backoff = INITIAL_BACKOFF
        try:
            while not self.stop_requested:
                self.attempts += 1
                started = time.monotonic()
                try:
                    self.proc = self._spawn()
                except OSError as e:
                    log.error("cannot start serve (%s); retrying in %.0fs",
                              e, backoff)
                    self._sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue

                rc = self._wait_polling()
                ran = time.monotonic() - started
                self.proc = None

                if rc is None:  # stop requested while serving
                    self._stop_child()
                    return 0
                if rc == 0:
                    log.info("serve exited cleanly; daemon exiting")
                    return 0
                if rc == RESTART_EXIT:
                    log.info("serve requested restart (rc=%d); relaunching "
                             "(attempt %d)", rc, self.attempts + 1)
                    backoff = INITIAL_BACKOFF
                    self._sleep(0.5)  # let the old listener release the port
                    continue
                if ran >= STABLE_SECONDS:
                    backoff = INITIAL_BACKOFF
                log.warning(
                    "serve exited rc=%s after %.0fs; restarting in %.0fs "
                    "(check the log above / muze.log for the cause)",
                    rc, ran, backoff,
                )
                self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            # stop requested between runs
            self._stop_child()
            return 0
        finally:
            self.proc = None


def run_daemon(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    serve_cmd = [sys.executable, "-m", "muze", "serve", "-c", args.config]
    if args.serve_args:
        serve_cmd += list(args.serve_args)
    sup = Supervisor(serve_cmd)
    sup.install_signal_handlers()
    try:
        return sup.run()
    except KeyboardInterrupt:  # belt & braces (e.g. handler missed pre-install)
        sup.stop_requested = True
        sup._stop_child()
        return 0


if __name__ == "__main__":
    raise SystemExit(run_daemon(sys.argv[1:]))
