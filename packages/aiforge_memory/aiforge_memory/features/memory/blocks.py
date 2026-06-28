"""Letta-style self-editing memory blocks (frontier gap #8).

A persistent, named scratchpad per (repo, label) that an AGENT maintains
itself via tools — read the block, rewrite it. Unlike Observations (mined
facts) a block is a single living document the agent owns: working notes,
a running TODO, accumulated conventions, or the user-preference profile
(gap #9 reuses this with label='user_preferences').

Stored as a singleton ``(:MemoryBlock {repo, label})`` node so a block is
overwritten in place, not appended as a new fact. Size-bounded so a
runaway agent can't grow it without limit.
"""
from __future__ import annotations

_MAX_CHARS = 8000

_GET_BLOCK = """
MATCH (b:MemoryBlock {repo: $repo, label: $label})
RETURN b.content AS content
"""

_SET_BLOCK = """
MERGE (b:MemoryBlock {repo: $repo, label: $label})
ON CREATE SET b.created_at = datetime()
SET b.content = $content, b.updated_at = datetime()
"""

_LIST_BLOCKS = """
MATCH (b:MemoryBlock {repo: $repo})
RETURN b.label AS label, size(coalesce(b.content,'')) AS chars
ORDER BY label
"""


def get_block(driver, *, repo: str, label: str = "working_notes") -> str:
    """Return the block's current content, or '' when it doesn't exist."""
    with driver.session() as s:
        row = s.run(_GET_BLOCK, repo=repo, label=label).single()
    return (row["content"] if row and row["content"] else "")


def set_block(driver, *, repo: str, label: str = "working_notes",
              content: str = "") -> dict:
    """Overwrite the block (the agent's self-edit). Size-bounded."""
    content = (content or "")[:_MAX_CHARS]
    with driver.session() as s:
        s.run(_SET_BLOCK, repo=repo, label=label, content=content).consume()
    return {"repo": repo, "label": label, "chars": len(content)}


def append_block(driver, *, repo: str, label: str = "working_notes",
                 line: str = "") -> dict:
    """Append one line, deduping exact repeats. Convenience over set_block."""
    line = (line or "").strip()
    if not line:
        return {"repo": repo, "label": label, "chars": 0, "skipped": True}
    cur = get_block(driver, repo=repo, label=label)
    if line in cur.splitlines():
        return set_block(driver, repo=repo, label=label, content=cur)
    new = f"{cur}\n{line}".strip() if cur else line
    return set_block(driver, repo=repo, label=label, content=new)


def list_blocks(driver, *, repo: str) -> list[dict]:
    with driver.session() as s:
        return [dict(r) for r in s.run(_LIST_BLOCKS, repo=repo)]


__all__ = ["get_block", "set_block", "append_block", "list_blocks"]
