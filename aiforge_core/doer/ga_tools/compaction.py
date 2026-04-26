"""Auto-compaction — middle-out history trim near token cap.

Mirrors Aider's ``/clear`` heuristic: when chat history approaches
``context_win`` we drop the middle turns and replace them with a
single ``[SUMMARY]`` placeholder so the model retains:
- The system prompt + initial task framing (start of history)
- The most recent N turns (end of history)

Pure logic — callers wire this BEFORE LLMSession.chat by mutating
``session.history`` in-place. KISS: no LLM-call summary; the summary
line is a fixed string ("N turns elided"). The model sees the elision
explicitly so it doesn't pretend it remembers.

Toggle via ``AIFORGE_DOER_COMPACT=1`` (default off until validated).
"""
from __future__ import annotations


def estimate_tokens(history: list[dict]) -> int:
    """Cheap len/4 heuristic — same one Aider's RepoMap uses as a
    fallback. Avoids tokenizer dep."""
    total = 0
    for msg in history or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    text = blk.get("text") or blk.get("content") or ""
                    total += len(str(text)) // 4
    return total


def needs_compaction(history: list[dict], context_win: int,
                     threshold: float = 0.8) -> bool:
    """True when estimated tokens exceed ``threshold * context_win``."""
    if not history or context_win <= 0:
        return False
    return estimate_tokens(history) > int(context_win * threshold)


def compact_middle(
    history: list[dict],
    *,
    keep_head: int = 2,
    keep_tail: int = 6,
) -> list[dict]:
    """Return a NEW history list with middle turns elided.

    ``keep_head``: how many oldest messages to retain (system + first
    user task framing).
    ``keep_tail``: how many newest messages to retain.

    If the history is already short enough that head+tail >= len(),
    return it unchanged.
    """
    n = len(history)
    if n <= keep_head + keep_tail:
        return list(history)

    elided = n - keep_head - keep_tail
    summary = {
        "role": "user",
        "content": [{
            "type": "text",
            "text": (
                f"[SUMMARY] {elided} earlier turn(s) elided to fit "
                f"context window. Earlier work landed file edits / "
                f"compile attempts as recorded in the worktree. "
                f"Continue from current state."
            ),
        }],
    }
    return list(history[:keep_head]) + [summary] + list(history[-keep_tail:])


def maybe_compact(
    history: list[dict],
    context_win: int,
    *,
    threshold: float = 0.8,
    keep_head: int = 2,
    keep_tail: int = 6,
) -> tuple[list[dict], bool]:
    """One-shot helper. Returns ``(new_history, did_compact)``."""
    if not needs_compaction(history, context_win, threshold):
        return list(history), False
    return compact_middle(history, keep_head=keep_head,
                          keep_tail=keep_tail), True
