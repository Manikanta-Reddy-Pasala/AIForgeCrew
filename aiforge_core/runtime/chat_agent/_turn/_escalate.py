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

from aiforge_core.runtime.stuck_policy import Policy

from ._stuck.state import reset


def arm_reasoning(st) -> None:
    """A stuck step is the one that needs thinking: turn reasoning on for the
    next few model calls (see llm.reasoning.boost)."""
    st.reason_boost = max(getattr(st, "reason_boost", 0), Policy.load().reason_steps)


def pause_on_stuck() -> bool:
    return Policy.load().pause_on_stuck


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
    pol = Policy.load()
    limit = pol.stuck_escalations
    wrapping = bool(limit) and n > limit
    _remember_failure(st, why)
    reset(st, "escalation")
    restarted = False
    if n % 2 == 0 and pol.stuck_restart:
        restarted = restart_with_handoff(st)
    elif n % 2 == 1:
        _condense(st)
    text = (_WRAP_UP if wrapping else _TIERS[min(n, len(_TIERS)) - 1])
    note = (f"[loop guard — not the user] {why} {text}" if not restarted
            else text)
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


def _remember_failure(st, why: str) -> None:
    """Write down the approach that just failed, so the next attempt (and the
    handoff after a restart) can say what not to repeat."""
    from aiforge_core.runtime import handoff
    attempt = handoff.last_attempt(getattr(st, "convo", []))
    handoff.record_failed(st, f"{attempt}" if attempt else why.rstrip("."))


def restart_with_handoff(st) -> bool:
    """Start the run over from a fresh context: the system prompt with the goal
    and the task board pinned, and ONE message holding the handoff. The failed
    transcript is saved (``memory_lookup {id}`` restores any detail), not kept.
    Returns True when the context was replaced."""
    try:
        from aiforge_core.runtime import context_offload, handoff
        from .._context import _compaction as C
        from .._context import _note
        from ._tasks import pin_board
        old = list(st.convo)
        if len(old) < 3 or old[0].get("role") != "system":
            return False
        oid = context_offload.save(context_offload.render(old[1:]))
        if not oid:
            return False          # never drop the transcript without a saved copy
        h = handoff.build_chat(st)
        if not h["goal"]:
            h["goal"] = next((C._text_of(m).strip()[:1200] for m in old[1:]
                              if m.get("role") == "user"
                              and not C._text_of(m).strip().startswith("OBSERVATION:")
                              and not C._is_harness_note(C._text_of(m).strip())), "")
        if _note.enabled():
            _restart_into_note(st, old, h, oid)
        else:
            sys_text = C._pin_goal(C._stripped_system(old), old)
            st.convo[:] = [{**old[0], "content": sys_text},
                           {"role": "user", "content": handoff.render(h, oid)}]
        if getattr(st, "board", None):
            pin_board(st.convo, st.board)
        seen = getattr(st, "read_sigs_seen", None)
        if hasattr(seen, "clear"):
            seen.clear()
        reset(st, "restart")
        st.restarts = getattr(st, "restarts", 0) + 1
        rec = getattr(st, "handoff_rec", None)
        if rec is not None:
            rec.note_restart(h, oid)       # the restart itself survives a crash
        return True
    except Exception:  # noqa: BLE001 — a failed restart falls back to the nudge
        return False


def _restart_into_note(st, old: list, h: dict, oid) -> None:
    """The restarted context with the system message UNTOUCHED: the pinned
    goal, the board and the handoff go in the note right after it (see
    ``_context._note``), so the model server's prompt cache is not thrown away
    by the restart."""
    from aiforge_core.runtime import handoff
    from .._context import _compaction as C
    from .._context import _note
    sys_text = old[0].get("content")
    sys_text = sys_text if isinstance(sys_text, str) else ""
    prior_src = _note.text(old) or sys_text
    gen = C._block_gen(C._prior_block(prior_src)) + 1
    work = [old[0]] + old[_note.prefix_len(old):]
    prior_goal = C._GOAL_RE.search(prior_src) or C._GOAL_RE.search(sys_text)
    goal_block = C._pin_goal(prior_goal.group(0) if prior_goal else "", work)
    head = (f"[HANDOFF (condense #{gen}) — the earlier attempt went in circles, "
            "so this is a fresh start. It holds what is known; do not repeat "
            "what failed.]")
    block = (f"{C._CONDENSE_OPEN}\n{handoff.render(h, oid, header=head)}\n"
             f"{C._CONDENSE_CLOSE}")
    st.convo[:] = [C._clean_system(old[0]), _note.build(goal_block, "", block)]


def _condense(st) -> None:
    try:
        from .._context import _compact_convo
        new = _compact_convo(st.convo, role=st.role, force=True, keep_recent=8,
                             run_key=getattr(st, "compact_key", None),
                             handoff_st=st)
        if new is not st.convo and len(new) < len(st.convo):
            st.convo[:] = new
    except Exception:  # noqa: BLE001 — a failed condense still nudges
        pass


def give_up_message(st) -> str:
    return ("(stopped after repeated attempts: I could not move this forward. "
            "Say \"continue\" and I will pick it up from where the work is on "
            "disk, or tell me what to change.)")
