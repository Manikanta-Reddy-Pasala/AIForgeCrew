"""Shared prompt-construction helpers.

Used by every archetype that talks to the LLM so context-shaping logic
stays in one place: compaction (head/tail keeper) and failure-recall
rendering. Both are deterministic, free, and don't require an LLM call.
"""
from __future__ import annotations

from typing import Iterable


def compact(text: str, *, head: int = 1500, tail: int = 1500) -> str:
    """Token-budget-friendly compaction.

    Keeps the first `head` chars + last `tail` chars and replaces the
    middle with `... [N chars elided] ...`. Good enough for code
    contexts where the most useful bits are at the top (semantic
    top-K) and the bottom (runbook / sources tail). Returns the input
    unchanged if it already fits."""
    if not text or len(text) <= head + tail + 64:
        return text
    return (
        text[:head]
        + f"\n\n... [{len(text) - head - tail} chars elided] ...\n\n"
        + text[-tail:]
    )


def render_failures_block(
    failures: Iterable[dict] | None, *, header: str | None = None,
) -> str:
    """Render the auto-correct failure-recall block.

    `failures` is the list returned by `learner.online.top_failures_for`:
    each dict has keys mode, evidence, lesson, seen_count. The block
    is injected into agent prompts so the model can avoid known
    mistakes on similar tickets."""
    if not failures:
        return ""
    items = list(failures)[:5]
    if not items:
        return ""
    head = header or (
        "# Mistakes from prior similar tickets — DO NOT REPEAT:"
    )
    out = [head]
    for f in items:
        mode = f.get("mode", "?")
        evid = (f.get("evidence") or "")[:120]
        seen = f.get("seen_count", 1)
        out.append(f"- [{mode} ×{seen}] {evid}")
        if f.get("lesson"):
            out.append(f"  → {f.get('lesson')[:200]}")
    return "\n".join(out) + "\n"
