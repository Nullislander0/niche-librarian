"""Split markdown into chunks on paragraph boundaries, keeping fenced code intact."""
from __future__ import annotations

import re

_FENCE = re.compile(r"^(```|~~~)")


def _blocks(md: str) -> list[str]:
    """Paragraph-level blocks; a fenced code block is always one block."""
    blocks, cur, in_fence = [], [], False
    for line in md.splitlines():
        if _FENCE.match(line.strip()):
            in_fence = not in_fence
            cur.append(line)
            if not in_fence:
                blocks.append("\n".join(cur))
                cur = []
            continue
        if not in_fence and not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")


def chunk_markdown(md: str, target: int = 1200, hard_max: int = 2400) -> list[str]:
    """Chunks of ~target chars. A chunk that starts mid-section is prefixed with its heading
    trail (e.g. "[Configure HTTPS > Import a certificate]") so it reads and embeds in context."""
    md = (md or "").strip()
    if not md:
        return []
    if len(md) <= hard_max:
        return [md]
    raw = _split(md, target, hard_max)
    out, trail, in_fence = [], [], False
    for chunk in raw:
        if trail and not in_fence and not _HEADING.match(chunk.split("\n", 1)[0]):
            chunk = f"[{' > '.join(trail)}]\n\n{chunk}"
        out.append(chunk)
        for line in chunk.splitlines():
            if _FENCE.match(line.strip()):
                in_fence = not in_fence
                continue
            m = None if in_fence else _HEADING.match(line)
            if m and not re.fullmatch(r"\w+:", m.group(2).strip()):  # skip "Note:", "Caution:" labels
                level = len(m.group(1))
                trail = trail[:level - 1] + [m.group(2).strip()]
    return out


def _split(md: str, target: int, hard_max: int) -> list[str]:
    chunks, cur = [], ""
    for block in _blocks(md):
        while len(block) > hard_max:  # pathological single block: cut it
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(block[:hard_max])
            block = block[hard_max:]
        if cur and len(cur) + len(block) + 2 > target:
            chunks.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        chunks.append(cur)
    return chunks
