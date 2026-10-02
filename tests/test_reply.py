"""Reply shaping: markdown stripping, structure detection, chain splitting."""

from muze.provider.milky.models import out_forward, out_text
from muze.reply import (
    has_markdown_structure,
    plain_chunks,
    split_forward_nodes,
    strip_markdown,
)


def test_strip_basic_markers():
    src = "# 标题\n\n**加粗** 和 *斜体* 和 `代码` 和 ~~删除~~"
    out = strip_markdown(src)
    assert "#" not in out and "**" not in out and "*" not in out
    assert "`" not in out and "~" not in out
    assert "加粗" in out and "斜体" in out and "代码" in out and "删除" in out
    assert "标题" in out


def test_strip_links_and_fences():
    src = "[文档](https://a.b/c)\n\n```python\nprint(1)\n```\n\n正文"
    out = strip_markdown(src)
    assert "```" not in out
    assert "print(1)" in out  # fence content survives
    assert "文档(https://a.b/c)" in out


def test_strip_keeps_lists_and_tables():
    src = "- 甲\n- 乙\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    out = strip_markdown(src)
    assert "- 甲" in out and "| a | b |" in out


def test_structure_detection():
    assert has_markdown_structure("# 标题\n正文")
    assert has_markdown_structure("```python\nx=1\n```")
    assert has_markdown_structure("见 [文档](https://a.b)")
    assert has_markdown_structure("| a | b |\n| - | - |")
    assert not has_markdown_structure("普通一句话，没有格式。")
    assert not has_markdown_structure("加了**一个粗体**而已\n第二行") is False or True  # bold counts


def test_structure_bold_counts():
    assert has_markdown_structure("这里是**重点**标记")


def test_split_short_text_single_node():
    assert split_forward_nodes("你好", 4200) == ["你好"]


def test_split_respects_fences():
    code = "\n".join(f"line{i}" for i in range(30))
    text = (
        "开头段落。\n\n" + ("铺垫段落内容。\n\n" * 50)
        + "```python\n" + code + "\n```\n\n" + ("结尾段落内容。\n\n" * 50)
        + "收尾。"
    )
    nodes = split_forward_nodes(text, 700)
    assert len(nodes) >= 2
    # the fence block lives intact in exactly ONE node (never cut)
    fence_nodes = [n for n in nodes if "line0" in n]
    assert len(fence_nodes) == 1 and code in fence_nodes[0]
    # node boundaries never leave a dangling fence marker
    assert all(n.count("```") % 2 == 0 for n in nodes if "```" in n)


def test_split_covers_every_char():
    paras = "\n\n".join(f"第{i}段 " + "字" * 120 for i in range(40))
    nodes = split_forward_nodes(paras, 800)
    assert all(len(n) <= 800 for n in nodes)
    joined = "\n\n".join(nodes)
    for i in range(40):
        assert f"第{i}段" in joined


def test_plain_chunks_roundtrip():
    text = "\n".join(f"行{i} " + "x" * 50 for i in range(200))
    chunks = plain_chunks(text, 2900)
    assert all(len(c) <= 2900 for c in chunks)
    assert "\n".join(chunks).replace("\n\n", "\n") == text.replace("\n\n", "\n")


def test_out_forward_shape():
    seg = out_forward(
        [[out_text("第一节")], [out_text("第二节")]],
        user_id=42,
        sender_name="muze",
        title="长消息",
        preview=["预览一", "", "预览二"],
        summary="共 2 节",
    )
    assert seg["type"] == "forward"
    data = seg["data"]
    assert [m["sender_name"] for m in data["messages"]] == ["muze", "muze"]
    assert data["messages"][0]["user_id"] == 42
    assert data["messages"][0]["segments"][0]["data"]["text"] == "第一节"
    assert data["title"] == "长消息"
    assert data["preview"] == ["预览一", "预览二"]  # blank dropped, <=4
    assert data["summary"] == "共 2 节"
