"""What is left of the Postgres-backed online learner: attachment lookup.

This module used to front seven Postgres tables — ``episodic_outcomes``,
``procedural_patterns``, ``audit_events``, ``step_traces``, ``skills``,
``failures`` and ``attachments``. Postgres was removed (SQLite-only build) and
every writer became a soft no-op, kept "so callers degrade gracefully".

They do not degrade gracefully any more, because there are no callers. The only
thing anything still imports from here is :func:`attachments_for`, reached from
per-host workflow modules (``$AIFORGE_CONFIG_DIR/workflows/``). Eleven no-op functions preserving
the shape of an interface nobody calls are not an interface — they are a
promise the code cannot keep, and each one made a reader ask whether the
arguments it accepts and ignores were forgotten rather than retired.

So they are gone. Skills and failures are served by
``aiforge_core.memory.skills`` and ``aiforge_core.runtime.failure_memory``;
audit and step traces by ``aiforge_core.observability``. Anything that wants
what those tables held should ask one of those, not a stub that answers "".

Attachments have no replacement store yet, which is why the reader below still
exists and still returns nothing: a workflow caller handles an empty list by
reporting the missing roles and blocking.
"""
from __future__ import annotations

import os


def attachments_for(_ticket_id: str) -> list[dict]:
    """Attachments recorded for a ticket — always empty, no store backs it.

    A workflow caller treats an empty list as
    "the required attachments are missing" and blocks with that reason, which
    is the correct outcome while nothing is recording attachments.
    """
    return []


def detect_attachment_role(filename: str) -> str:
    """Classify an attachment by its name — a configured role / screenshot.

    Roles come from ``AIFORGE_ATTACHMENT_ROLES`` (comma-separated names, first
    match wins): a file whose name contains a role name gets that role. A
    per-host workflow that needs named attachments sets it.

    Kept with :func:`attachments_for`: whatever restores an attachment store
    needs this rule, and it is the only part of the old module that was
    behaviour rather than a stub.
    """
    low = (filename or "").lower()
    for role in os.environ.get("AIFORGE_ATTACHMENT_ROLES", "").split(","):
        role = role.strip().lower()
        if role and role in low:
            return role
    if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "screenshot"
    return "other"


__all__ = ["attachments_for", "detect_attachment_role"]
