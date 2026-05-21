"""OpenHands-parity memory condensers (sub #4).

Plug-in strategies that reduce a list of ADK session events before they
are replayed on the next agent turn. Pure functions: input list →
output list. ADK ``ContextFilterPlugin`` runs ahead of these and
typically eats most of the volume; condensers handle the residual
long-tail.

Strategies:

* ``noop``       — identity passthrough
* ``recent``     — keep last N events
* ``amortized``  — collapse oldest half into one ``<condensed>`` block
                  once event count exceeds a threshold
* ``llm``        — placeholder; falls back to ``recent`` until wired
"""
from __future__ import annotations

from typing import Any


def _noop(events: list[dict[str, Any]], **_kw) -> list[dict[str, Any]]:
    return list(events)


def _recent(
    events: list[dict[str, Any]], *, keep: int = 20, **_kw,
) -> list[dict[str, Any]]:
    if keep <= 0:
        return []
    return list(events[-keep:])


def _amortized(
    events: list[dict[str, Any]],
    *,
    threshold: int = 40,
    keep_tail: int = 20,
    **_kw,
) -> list[dict[str, Any]]:
    if len(events) <= threshold:
        return list(events)
    pivot = max(1, len(events) - keep_tail)
    head = events[:pivot]
    tail = events[pivot:]

    summary_lines = []
    for evt in head:
        kind = evt.get("type") or evt.get("kind") or "event"
        role = evt.get("role") or evt.get("agent") or ""
        body = (evt.get("text") or evt.get("content") or "")
        if isinstance(body, str):
            body = body.strip().replace("\n", " ")[:80]
        else:
            body = str(body)[:80]
        if role:
            summary_lines.append(f"- [{role}] {kind}: {body}")
        else:
            summary_lines.append(f"- {kind}: {body}")
    summary_text = (
        "<condensed>\n"
        f"{len(head)} earlier events compressed to brief summaries:\n"
        + "\n".join(summary_lines[:200])
        + "\n</condensed>"
    )
    condensed_event = {
        "type": "system", "role": "condenser",
        "text": summary_text,
        "n_compressed": len(head),
    }
    return [condensed_event] + tail


def _llm(
    events: list[dict[str, Any]], *, keep: int = 20,
    summarizer: Any = None, **_kw,
) -> list[dict[str, Any]]:
    """Real LLM-summarized condenser.

    Asks the operator-supplied ``summarizer`` (callable: prompt → str)
    to compress the head events into a single ``<llm_summary>`` block,
    keeps the tail verbatim. Falls back to ``recent`` when no
    ``summarizer`` provided or the call fails — preserves the
    deterministic contract from sub #4's first ship.
    """
    if summarizer is None or len(events) <= keep:
        return _recent(events, keep=keep)
    pivot = max(1, len(events) - keep)
    head = events[:pivot]
    tail = events[pivot:]
    # Build a compact prompt so the summarizer doesn't OOM on giant
    # event streams. Cap each event to 200 chars; cap total to ~16 KB.
    rows: list[str] = []
    total = 0
    for evt in head:
        role = evt.get("role") or evt.get("agent") or ""
        body = (evt.get("text") or evt.get("content") or "")
        if isinstance(body, str):
            body = body.replace("\n", " ")[:200]
        else:
            body = str(body)[:200]
        row = f"[{role}] {body}"
        if total + len(row) > 16 * 1024:
            rows.append("...[further events truncated]")
            break
        rows.append(row)
        total += len(row)
    prompt = (
        "Summarize the following agent transcript into 5-10 short "
        "bullet points capturing decisions, file edits, test outcomes, "
        "and open questions. Drop tool noise.\n\n"
        + "\n".join(rows)
    )
    try:
        summary_text = summarizer(prompt)
        if not isinstance(summary_text, str) or not summary_text.strip():
            return _recent(events, keep=keep)
    except Exception:  # noqa: BLE001 — fallback path
        return _recent(events, keep=keep)
    summary_event = {
        "type": "system", "role": "condenser",
        "text": f"<llm_summary>\n{summary_text.strip()}\n</llm_summary>",
        "n_compressed": len(head),
    }
    return [summary_event] + tail


_STRATEGIES = {
    "noop": _noop,
    "recent": _recent,
    "amortized": _amortized,
    "llm": _llm,
}


def condense(
    events: list[dict[str, Any]],
    strategy: str = "noop",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Apply ``strategy`` to ``events`` and return the reduced list.

    Unknown strategy → noop with a debug log (no raise).
    """
    fn = _STRATEGIES.get(strategy, _noop)
    return fn(events, **kwargs)


__all__ = ["condense"]
