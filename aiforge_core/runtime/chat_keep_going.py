"""Keep going: a chat's "big task" setting — the turn keeps working on the
goal instead of stopping when it gets stuck or runs out of budget.

Without it a turn ends at the stuck-escalation limit ("stopped after
repeated attempts"), at the step cap or at the time budget when it shows no
new progress. With it, each of those points starts the work over from a
fresh context (the goal, the task board and a handoff of what is known and
what already failed; the code on disk stays) and carries on.

Bounded, so a task the model cannot solve does not burn tokens forever:
``AIFORGE_CHAT_PERSIST_RETRIES`` fresh starts (default 8) and
``AIFORGE_CHAT_PERSIST_MAX_S`` seconds of work (default 4 h) per turn. The
user can always press Stop.
"""
from __future__ import annotations

import os
import threading
import time

_LOCK = threading.Lock()
_ON: dict = {}


def set_for(session_id, on: bool) -> None:
    """The setting the user sent with this message (per chat session)."""
    if session_id is None:
        return
    with _LOCK:
        if on:
            _ON[session_id] = True
        else:
            _ON.pop(session_id, None)


def on(session_id) -> bool:
    if session_id is None:
        return False
    with _LOCK:
        return bool(_ON.get(session_id))


def _int_env(key: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(key, default)))
    except ValueError:
        return default


def retries() -> int:
    return _int_env("AIFORGE_CHAT_PERSIST_RETRIES", 8)


def max_s() -> int:
    return _int_env("AIFORGE_CHAT_PERSIST_MAX_S", 4 * 3600)


def start(st, session_id) -> None:
    """Arm the turn state: whether this turn keeps going, and its bounds."""
    st.persist = on(session_id)
    st.persist_used = 0
    # The clock is read only for a turn that keeps going: an ordinary turn's
    # timing (and the tests' stepped clock) stays exactly as it was.
    st.persist_t0 = time.monotonic() if st.persist else 0.0


def may_restart(st) -> bool:
    """True when this turn keeps going and has a fresh start left."""
    if not getattr(st, "persist", False):
        return False
    if getattr(st, "persist_used", 0) >= retries():
        return False
    limit = max_s()
    return not limit or time.monotonic() - getattr(st, "persist_t0", time.monotonic()) < limit


def fresh_start(st, why: str):
    """Start the work over from a fresh context instead of stopping: the
    goal, the board and a handoff of what is known and what failed. The
    best tested code is put back first when the code now tests worse."""
    st.persist_used = getattr(st, "persist_used", 0) + 1
    try:
        from aiforge_core.runtime.chat_agent._turn._best_state import restore_if_worse
        yield from restore_if_worse(st)
    except Exception:  # noqa: BLE001
        pass
    restarted = False
    try:
        from aiforge_core.runtime.chat_agent._turn._escalate import _condense, restart_with_handoff
        restarted = restart_with_handoff(st)
        if not restarted:
            _condense(st)
    except Exception:  # noqa: BLE001
        pass
    st.stuck_escalations = 0
    st.wrapping_up = False
    st.time_warned = False
    st.half_checked = False
    st.deadline_final = None
    note = ("[keep going — not the user] The previous attempt " + why.rstrip(".") +
            ". The task is not done and this chat is set to keep going: work on the goal "
            "again with a DIFFERENT approach than what the handoff says failed. Re-check "
            "the code on disk first, then continue.")
    last = st.convo[-1] if st.convo else None
    if isinstance(last, dict) and last.get("role") == "user" and isinstance(last.get("content"), str):
        st.convo[-1] = {**last, "content": last["content"] + "\n\n" + note}
    else:
        st.convo.append({"role": "user", "content": note})
    yield {"type": "thought", "role": "system",
           "text": f"↻ keep going: {why.rstrip('.')} — fresh start "
                   f"{st.persist_used}/{retries()}"
                   + (" from a handoff" if restarted else "")}


__all__ = ["set_for", "on", "start", "may_restart", "fresh_start", "retries", "max_s"]
