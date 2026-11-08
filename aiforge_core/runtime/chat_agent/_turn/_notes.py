"""How a harness note reaches the model.

Replays of recorded runs (11-08): a note inside a tool result was ignored (1
of 8 samples moved), the same note as its own user message was followed (8
of 8). And a note written while a step was still running landed BEFORE that
step's result: the post-edit error check appended "the file you just wrote
has these errors" before the edit's OBSERVATION, so the native replay
answered the edit call with "not run: no result was recorded" and sent the
real result as a stray user message (attrs t1: the package could not import
for the last 25 minutes of the run).

So a note made during a step is held (:func:`hold`) and sent after that
step's result (:func:`flush`); a note made between steps goes out at once
(:func:`send`). In native tool-calling mode a note is its own user message
right after the result; in text mode it rides on the last user turn, since
two user turns in a row break some providers.
"""
from __future__ import annotations

#: Lines that start a harness note (merged into one message when two meet).
_HEADS = ("[harness", "[automated ", "[loop guard", "[hook ")


def hold(st, text: str) -> None:
    """Keep a note made during a step until the step's result is written."""
    if text:
        st.__dict__.setdefault("held_notes", []).append(text)


def take(st) -> list:
    """The held notes, cleared."""
    notes = list(getattr(st, "held_notes", None) or [])
    if notes:
        st.held_notes = []
    return notes


def send(st, text: str) -> None:
    """Deliver a note now: its own user message in native mode (merged into a
    note just before it), else on the last user turn."""
    if not text:
        return
    last = st.convo[-1] if st.convo else None
    is_user = isinstance(last, dict) and last.get("role") == "user" \
        and isinstance(last.get("content"), str)
    if getattr(st, "native_on", False):
        if is_user and last["content"].lstrip().startswith(_HEADS):
            st.convo[-1] = {**last, "content": last["content"] + "\n\n" + text}
        else:
            st.convo.append({"role": "user", "content": text})
        return
    if is_user:
        st.convo[-1] = {**last, "content": last["content"] + "\n\n" + text}
    else:
        st.convo.append({"role": "user", "content": text})


def flush(st) -> None:
    """Send the notes held during the step that just wrote its result."""
    for text in take(st):
        send(st, text)
