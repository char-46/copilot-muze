"""Restart continuity for agent sessions (alpha.143).

/restart and wheel updates used to kill running turns outright -- every
token of the in-flight conversation was wasted. Now shutdown checkpoints
RUNNING sessions as restart-marked suspends and the next boot auto-resumes
exactly those (user-paused suspends stay paused)."""

import pytest

from muze.ai.agent import Agent
from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.store.db import open_db
from muze.store.repo import Repository


@pytest.fixture
async def svc(tmp_path):
    conn = await open_db(str(tmp_path / "r.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)

    class NoAdapter:
        pass

    service = ControlService(cfg, NoAdapter(), repo, ChatLog(), Runtime())
    yield service, repo
    await conn.close()


def mk_agent(svc):
    service, repo = svc
    return Agent(service, repo, None, ChatLog())


async def test_checkpoint_marks_running_sessions(svc):
    _, repo = svc
    agent = mk_agent(svc)
    s1 = agent.sessions.create("qq:group:123", "查一下运动会照片")
    s2 = agent.sessions.create("qq:group:123", "已完成的任务")
    s2.finish(True, "done")
    await agent._persist(s2)

    n = await agent.checkpoint()
    assert n == 1  # only the running one
    row = await repo.get_ai_session(s1.id)
    assert row["status"] == "suspend"
    assert "restart-ckpt" in row["events_json"]
    row2 = await repo.get_ai_session(s2.id)
    assert row2["status"] == "done"  # untouched


async def test_auto_resume_takes_only_restart_marked(svc):
    service, repo = svc
    agent = mk_agent(svc)
    resumed = []

    def fake_resume(session, ctx, extra_text=None):
        resumed.append((session.id, ctx))

    agent.resume = fake_resume

    # (a) restart-marked suspend -> resumed, ctx rebuilt from chat_key
    a = agent.sessions.create("qq:group:123", "被重启打断")
    a.status = "suspend"
    a.event(agent.CHECKPOINT_EVENT, "进程重启，已保存断点")
    await agent._persist(a)
    # (b) user-paused suspend -> stays paused
    b = agent.sessions.create("qq:group:123", "用户手动暂停")
    b.status = "suspend"
    b.event("suspend", "第 3 轮暂停")
    await agent._persist(b)
    # (c) zombie 'running' row from a hard-killed process -> resumed too
    c = agent.sessions.create("qq:friend:45", "硬退出中断")
    c.status = "running"
    await agent._persist(c)
    # (d) non-QQ origin: resumable via /sess, but no QQ push
    d = agent.sessions.create("web:ui", "webui 会话")
    d.status = "suspend"
    d.event(agent.CHECKPOINT_EVENT, "进程重启，已保存断点")
    await agent._persist(d)

    n = await agent.auto_resume()
    assert n == 2  # (a) and (c) only
    ids = {sid for sid, _ in resumed}
    assert ids == {a.id, c.id}
    for sid, ctx in resumed:
        assert ctx.source == "qq" and ctx.peer_id in (123, 45)
    # the zombie got the marker persisted
    row_c = await repo.get_ai_session(c.id)
    assert "restart-ckpt" in row_c["events_json"]


async def test_ctx_for_parses_chat_keys(svc):
    agent = mk_agent(svc)
    s = agent.sessions.create("qq:group:1075918752", "x")
    ctx = agent._ctx_for(s)
    assert ctx is not None and ctx.scene == "group" and ctx.peer_id == 1075918752
    s2 = agent.sessions.create("qq:friend:42", "y")
    ctx2 = agent._ctx_for(s2)
    assert ctx2 is not None and ctx2.scene == "friend" and ctx2.peer_id == 42
    s3 = agent.sessions.create("cli:console", "z")
    assert agent._ctx_for(s3) is None
