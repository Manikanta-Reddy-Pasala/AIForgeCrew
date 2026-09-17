"""Which session and step a request is billed to."""
from __future__ import annotations


def _pkg():
    """The parent module, looked up on each call so a name patched there is the
    one used here."""
    import aiforge_core.llm.call_meter as package
    return package


def _key(session_id) -> "str | None":
    return None if session_id in (None, "") else str(session_id)


def _slot(sid: str) -> dict:
    pkg = _pkg()
    slot = pkg._sessions.get(sid)
    if slot is None:
        slot = {"total": 0, "turn": 0, "by_role": {}, "epoch": 0,
                "failed": 0, "turn_failed": 0,
                "tokens_out": 0, "turn_tokens_out": 0,
                "tokens_in": 0, "turn_tokens_in": 0}
        pkg._sessions[sid] = slot
        while len(pkg._sessions) > pkg._MAX_SESSIONS:
            pkg._sessions.popitem(last=False)     # oldest out
    else:
        pkg._sessions.move_to_end(sid)
    return slot


def _attribute(sid, role):
    """(session, role) for this call, falling back to the request context.

    The wire-level caller (_post) knows neither — both ride the request
    context, which the chat turn binds and the generation thread inherits.

    CONTEXTVAR ONLY. request_context.get_session_id() also falls back to the
    process-global AIFORGE_CURRENT_SESSION env var, which the chat route sets
    and never clears — so every bare thread in the system (session folds,
    learners, classifiers) would bill its calls to whichever chat last ran a
    turn. An unattributed call is correct; a call billed to an innocent chat
    is not.
    """
    if sid is not None and role:
        return sid, role
    try:
        from aiforge_core.runtime import request_context
        if sid is None:
            sid = _key(request_context.context_session_id())
        role = role or request_context.get_role()
    except Exception:  # noqa: BLE001
        pass
    return sid, role


def _current_epoch():
    try:
        return _pkg()._TURN_EPOCH.get()
    except Exception:  # noqa: BLE001
        return None


def _bump_step_counter() -> None:
    """One more call inside the current ReAct step, when a step is bound."""
    try:
        _step = _pkg()._STEP_CALLS.get()
        if isinstance(_step, dict):
            _step["n"] = int(_step.get("n") or 0) + 1
    except Exception:  # noqa: BLE001
        pass


def _bill_session_locked(sid, role, epoch):
    """Charge one call to a chat session. Returns the epoch to STAMP the token
    with. Caller holds ``_lock``.

    A cancelled generation is ABANDONED, not stopped: its thread keeps retrying
    with the turn's context still bound. Those calls belong to the turn that
    made them, not to whatever the user typed next — so the per-turn counter
    only accepts calls stamped with the CURRENT turn's epoch (None = a caller
    outside any turn, e.g. a background fold).
    """
    slot = _slot(sid)
    slot["total"] += 1
    if epoch is None or epoch == slot["epoch"]:
        slot["turn"] += 1
        # Stamp the token with the turn this call was COUNTED against.
        # Carrying the caller's bare None instead meant a failure resolved
        # `epoch is None` at settle time and landed on whatever turn was
        # current THEN — the exact thing the token exists to prevent, and
        # visible as "0 requests · 1 failed" on a message that sent nothing.
        epoch = slot["epoch"]
    if role:
        slot["by_role"][role] = slot["by_role"].get(role, 0) + 1
    return epoch
