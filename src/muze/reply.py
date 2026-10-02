"""Outbound reply shaping for QQ.

QQ text carries no formatting, so plain-text replies get markdown markers
stripped (the formatted original travels as a rendered image — see
muze.render). Long replies become merged-forward chains instead of being
truncated: the full text is preserved, split across chain nodes.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^```.*$", re.M)

_INLINE_RULES = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"\1"),
    (re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])"), r"\1"),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"!\[([^\]\n]*)\]\((https?://[^)\s]+)\)"), r"\1(\2)"),
    (re.compile(r"\[([^\]\n]*)\]\((https?://[^)\s]+)\)"), r"\1(\2)"),
]


def strip_markdown(text: str) -> str:
    """Markdown -> QQ-friendly plain text.

    Fenced code keeps its content (fence lines removed); headings, emphasis,
    inline code, links and quote markers lose their syntax; tables and list
    bullets stay (their plain layout is readable in chat).
    """
    if not text:
        return text
    out = _FENCE_RE.sub("", text)
    for rx, repl in _INLINE_RULES:
        out = rx.sub(repl, out)
    out = re.sub(r"^#{1,6}\s+", "", out, flags=re.M)   # headings
    out = re.sub(r"^>\s?", "", out, flags=re.M)        # blockquotes
    out = re.sub(r"^(-{3,}|\*{3,})\s*$", "——", out, flags=re.M)  # hr
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


_STRUCTURE_HINTS = [
    re.compile(r"```"),
    re.compile(r"^#{1,6}\s+\S", re.M),
    re.compile(r"\[[^\]\n]+\]\(https?://[^)\s]+\)"),
    re.compile(r"^>\s?\S", re.M),
    re.compile(r"^\|.*\|", re.M),
    re.compile(r"^(-{3,}|\*{3,})$", re.M),
    re.compile(r"\*\*[^*\n]+\*\*"),
    re.compile(r"`[^`\n]+`"),
]


def has_markdown_structure(text: str) -> bool:
    """True when the text's formatting is worth an accompanying image."""
    return any(rx.search(text or "") for rx in _STRUCTURE_HINTS)


# ------------------------------------------------------------ chain split
def _fence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) char spans of fenced code blocks (including fences)."""
    spans: list[tuple[int, int]] = []
    pos = 0
    open_at = -1
    for m in _FENCE_RE.finditer(text):
        if open_at < 0:
            open_at = m.start()
        else:
            spans.append((open_at, m.end()))
            open_at = -1
        pos = m.end()
    if open_at >= 0:  # unclosed fence: treat the rest as code
        spans.append((open_at, len(text)))
    return spans


def _in_fence(spans: list[tuple[int, int]], i: int) -> bool:
    return any(a <= i < b for a, b in spans)


def split_forward_nodes(text: str, node_chars: int = 4200) -> list[str]:
    """Split plain text into forward-chain nodes.

    Splits on paragraph boundaries (blank lines); never cuts inside a fenced
    code block; oversized blocks are split by lines, then hard-wrapped.
    """
    text = (text or "").strip()
    if not text:
        return []
    n = max(600, int(node_chars))
    if len(text) <= n:
        return [text]
    spans = _fence_spans(text)
    # paragraph boundaries OUTSIDE fences
    cuts: list[int] = [0]
    for m in re.finditer(r"\n\s*\n", text):
        i = m.start()
        if not _in_fence(spans, i):
            cuts.append(m.end())
    cuts.append(len(text))
    blocks: list[str] = []
    for a, b in zip(cuts, cuts[1:]):
        blk = text[a:b].strip("\n")
        if blk.strip():
            blocks.append(blk)
    # hard-split any oversized block by lines
    pieces: list[str] = []
    for blk in blocks:
        if len(blk) <= n:
            pieces.append(blk)
            continue
        cur = ""
        for line in blk.split("\n"):
            if len(cur) + len(line) + 1 > n and cur:
                pieces.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
            while len(cur) > n:  # single absurd line
                pieces.append(cur[:n])
                cur = cur[n:]
        if cur.strip():
            pieces.append(cur)
    # greedy packing
    nodes: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) + 2 > n:
            nodes.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur.strip():
        nodes.append(cur)
    return nodes or [text]


def plain_chunks(text: str, limit: int = 2900) -> list[str]:
    """Fallback sequential-split when the protocol side cannot send chains."""
    text = text or ""
    limit = max(200, int(limit))
    if len(text) <= limit:
        return [text] if text.strip() else []
    out: list[str] = []
    while text:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [c for c in out if c.strip()]
