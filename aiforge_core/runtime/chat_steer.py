"""Shared mid-run steer / reject-guidance helpers — one source for all modes.

The same three ideas were re-implemented in four places (the simple/plan ReAct
loop in ``chat_agent``, the team gate in ``tool_gate``, the parallel-team drain
in ``parallel_subtasks``, and the sequential driver in ``chat_pipeline``):

  1. is a reject NOTE real user guidance, or a system note the registry set?
  2. the "user rejected — adjust per this, don't repeat, continue" directive.
  3. the stream event that shows a steer / an applied steer in the UI.

Centralised here so reject-with-guidance behaves identically everywhere (steer +
continue) and the UI events are consistent.
"""
from __future__ import annotations

# Notes the approval registry sets ITSELF (not user guidance) — see
# chat_approve. A reject carrying one of these is a plain stop/expiry, not a
# steer.
SYSTEM_NOTES = frozenset({
    "cancelled", "superseded", "run finished", "approval timed out",
    "no pending approval",
})


def user_guidance(note: "str | None") -> str:
    """The user's typed guidance from a reject note, or "" when the note is
    blank or a system note (so callers steer only on REAL guidance)."""
    n = (note or "").strip()
    return "" if (not n or n in SYSTEM_NOTES) else n


def reject_directive(tool_name: str, guidance: str) -> str:
    """The instruction folded into the agent's context after a reject-with-
    guidance so it adjusts course + continues instead of repeating the action."""
    return (f"The user REJECTED the `{tool_name}` action and gave this guidance: "
            f"{guidance}\nDo NOT repeat the rejected action as-is — adjust per "
            "the guidance and continue.")


def steer_event(text: str) -> dict:
    """Stream event echoing the user's steer TEXT (shown + persisted as a
    ``steer`` step) — used the moment a steer is drained, in every mode."""
    return {"type": "thought", "role": "steer", "text": text}


def applied_event(text: str) -> dict:
    """Stream event acknowledging a steer was folded into the run (the
    sequential team driver's poll-once ack, since its before_model callback has
    no direct handle to the stream)."""
    return {"type": "thought", "role": "system",
            "text": f"📌 Got your message — folding it in now: “{text[:120]}”"}


__all__ = ["SYSTEM_NOTES", "user_guidance", "reject_directive",
           "steer_event", "applied_event"]
