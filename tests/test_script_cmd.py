"""QQ-driven script runner: /script <name.ps1> (and the /script:name colon
form), /scriptlog, /full-restart (alpha.144). Scripts run detached in their
own process group so lifecycle scripts (start.ps1 -Restart) can stop the
service without killing their own runner."""

import pytest

from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.chatlog import ChatLog
from muze.store.db import open_db
from muze.store.repo import Repository


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    conn = await open_db(str(tmp_path / "s.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)

    class NoAdapter:
        pass

    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())

    class FakeProc:
        def wait(self):
            import time as _t

            _t.sleep(3600)  # "still running"

    spawned = []

    def fake_spawn(script, args, log_path):
        spawned.append((str(script), list(args), str(log_path)))
        return FakeProc()

    watched = []

    async def fake_watch(*a, **k):
        watched.append(a)

    monkeypatch.setattr(service, "_spawn_script", fake_spawn)
    monkeypatch.setattr(service, "_watch_script", fake_watch)
    yield service, spawned
    await conn.close()


def ctx(service):
    return ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)


async def test_script_colon_and_space_forms_spawn_detached(svc):
    service, spawned = svc
    out = await service.run_command("script:start.ps1 -Restart", ctx(service))
    assert not out.is_error and "已分离运行" in out.text
    assert spawned and spawned[0][0].endswith("start.ps1") and spawned[0][1] == ["-Restart"]

    out = await service.run_command("script start.ps1", ctx(service))
    assert not out.is_error and len(spawned) == 2


async def test_script_rejects_traversal_and_missing(svc):
    service, spawned = svc
    out = await service.run_command("script ..\\config.yaml", ctx(service))
    assert out.is_error and "非法脚本名" in out.text
    out = await service.run_command("script nope.ps1", ctx(service))
    assert out.is_error and "不存在" in out.text
    assert spawned == []  # nothing ever spawned


async def test_script_listing(svc):
    service, _ = svc
    out = await service.run_command("script", ctx(service))
    assert not out.is_error and "start.ps1" in out.text  # repo scripts dir


async def test_full_restart_prefers_scheduled_task(svc, monkeypatch):
    """schtasks working -> the restart runs OUTSIDE the daemon's job (the
    breakaway runner proved unreliable under nested jobs, live 2026-10-26)."""
    import subprocess as _sp

    from muze.core import service as svc_mod

    service, spawned = svc
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv[1])
        import types

        r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return r

    monkeypatch.setattr(svc_mod.subprocess, "run", fake_run)
    out = await service.run_command("full-restart", ctx(service))
    assert not out.is_error and "计划任务" in out.text
    assert calls == ["/Create", "/Run"]  # task created then fired
    assert spawned == []  # no in-process fallback needed
    # the task action launches a WINDOWLESS wscript launcher, not the .cmd
    vbs = service._script_logs_dir() / "fullrestart-run.vbs"
    body = vbs.read_text(encoding="utf-8", errors="replace")
    assert "WScript.Shell" in body and ", 0, False" in body and "start.ps1" in body


async def test_full_restart_falls_back_when_schtasks_fails(svc, monkeypatch):
    import subprocess as _sp
    import types

    from muze.core import service as svc_mod

    service, spawned = svc

    def fake_run(argv, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no")

    monkeypatch.setattr(svc_mod.subprocess, "run", fake_run)
    out = await service.run_command("full-restart", ctx(service))
    assert not out.is_error and "分离进程" in out.text
    assert spawned and spawned[0][1] == ["-Restart"]


async def test_scriptlog_reads_tail(svc, tmp_path):
    service, _ = svc
    d = service._script_logs_dir()
    (d / "demo-20260101-000000.log").write_text("x" * 2000 + "TAIL-HERE", encoding="utf-8")
    out = await service.run_command("scriptlog demo", ctx(service))
    assert not out.is_error and "TAIL-HERE" in out.text and "demo-" in out.text


async def test_spawn_script_breakaway_with_fallback(tmp_path, monkeypatch):
    """The runner asks to break away from the daemon's job (so it survives
    KILL_ON_JOB_CLOSE during /full-restart); an old daemon that forbids it
    falls back to a plain spawn."""
    import subprocess as _sp

    from muze.core import service as svc_mod

    brk = getattr(_sp, "CREATE_BREAKAWAY_FROM_JOB", 0)
    grp = getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0)
    if not brk:
        pytest.skip("breakaway flag not on this platform")
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)

    class NoAdapter:
        pass

    service = ControlService(cfg, NoAdapter(), None, ChatLog(), Runtime())
    calls = []

    class FakePopen:
        def __init__(self, argv, creationflags=0, **kw):
            calls.append(creationflags)
            if creationflags & brk:
                raise OSError(5, "access denied (job without BREAKAWAY_OK)")

    monkeypatch.setattr(svc_mod.subprocess, "Popen", FakePopen)
    service._spawn_script(
        tmp_path / "x.ps1", [], tmp_path / "x.log"
    )
    assert len(calls) == 2
    assert calls[0] & brk and not (calls[1] & brk)  # breakaway, then fallback
    assert calls[1] & grp  # still its own process group
    no_win = getattr(_sp, "CREATE_NO_WINDOW", 0)
    if no_win:
        assert calls[0] & no_win and calls[1] & no_win  # never a visible console

