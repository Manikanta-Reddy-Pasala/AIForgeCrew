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
    """What the model is TOLD when a mid-run message arrives.

    A bare "[steer] …" tag was too weak to change anything: a local model read
    it as a footnote to the request it was already executing and carried on
    answering the OLD question. The message the user typed after the run
    started is, by definition, the more recent statement of what they want —
    say so, in the imperative, and make the two possible readings explicit
    (replaces vs adds) so the model has to pick one rather than defaulting to
    the plan already in its context.

    NOT used by parallel-team mode: that folds steering into SPEC.md as a
    "[MANDATORY user instruction]" line (parallel_subtasks/_stream.py), where
    there is no single in-flight request to replace. Deliberate — do not
    "unify" it without reading that path first.
    """
    return steer_block([text])


def steer_block(texts: "list[str]") -> str:
    """One directive for however many messages drained together.

    A directive per message meant three queued steers produced three blocks
    each claiming to be THE most recent instruction, with no ordering signal —
    "use postgres" and the "actually no, sqlite" that followed it a second
    later arrived as equals. One header, numbered, newest marked.
    """
    items = [t for t in (texts or []) if str(t).strip()]
    if not items:
        return ""
    if len(items) == 1:
        body = items[0]
    else:
        body = "\n".join(
            f"{i + 1}. {t}" + ("   ← the latest, and the one that wins if they "
                               "conflict" if i == len(items) - 1 else "")
            for i, t in enumerate(items))
    return (
        "[NEW MESSAGE FROM THE USER — sent while you were working. It is the "
        "user's MOST RECENT instruction and takes PRIORITY over what you are "
        "currently doing.]\n"
        f"{body}\n"
        "If it REPLACES the request you were working on, abandon that one and "
        "do this instead. If it ADDS to it, do this first, then continue. Act "
        "on it now — do NOT reply with a sentence about which reading you "
        "chose: a bare line of prose ends the turn, and the user gets a "
        "comment where the work should have been."
    )


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


def applied_event(text: str) -> dict:
    """Stream event acknowledging a steer was folded into the run (the
    sequential team driver's poll-once ack, since its before_model callback has
    no direct handle to the stream)."""
    return {"type": "thought", "role": "system",
            "text": f"📌 Got your message — folding it in now: “{text[:120]}”"}


__all__ = ["SYSTEM_NOTES", "user_guidance", "reject_directive",
           "steer_directive", "steer_block", "reject_note",
           "steer_event", "applied_event"]
