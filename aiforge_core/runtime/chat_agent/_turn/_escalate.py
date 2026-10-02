"""A stuck run changes approach and keeps going; it does not stop to ask.

Every guard that used to pause the turn ("I keep trying the same step… could
you clarify?") calls :func:`escalate` instead. Each trip does two things: it
condenses the history (a loop is usually a model that lost the thread in a long
prompt), and it sends a stronger, different instruction than the trip before.
The task finishes. The turn ends only when the model answers, the user presses
Stop, or — after ``AIFORGE_CHAT_STUCK_ESCALATIONS`` trips (default 20, 0 =
never) — with a written summary of what is done and what remains.

``AIFORGE_CHAT_PAUSE_ON_STUCK=1`` restores the old pause-and-ask.
"""
from __future__ import annotations

import os


def arm_reasoning(st) -> None:
    """A stuck step is the one that needs thinking: turn reasoning on for the
    next few model calls (see llm.reasoning.boost)."""
    from aiforge_core.llm import reasoning
    st.reason_boost = max(getattr(st, "reason_boost", 0), reasoning.boost_steps())


def pause_on_stuck() -> bool:
    return os.environ.get("AIFORGE_CHAT_PAUSE_ON_STUCK", "").strip().lower() \
        in ("1", "true", "yes", "on")


def _limit() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_STUCK_ESCALATIONS", "20")))
    except ValueError:
        return 20


_TIERS = (
    "Step back. In one line say what the last attempts had in common, then "
    "choose an approach you have NOT tried (a different tool, a different "
    "file, a smaller step) and do it now.",
    "Make the smallest change that moves the task forward and do it: read "
    "the one file you need, or write the one edit you are sure of. If a tool "
    "keeps failing, use a different tool for the same goal.",
    "Take the simplest path to finishing. Do what you can with what you "
    "already know, then verify it. If one part is truly blocked, finish the "
    "rest and say plainly what is blocked and why.",
)

_WRAP_UP = ("Stop retrying. Write `FINAL:` now: what is done, what remains, and "
            "what you would do next. Be specific.")


def escalate(st, why: str):
    """One stuck trip. Yields a status line and queues the nudge (and a
    condense) on ``st.convo``; returns ``"continue"`` to keep the run going, or
    ``"wrap_up"`` once the limit is spent and the model has been told to
    summarise (the caller then ends the turn if it still does not)."""
    n = getattr(st, "stuck_escalations", 0) + 1
    st.stuck_escalations = n
    arm_reasoning(st)
    limit = _limit()
    wrapping = bool(limit) and n > limit
    if n % 2 == 1:
        _condense(st)
    text = (_WRAP_UP if wrapping else _TIERS[min(n, len(_TIERS)) - 1])
    note = f"[loop guard — not the user] {why} {text}"
    last = st.convo[-1] if st.convo else None
    if (isinstance(last, dict) and last.get("role") == "user"
            and isinstance(last.get("content"), str)):
        # Two user turns in a row break some providers: ride on the last one.
        st.convo[-1] = {**last, "content": last["content"] + "\n\n" + note}
    else:
        st.convo.append({"role": "user", "content": note})
    yield {"type": "thought", "role": "system",
           "text": f"↺ {why.rstrip('.')} — changing approach (try {n}), "
                   "the task continues"}
    return "wrap_up" if wrapping and n > limit + 1 else "continue"


def _condense(st) -> None:
    try:
        from .._context import _compact_convo
        new = _compact_convo(st.convo, role=st.role, force=True, keep_recent=8,
                             run_key=getattr(st, "compact_key", None))
        if new is not st.convo and len(new) < len(st.convo):
            st.convo[:] = new
    except Exception:  # noqa: BLE001 — a failed condense still nudges
        pass


def give_up_message(st) -> str:
    return ("(stopped after repeated attempts: I could not move this forward. "
            "Say \"continue\" and I will pick it up from where the work is on "
            "disk, or tell me what to change.)")
