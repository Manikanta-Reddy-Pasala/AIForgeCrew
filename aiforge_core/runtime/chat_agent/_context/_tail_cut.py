"""Where a condense cuts the history, and what it may put back.

Two rules the kept tail has to follow after the system message:

* it opens on a USER turn. Strict chat templates require user after system;
  an assistant ACTION or ``tool_calls`` straight after it is rejected.
* it never splits a native tool exchange. A ``role: tool`` result without the
  assistant ``tool_calls`` that asked for it is rejected too.

Restoring a pointer's body happens after the budget check, so it is measured
again here: a restore that would put the history back over budget leaves the
pointer instead, and the model re-reads the file if it needs it.
"""
from __future__ import annotations

#: How far back a cut may move to reach a real user turn before a short
#: harness note is used instead.
_BACK_STEPS = 3

_OPENER = ("[context condensed — not the user] The earlier turns are "
           "summarised in the system message. Continue the task from here.")


def _chars(m) -> int:
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, list):
        return sum(len(p.get("text", "")) for p in c
                   if isinstance(p, dict) and p.get("type") == "text")
    return len(c) if isinstance(c, str) else 0


def tail_start(convo: list, keep: int, room: int = 0) -> "tuple[int, bool]":
    """``(start, opener)`` for a tail of about ``keep`` messages.

    ``start`` never lands on a tool result: it moves back to the assistant
    turn that made the call. When that turn is not a user message, the cut
    moves back up to :data:`_BACK_STEPS` messages to reach one, as long as
    the extra text fits in ``room`` chars (0 = no limit). Otherwise ``opener``
    is True and the caller puts a short user note first.
    """
    n = len(convo)
    start = max(1, n - keep)
    while start > 1 and convo[start].get("role") == "tool":
        start -= 1
    if convo[start].get("role") == "user":
        return start, False
    extra = 0
    for i in range(start - 1, max(0, start - 1 - _BACK_STEPS), -1):
        if i < 1:
            break
        extra += _chars(convo[i])
        if room and extra > room:
            break
        role = convo[i].get("role")
        if role == "user":
            return i, False
        if role == "system":
            break
    return start, True


def opener() -> dict:
    return {"role": "user", "content": _OPENER}


def within_budget(before: list, after: list, room: int) -> list:
    """``after`` (``before`` with pointer bodies restored), cut back to what
    fits in ``room`` extra chars. Per message: a restore that does not fit is
    dropped, and that message keeps its pointer."""
    if after is before or len(after) != len(before):
        return after
    grow = sum(_chars(a) - _chars(b) for a, b in zip(after, before))
    if grow <= room:
        return after
    out, used = [], 0
    for a, b in zip(after, before):
        d = _chars(a) - _chars(b)
        if d <= 0 or used + d <= room:
            out.append(a)
            used += max(0, d)
        else:
            out.append(b)
    return out
