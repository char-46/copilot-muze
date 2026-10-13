"""SSH reverse tunnel supervisor for remote debugging.

Forwards the local webui (loopback) through a relay box:

    dev machine --ssh -L--> relay:127.0.0.1:RPORT <--ssh -R-- this box --> webui

Both connections are *outbound ssh*; the relay opens no new public listener
(the forwarded port binds loopback on the relay). The dedicated key on the
relay must be restricted to `restrict,port-forwarding` so it can do nothing
but forward — the webui bearer token is the last layer.

Windows ships OpenSSH client at C:\\Windows\\System32\\OpenSSH\\ssh.exe; no
extra software is needed on this side, and the relay needs nothing but its
existing hardened sshd.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from muze.config import TunnelConfig

log = logging.getLogger("muze.tunnel")

RESTART_DELAY_MIN = 5.0
RESTART_DELAY_MAX = 60.0
STABLE_AFTER = 600.0  # a connection that lived this long resets the backoff


class SshTunnel:
    """Keeps one `ssh -N -R` child alive for the process lifetime."""

    def __init__(self, cfg: TunnelConfig, web_port: int, data_dir: Path):
        if not cfg.host:
            raise ValueError("web.tunnel.enabled but web.tunnel.host is empty")
        self.cfg = cfg
        self.web_port = web_port
        self.remote_port = cfg.remote_port or web_port
        self.key_file = Path(cfg.key_file) if cfg.key_file else data_dir / ".ssh" / "muze_tunnel"
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False

    def command(self) -> list[str]:
        if not self.key_file.is_file():
            raise FileNotFoundError(
                f"tunnel key not found: {self.key_file} — generate one "
                '(ssh-keygen -t ed25519 -N "" -f <key>) and install its '
                "public half on the relay as a single authorized_keys line "
                'like: restrict,port-forwarding,command="/bin/false",'
                'permitopen="127.0.0.1:1",permitlisten="127.0.0.1:<remote_port>" '
                "<pubkey> — forward-only, no shell, no pty, no pivot, and the "
                "relay bind pinned to one loopback port (must equal "
                "remote_port here)"
            )
        return [
            "ssh", "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15",
            "-i", str(self.key_file),
            "-p", str(self.cfg.port),
            "-R",
            f"{self.cfg.remote_host}:{self.remote_port}:127.0.0.1:{self.web_port}",
            f"{self.cfg.user}@{self.cfg.host}",
        ]

    async def start(self) -> None:
        cmd = self.command()  # fail fast on missing key/config
        log.info(
            "ssh tunnel: relay %s:%s <- 127.0.0.1:%s (webui)",
            self.cfg.host, self.remote_port, self.web_port,
        )
        self._task = asyncio.create_task(self._supervise(cmd))

    async def _supervise(self, cmd: list[str]) -> None:
        delay = RESTART_DELAY_MIN
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as e:  # e.g. ssh client missing
                log.error("ssh tunnel: cannot spawn ssh (%s); giving up", e)
                return
            rc = await self._proc.wait()
            if self._stopping:
                return
            lived = asyncio.get_running_loop().time() - started
            if lived >= STABLE_AFTER:
                delay = RESTART_DELAY_MIN
            stderr_tail = ""
            if self._proc.stderr is not None:
                try:
                    err = await asyncio.wait_for(self._proc.stderr.read(), timeout=5)
                    stderr_tail = err.decode(errors="replace").strip().splitlines()[-1:]\
                        [0][:200] if err.strip() else ""
                except Exception:
                    pass
            log.warning(
                "ssh tunnel exited rc=%s after %.0fs; retrying in %.0fs%s",
                rc, lived, delay, f" ({stderr_tail})" if stderr_tail else "",
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.sleep(delay)), timeout=delay + 1
                )
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RESTART_DELAY_MAX)

    async def stop(self) -> None:
        self._stopping = True
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("ssh tunnel stopped")
