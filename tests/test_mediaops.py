"""Media operations: chart rendering + agent tools (send_media, ocr, chart)."""

import json

import pytest

from muze.ai.tools import ToolRunner
from muze.chatlog import ChatLog
from muze.config import MuzeConfig
from muze.core.service import ControlContext, ControlService, Runtime
from muze.store.db import open_db
from muze.store.repo import Repository

from test_permissions import Recorder


@pytest.fixture
async def env(tmp_path):
    conn = await open_db(str(tmp_path / "m.db"))
    repo = Repository(conn)
    adapter = Recorder()
    cfg = MuzeConfig()
    cfg.authority = [10001]
    cfg.storage.data_dir = tmp_path
    service = ControlService(cfg, adapter, repo, ChatLog(), Runtime())
    yield service, repo, adapter, tmp_path
    await conn.close()


def cctl():
    return ControlContext(source="cli", chat_key="cli", scene="", peer_id=0, auto_confirm=True)


def test_make_chart_writes_png(tmp_path):
    from muze.mediaops import make_chart

    p = make_chart(tmp_path, "销量", ["一月", "二月", "三月"], [3, 7, 5])
    assert p.exists() and p.suffix == ".png"
    assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    line = make_chart(tmp_path, "趋势", ["a", "b"], [1, 2], kind="line")
    assert line.exists()
    with pytest.raises(ValueError):
        make_chart(tmp_path, "bad", ["a"], [1, 2])


async def test_send_media_from_archive(env):
    service, repo, adapter, tmp_path = env
    png = tmp_path / "pic.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    mid = await repo.add_media(
        {
            "scene": "friend", "peer_id": 9, "message_seq": 5, "seg_index": 0,
            "kind": "image", "resource_id": "r", "file_id": "", "file_name": "pic.png",
            "file_hash": "", "width": 1, "height": 1, "duration": None, "summary": "",
            "status": "done", "file_path": str(png), "file_size": png.stat().st_size,
            "content_hash": None, "time": 1758000000,
        }
    )
    runner = ToolRunner(service, repo, adapter, None, cctl())
    res = json.loads(await runner.run("send_media", {"scene": "friend", "peer_id": 9, "media_id": mid, "caption": "给你"}))
    assert res.get("is_error") is False
    # an image segment + the caption text went out
    segs = adapter.sent[-1][2]
    assert segs[0]["type"] == "image" and segs[1] == {"type": "text", "data": {"text": "给你"}}


async def test_send_media_local_path(env):
    service, repo, adapter, tmp_path = env
    doc = tmp_path / "report.txt"
    doc.write_text("hi", encoding="utf-8")
    runner = ToolRunner(service, repo, adapter, None, cctl())
    res = json.loads(await runner.run("send_media", {"scene": "group", "peer_id": 1, "path": str(doc)}))
    assert res.get("is_error") is False
    assert adapter.sent[-1][2][0]["type"] == "file"
    assert adapter.sent[-1][2][0]["data"]["file_name"] == "report.txt"


async def test_make_chart_tool(env):
    service, repo, adapter, _ = env
    runner = ToolRunner(service, repo, adapter, None, cctl())
    res = json.loads(await runner.run("make_chart", {"title": "T", "labels": ["a", "b"], "values": [1, 2]}))
    assert "path" in res and res["path"].endswith(".png")


async def test_ocr_image_uses_vision(env):
    service, repo, adapter, tmp_path = env
    png = tmp_path / "o.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    await repo.add_media(
        {
            "scene": "group", "peer_id": 1, "message_seq": 2, "seg_index": 0,
            "kind": "image", "resource_id": "r", "file_id": "", "file_name": "",
            "file_hash": "", "width": 1, "height": 1, "duration": None, "summary": "",
            "status": "done", "file_path": str(png), "file_size": 24,
            "content_hash": None, "time": 1,
        }
    )

    class VisionLLM:
        async def vision_describe(self, path, prompt):
            return "识别到的文字"

    runner = ToolRunner(service, repo, adapter, VisionLLM(), cctl())
    res = json.loads(await runner.run("ocr_image", {"scene": "group", "peer_id": 1, "message_seq": 2}))
    assert "识别到的文字" in res["text"]
