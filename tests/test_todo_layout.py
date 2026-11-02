"""/todo readability (user feedback 2026-11-02): line-broken plain nodes,
table layout in the image, friendly origin labels instead of raw
friend/qq keys, and content-weighted table columns in the renderer."""

import datetime as _dtm

import pytest

from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.chatlog import ChatLog


class _NameAdapter:
    """Answers cached name lookups; nothing else."""

    def cached_friend_name(self, uid):
        return {10001: "张三"}.get(uid, "")

    def cached_group_name(self, gid):
        return {4242: "三班通知群"}.get(gid, "")

    async def ensure_friend_name(self, uid):
        return self.cached_friend_name(uid)

    async def ensure_group_name(self, gid):
        return self.cached_group_name(gid)


@pytest.fixture
async def svc(tmp_path):
    from muze.store.db import open_db
    from muze.store.repo import Repository

    conn = await open_db(str(tmp_path / "t.db"))
    repo = Repository(conn)
    cfg = MuzeConfig()
    cfg.storage.data_dir = str(tmp_path)
    service = ControlService(cfg, _NameAdapter(), repo, ChatLog(), Runtime())
    yield service, repo
    await conn.close()


def _cli():
    return ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)


def _due_in(days: int) -> str:
    return (_dtm.date.today() + _dtm.timedelta(days=days)).isoformat()


async def test_subject_label_prefers_cached_names(svc):
    service, _ = svc
    assert await service._subject_label("friend/10001") == "张三"
    assert await service._subject_label("group/4242") == "三班通知群"
    assert await service._subject_label("friend/99999") == "好友99999"
    assert await service._subject_label("") == "全局"
    assert await service._subject_label("随便标的") == "随便标的"


async def test_todo_markdown_is_a_table_with_friendly_labels(svc):
    from muze.digest import local_date

    service, repo = svc
    due = _due_in(30)
    await repo.add_followup(
        "friend/10001", "交推送初稿", "", "user", local_date(),
        assignee="张三", due_date=due,
    )
    out = await service.run_command("todo", _cli())
    assert "| # | 事项 | 负责人 | 状态 | 截止 |" in out.text
    assert f"| #1 | 交推送初稿 | 张三 | 等交付 | {due[5:]}（30天后） |" in out.text
    assert "friend/" not in out.text  # no raw keys in the image source
    assert "张三" in out.text


async def test_todo_plain_nodes_one_fact_per_line(svc):
    from muze.digest import local_date

    service, repo = svc
    due = _due_in(30)
    await repo.add_followup(
        "group/4242", "交推送初稿", "细节说明", "user", local_date(),
        assignee="张三", due_date=due,
    )
    out = await service.run_command("todo", _cli())
    node = next(n for n in out.forward_nodes if n.startswith("#1 交推送初稿\n"))
    lines = node.splitlines()
    assert lines[0] == "#1 交推送初稿"
    assert any(ln.strip().startswith("负责人 张三（等交付）") for ln in lines)
    assert any(f"截止 {due[5:]}（30天后）" in ln for ln in lines)
    assert any(ln.strip().startswith("来自 三班通知群") for ln in lines)
    assert all("group/" not in ln for ln in lines)


async def test_todo_node_layout_forces_the_forward_chain(svc, monkeypatch):
    """A short /todo must still ride as ONE merged forward: the
    one-item-per-bubble layout is the point, not the length."""
    from muze.digest import local_date

    service, repo = svc
    await repo.add_followup("", "唯一事项", "", "user", local_date())
    sent: list = []

    async def fake_send_text(scene, peer, text, *, preview="", nodes=None):
        sent.append((text, nodes))
        return True

    monkeypatch.setattr(service, "send_text", fake_send_text)
    out = await service.run_command("todo", _cli())
    ctx = ControlContext(source="qq", chat_key="qq:friend/1", scene="friend", peer_id=1)
    await service.deliver(ctx, out)
    text, nodes = sent[0]
    assert nodes and any("唯一事项" in n for n in nodes)


# ------------------------------------------------- renderer table columns
def test_table_columns_weighted_by_content():
    R = pytest.importorskip("muze.render")
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    from xml.etree import ElementTree as ET

    from muze.render import _styles, _table_flowable

    html = (
        "<table><tr><th>#</th><th>事项</th><th>负责人</th><th>状态</th><th>截止</th></tr>"
        "<tr><td>#1</td><td>这是一个相当长的事项标题会占据更多宽度</td>"
        "<td>张三</td><td>等交付</td><td>01-01</td></tr></table>"
    )
    t = _table_flowable(ET.fromstring(html), _styles("Helvetica"))
    widths = list(t._colWidths)
    assert widths[1] > widths[0] * 1.5  # content-heavy column wins
    assert widths[0] < widths[1]  # short columns stay narrow


def test_table_ragged_rows_are_padded_not_crashed():
    R = pytest.importorskip("muze.render")
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    from xml.etree import ElementTree as ET

    from muze.render import _styles, _table_flowable

    html = (
        "<table><tr><th>a</th><th>b</th><th>c</th></tr>"
        "<tr><td>1</td><td>2</td></tr>"  # short row (LLM tables do this)
        "<tr><td>1</td><td>2</td><td>3</td><td>4</td></tr>"  # long row
        "</table>"
    )
    t = _table_flowable(ET.fromstring(html), _styles("Helvetica"))
    assert all(len(row) == 4 for row in t._cellvalues)


def test_full_todo_markdown_renders_to_png(tmp_path):
    """End-to-end: the actual /todo table markdown must survive the whole
    md -> html -> pdf -> png pipeline (weighted columns, zebra rows)."""
    R = pytest.importorskip("muze.render")
    if not R.RENDER_OK:
        pytest.skip("render deps missing")
    md = (
        "# 未结待办（2 条，1 条已布置给他人）\n\n"
        "| # | 事项 | 负责人 | 状态 | 截止 |\n"
        "|---|---|---|---|---|\n"
        "| #1 | 交运动会推送初稿并附上配图说明 | 张三 | 等交付 | 01-01（还剩2天） |\n"
        "| #2 | └ 排版推送 | 我 | 进行中 | — |\n\n"
        "**最近完成**\n\n| 事项 | 结果 | 日期 |\n|---|---|---|\n"
        "| 交初稿 | 完成·docx+pdf已发 | 12-31 |\n"
    )
    png = R.render_png(md, tmp_path)
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
