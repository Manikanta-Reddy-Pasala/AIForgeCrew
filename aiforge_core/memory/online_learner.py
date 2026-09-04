"""What is left of the Postgres-backed online learner: attachment lookup.

This module used to front seven Postgres tables — ``episodic_outcomes``,
``procedural_patterns``, ``audit_events``, ``step_traces``, ``skills``,
``failures`` and ``attachments``. Postgres was removed (SQLite-only build) and
every writer became a soft no-op, kept "so callers degrade gracefully".

They do not degrade gracefully any more, because there are no callers. The only
thing anything still imports from here is :func:`attachments_for`, reached once
from ``memory.trial_balance.run_workflow``. Eleven no-op functions preserving
the shape of an interface nobody calls are not an interface — they are a
promise the code cannot keep, and each one made a reader ask whether the
arguments it accepts and ignores were forgotten rather than retired.

So they are gone. Skills and failures are served by
``aiforge_core.memory.skills`` and ``aiforge_core.runtime.failure_memory``;
audit and step traces by ``aiforge_core.observability``. Anything that wants
what those tables held should ask one of those, not a stub that answers "".

Attachments have no replacement store yet, which is why the reader below still
exists and still returns nothing: the caller already handles an empty list (it
reports the missing roles and blocks), and that behaviour is under test in
``tests/python/test_trial_balance_delegate.py``.
"""
from __future__ import annotations


def attachments_for(_ticket_id: str) -> list[dict]:
    """Attachments recorded for a ticket — always empty, no store backs it.

    The one caller (``trial_balance.run_workflow``) treats an empty list as
    "the required attachments are missing" and blocks with that reason, which
    is the correct outcome while nothing is recording attachments.
    """
    return []


def detect_attachment_role(filename: str) -> str:
    """Classify an attachment by its name — tally / oneshell / screenshot.

    Kept with :func:`attachments_for`: whatever restores an attachment store
    needs this rule, and it is the only part of the old module that was
    behaviour rather than a stub.
    """
    low = (filename or "").lower()
    if "tally" in low:
        return "tally"
    if "oneshell" in low:
        return "oneshell"
    if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "screenshot"
    return "other"


__all__ = ["attachments_for", "detect_attachment_role"]
