"""Shared singletons + constants for the ``adk_runner`` package.

Leaf module — imports nothing from the rest of the package so it can
never take part in a circular import. Holds the module logger (and the
``logging.basicConfig`` side-effect, kept in exactly one place), the
tickets-store handle, and the Feedback-verdict mapping constants.
"""
from __future__ import annotations

import logging
import os

from aiforge_core.tickets import store as tickets_mod

log = logging.getLogger("adk_runner")
logging.basicConfig(
    level=os.environ.get("AIFORGE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# Map Feedback verdict → tickets-store status. ``fail`` and any
# unrecognised value land in ``blocked`` so a human can triage.
# ``partial`` comes from the loop-budget kill switch (see
# :mod:`loop_budget`) — partial work still gets PR-shipped for human
# review, so the status maps to ``blocked`` to flag triage need.
_VERDICT_TO_STATUS: dict[str, str] = {
    "pass": "done",
    "scope_violation": "cancelled",
    "partial": "blocked",
}


# Order matters: ``scope_violation`` is checked before ``fail`` (string
# substring overlap) and ``partial`` is checked before ``pass`` for
# the same reason — neither is a strict substring of the other today
# but the rule keeps future-proofing cheap.
_VERDICT_TOKENS: tuple[str, ...] = (
    "scope_violation", "partial", "pass", "fail",
)

# Cap rationales persisted to ticket_events so a chatty model can't bloat
# the audit trail with a multi-paragraph rant. 300 chars matches the spec
# in the operator-observability ticket.
_REASON_MAX_CHARS = 300
_REASON_DEFAULT_PASS = "no rationale provided"
_REASON_DEFAULT_FAIL = "no rationale provided"
