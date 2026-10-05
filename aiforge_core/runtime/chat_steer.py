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


def steer_directive(text: str) -> str:
    """What the model is TOLD when a mid-run message arrives: the user's words
    under a short header (see :func:`steer_block`).

    NOT used by parallel-team mode: that folds steering into SPEC.md as a
    "[MANDATORY user instruction]" line (parallel_subtasks/_stream_steer.py), where
    there is no single in-flight request to replace. Deliberate — do not
    "unify" it without reading that path first.
    """
    return steer_block([text])


def steer_block(texts: "list[str]") -> str:
    """The messages that drained together, as ONE light wrapper.

    The running model reads the user's words and decides what they mean — a
    correction, an extra requirement, a question, a change of task. The wrapper
    only says where the words come from and that the task goes on unless they
    say otherwise; it does not tell the model how to classify them. Several
    messages are numbered in the order they were sent, the newest marked.
    The opening "[NEW MESSAGE FROM THE USER" is matched elsewhere
    (chat_agent/_native_replay, _native_select): keep it.
    """
    items = [t for t in (texts or []) if str(t).strip()]
    if not items:
        return ""
    if len(items) == 1:
        body = items[0]
    else:
        body = "\n".join(
            f"{i + 1}. {t}" + ("   ← the latest" if i == len(items) - 1 else "")
            for i, t in enumerate(items))
    return ("[NEW MESSAGE FROM THE USER — sent while you were working.]\n"
            f"{body}\n"
            "Address it, and continue with the task you were on unless it says "
            "otherwise.")


def reject_note(guidance: str) -> str:
    """A correction the user typed when REJECTING one tool call.

    Deliberately not :func:`steer_directive`: this is guidance about the action
    that was refused, never a new task, so it must not offer "abandon the
    request you were working on". A rejected `write_file` with "use tmp/
    instead" is a path correction — an agent that read it as a replacement
    dropped the remaining files of a half-built feature.
    """
    return ("The user rejected the last action and gave this correction: "
            f"{guidance}\nAdjust accordingly and CONTINUE the current task — "
            "this is guidance about that action, not a new request.")


def steer_event(text: str) -> dict:
    """Stream event echoing the user's steer TEXT (shown + persisted as a
    ``steer`` step) — used the moment a steer is drained, in every mode."""
    return {"type": "thought", "role": "steer", "text": text}


def reply_event(to: "list[str] | str", text: str) -> dict:
    """Stream event carrying the REPLY to a message the user sent mid-run.

    A supplementary message with role ``reply``: stored with the turn's steps,
    and shown by the UI as a card of its own ("You asked … / the reply"), not
    as one more row in the steps list that folds away when the turn ends.
    ``to`` is what the user wrote."""
    asked = [to] if isinstance(to, str) else list(to or [])
    return {"type": "message", "supplementary": True, "role": "reply",
            "text": text,
            "to": " / ".join(" ".join(str(q).split())[:300]
                             for q in asked if str(q).strip())}


#: Team runs fold a mid-run message into the work; no agent answers it there.
TEAM_REPLY = ("Passed to the agent that is working now; it applies to the rest "
              "of this run. A team run does not answer a question while it "
              "works: the final report covers it. For an answer now, send it "
              "with the Side task button.")

#: The run ended before any step could read the message.
LATE_REPLY = ("This message arrived as the run was ending and was not read. "
              "Send it again: it starts a new turn.")


def applied_event(text: str) -> dict:
    """Stream event acknowledging a steer was folded into the run (the
    sequential team driver's poll-once ack, since its before_model callback has
    no direct handle to the stream)."""
    return {"type": "thought", "role": "system",
            "text": f"📌 Got your message — folding it in now: “{text[:120]}”"}


__all__ = ["SYSTEM_NOTES", "user_guidance", "reject_directive",
           "steer_directive", "steer_block", "reject_note",
           "steer_event", "applied_event", "reply_event", "TEAM_REPLY",
           "LATE_REPLY"]
