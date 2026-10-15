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
import os
import sys
from pathlib import Path

from muze.config import TunnelConfig, TunnelForward

log = logging.getLogger("muze.tunnel")

RESTART_DELAY_MIN = 5.0
RESTART_DELAY_MAX = 60.0
STABLE_AFTER = 600.0  # a connection that lived this long resets the backoff


class SshTunnel:
    """Keeps one `ssh -N -R ...` child alive for the process lifetime.

    By default it forwards just the webui; a `forwards:` table can add more
    services of this box (e.g. sshd for dev shells through the relay).
    """

    def __init__(self, cfg: TunnelConfig, web_port: int, data_dir: Path):
        if not cfg.host:
            raise ValueError("web.tunnel.enabled but web.tunnel.host is empty")
        self.cfg = cfg
        self.web_port = web_port
        self.forwards: list[TunnelForward] = list(cfg.forwards) or [
            TunnelForward(local_port=web_port, remote_port=cfg.remote_port or web_port)
        ]
        remotes = [f.remote_port for f in self.forwards]
        if len(remotes) != len(set(remotes)):
            raise ValueError(f"web.tunnel.forwards: duplicate remote ports {remotes}")
        self.key_file = Path(cfg.key_file) if cfg.key_file else data_dir / ".ssh" / "muze_tunnel"
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._restarts = 0
        self._last_err = ""
        self._connected_at: float | None = None

    # ------------------------------------------------------------- status
    def status(self) -> dict:
        alive = self._proc is not None and self._proc.returncode is None
        import time as _time

        return {
            "host": f"{self.cfg.user}@{self.cfg.host}:{self.cfg.port}",
            "connected": alive,
            "uptime_sec": (
                round(_time.time() - self._connected_at, 1)
                if alive and self._connected_at else 0.0
            ),
            "restarts": self._restarts,
            "last_error": self._last_err[:160],
            "forwards": [
                f"{self.cfg.remote_host}:{f.remote_port} -> {f.local_host}:{f.local_port}"
                for f in self.forwards
            ],
        }

    async def restart(self) -> None:
        log.info("ssh tunnel: manual restart requested")
        await self.stop()
        self._stopping = False
        self._restarts = 0
        await self.start()

    def command(self) -> list[str]:
        if not self.key_file.is_file():
            raise FileNotFoundError(
                f"tunnel key not found: {self.key_file} — the relay is "
                "configured ONCE with a shared forward-only key; copy that "
                "private key to this path (generating a new one would need "
                "a fresh relay-side authorized_keys line: "
                'restrict,port-forwarding,command="/bin/false",'
                'permitopen="127.0.0.1:1",permitlisten="127.0.0.1:*" '
                "<pubkey> — forward-only, no shell, no pty, no pivot, "
                "loopback-only binds; each box claims its own remote_port "
                "range so tunnels never collide)"
            )
        self._tighten_key_acl()
        cmd = [
            "ssh", "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            # aggressive liveness: detect a dead peer in <=60s so the relay
            # side (ClientAlive) and this side tear down together — without
            # this a half-open connection keeps the -R ports held hostage
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-o", "TCPKeepAlive=yes",
            "-o", "ConnectTimeout=15",
            "-i", str(self.key_file),
            "-p", str(self.cfg.port),
        ]
        for f in self.forwards:
            cmd += [
                "-R",
                f"{self.cfg.remote_host}:{f.remote_port}:{f.local_host}:{f.local_port}",
            ]
        cmd.append(f"{self.cfg.user}@{self.cfg.host}")
        return cmd

    def _tighten_key_acl(self) -> None:
        """Windows OpenSSH refuses private keys readable by other accounts
        ("Bad permissions ... UNPROTECTED PRIVATE KEY FILE"). A key copied
        from elsewhere inherits group ACEs (Authenticated Users etc.), so
        strip inheritance and grant only the current user read access —
        exactly the manual icacls dance every new box would otherwise hit."""
        if sys.platform != "win32":
            return
        import subprocess

        try:
            r = subprocess.run(
                ["icacls", str(self.key_file)], capture_output=True,
                text=True, timeout=10,
            )
            acl = r.stdout or ""
            too_open = (
                "Authenticated Users" in acl
                or "Everyone" in acl
                or "\\Users" in acl
            )
            if not too_open:
                return
            user = os.environ.get("USERNAME") or ""
            if not user:
                return
            log.info("tunnel key %s ACL too open; restricting to %s",
                     self.key_file.name, user)
            # NB: /inheritance:r only strips INHERITED aces; copy tools and
            # default folder templates leave EXPLICIT group aces behind, so
            # also remove the usual suspects by SID (locale-proof):
            # Authenticated Users S-1-5-11, Users S-1-5-32-545, Everyone
            # S-1-1-0. Administrators/SYSTEM may stay (ssh accepts those).
            for args in (
                ["icacls", str(self.key_file), "/inheritance:r"],
                ["icacls", str(self.key_file), "/remove:g",
                 "*S-1-5-11", "*S-1-5-32-545", "*S-1-1-0"],
                ["icacls", str(self.key_file), "/grant:r", f"{user}:R"],
            ):
                subprocess.run(args, capture_output=True, timeout=10, check=False)
        except Exception as e:  # best effort: ssh will report if still bad
            log.warning("key ACL tightening failed (%s); ssh may reject the key", e)

    async def start(self) -> None:
        cmd = self.command()  # fail fast on missing key/config
        for f in self.forwards:
            log.info(
                "ssh tunnel: relay %s:%s <- %s:%s", self.cfg.host, f.remote_port,
                f.local_host, f.local_port,
            )
        self._task = asyncio.create_task(self._supervise(cmd))

    async def _supervise(self, cmd: list[str]) -> None:
        import time as _time

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
                self._last_err = f"cannot spawn ssh: {e}"
                return
            self._connected_at = _time.time()
            rc = await self._proc.wait()
            if self._stopping:
                return
            lived = asyncio.get_running_loop().time() - started
            if lived >= STABLE_AFTER:
                delay = RESTART_DELAY_MIN
            self._restarts += 1
            stderr_tail = ""
            if self._proc.stderr is not None:
                try:
                    err = await asyncio.wait_for(self._proc.stderr.read(), timeout=5)
                    stderr_tail = err.decode(errors="replace").strip().splitlines()[-1:]\
                        [0][:200] if err.strip() else ""
                except Exception:
                    pass
            self._last_err = stderr_tail or f"rc={rc}"
            # a remote port held by a relay-side zombie (dead peer whose
            # connection the relay hasn't reaped yet) frees itself within the
            # relay's ClientAlive window — retry fast instead of backing off
            port_conflict = "forwarding failed" in stderr_tail or "already in use" in stderr_tail
            if port_conflict:
                delay = 15.0
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
