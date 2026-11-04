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


def chunk_markdown(md: str, target: int = 1200, hard_max: int = 2400) -> list[str]:
    md = (md or "").strip()
    if not md:
        return []
    if len(md) <= hard_max:
        return [md]
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
