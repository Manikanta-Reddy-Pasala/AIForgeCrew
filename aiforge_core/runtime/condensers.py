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
    events: list[dict[str, Any]], *, keep: int = 20, **_kw,
) -> list[dict[str, Any]]:
    # Placeholder — wires to EscalatingLlm in a follow-up. Today behaves
    # identically to ``recent`` so behaviour is deterministic.
    return _recent(events, keep=keep)


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
