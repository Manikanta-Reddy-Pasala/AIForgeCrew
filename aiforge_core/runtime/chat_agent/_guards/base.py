"""What every claim guard has in common, and the one loop that runs them.

A claim guard watches a FINAL answer for a statement that reality does not
back (a file edited, a Confluence page created, a change request answered with
a plan). When it finds one it sends the model back to do the work, a bounded
number of times; when the budget is spent it puts an honest note in front of
the answer so the user is never told something that did not happen.

A guard only says WHAT it checks (``detect`` / ``evidence``) and WHAT it says
(``notice`` / ``nudge`` / ``disclaimer``); :func:`run_guards` owns the
nudge-then-disclaim loop, the counter and the events.
"""
from __future__ import annotations

from typing import Protocol


class ClaimGuard(Protocol):
    #: Attribute on ``st`` counting the nudges this guard has sent.
    counter: str
    #: Put the disclaimer in front of the answer stripped of surrounding space.
    strip_body: bool
    #: Show the answer being sent back as a thought before the notice.
    echo_text: bool

    def applies(self, st) -> bool:
        """Cheap gate: is this guard relevant to this turn at all?"""

    def budget(self, st) -> int:
        """How many times the model may be sent back before the disclaimer."""

    def detect(self, text: str) -> list:
        """The claims ``text`` makes (empty: nothing to check)."""

    def evidence(self, st) -> "set | bool":
        """What backs the claims: a set of claims that ARE backed, or True
        when everything is."""

    def notice(self, claims: list) -> str:
        """The system line shown while the model is sent back ('' for none)."""

    def nudge(self, claims: list) -> str:
        """The message sent to the model."""

    def disclaimer(self, claims: list) -> str:
        """The note put in front of the answer once the budget is spent."""


def _check(guard, st, step):
    text = step.get("text") or ""
    if not guard.applies(st):
        return None
    claims = guard.detect(text)
    if not claims:
        return None
    backed = guard.evidence(st)
    if isinstance(backed, (set, frozenset)):
        claims = [c for c in claims if c not in backed]
    elif backed:
        return None
    if not claims:
        return None
    sent = getattr(st, guard.counter, 0)
    if sent < guard.budget(st):
        setattr(st, guard.counter, sent + 1)
        if guard.echo_text and step.get("text"):
            yield {"type": "thought", "text": step["text"]}
        note = guard.notice(claims)
        if note:
            yield {"type": "thought", "role": "system", "text": note}
        hook = getattr(guard, "on_nudge", None)
        if hook is not None:
            hook(st)                      # e.g. turn reasoning on for the retry
        st.convo.append({"role": "user", "content": guard.nudge(claims)})
        return "continue"
    step["text"] = guard.disclaimer(claims) + (text.strip() if guard.strip_body
                                               else text)
    return None


def run_guards(st, step, guards):
    """Run ``guards`` in order over the FINAL ``step``. Returns ``"continue"``
    as soon as one sends the model back, else None (the answer may have been
    labelled). A guard that is not a claim check (the action-log sanitiser)
    supplies its own ``check(st, step)`` generator."""
    for guard in guards:
        check = getattr(guard, "check", None)
        sig = yield from (check(st, step) if check else _check(guard, st, step))
        if sig == "continue":
            return "continue"
    return None
