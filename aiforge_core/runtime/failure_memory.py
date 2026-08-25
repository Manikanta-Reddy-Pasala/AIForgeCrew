"""Failure-pattern memory writeback.

Pattern the user asked for: every time the pipeline fails on a
ticket, the agent learns from it so the same shape gets handled
better next time. We extract a small structured record from the
verdict + recent ticket_events and persist it as an AFM
``Observation_v2`` with ``kind="failure"`` so future memory_block
hits surface the prior failure when a similar ticket comes through.

Mirrors :mod:`learner_persist` but writes only on verdict !=
``pass``. Soft-fail, never raises.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.failure_memory")


def _failure_text(ticket, verdict, reason, ci_status, review_verdict) -> str:
    parts = [
        f"FAILURE ticket={ticket.identifier} project={ticket.project} "
        f"title={(ticket.title or '')[:120]!r}",
        f"verdict={verdict}",
    ]
    if reason:
        parts.append(f"reason={reason[:300]}")
    if ci_status:
        parts.append(f"ci={ci_status}")
    if review_verdict:
        parts.append(f"review={review_verdict}")
    return " | ".join(parts)


def _failure_tags(ticket, verdict, ci_status, review_verdict) -> list[str]:
    tags = [f"ticket:{ticket.identifier}", "kind:failure", f"verdict:{verdict}"]
    if ci_status:
        tags.append(f"ci:{ci_status}")
    if review_verdict:
        tags.append(f"review:{review_verdict}")
    return tags


def record_failure(
    ticket,
    *,
    verdict: str,
    reason: str = "",
    ci_status: str | None = None,
    review_verdict: str | None = None,
) -> dict:
    """Persist a failure observation. Returns ``{ok, id?, error?}``."""
    if verdict == "pass" or not ticket:
        return {"ok": False, "error": "not_a_failure"}
    if not ticket.project:
        return {"ok": False, "error": "no_project"}

    text = _failure_text(ticket, verdict, reason, ci_status, review_verdict)
    tags = _failure_tags(ticket, verdict, ci_status, review_verdict)

    # Write the failure to the embedded SQLite memory store so prior failures
    # still surface on the next similar ticket.
    try:
        from aiforge_core.memory import sqlite_memory as _sqlmem
        rid = _sqlmem.write_unit(
            text=text, kind="failure", source="failure_memory",
            tags=tags, repo=ticket.project, ticket=ticket.identifier,
        )
        log.info("failure_memory[sqlite]: ticket=%s id=%s",
                 ticket.identifier, rid)
        return {"ok": True, "id": rid, "deduped": rid == 0}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sqlite: {exc}"}


def _validator_dict(vv) -> dict:
    if not isinstance(vv, str):
        return vv or {}
    try:
        import json
        return json.loads(vv)
    except Exception:  # noqa: BLE001
        return {}


def _feedback_token(fb) -> str:
    """Feedback's contract is "verdict token on the FIRST LINE, rationale
    below" — the raw value is e.g. "pass\\nlooks good", never exactly "pass".
    Comparing the leading token (not the whole string) is what keeps every
    clean pass from writing a fake failure."""
    if not isinstance(fb, str) or not fb.split():
        return ""
    return fb.split()[0].lower()


def _is_terminal_failure(validator_verdict: str, state) -> bool:
    """A failure that is about to be RE-PLANNED is not recorded — the Validator
    runs once PER attempt, so a mid-replan write would pile up near-duplicate
    failure observations. Only the TERMINAL pass (replan budget spent) records."""
    try:
        from .graph_pipeline import MAX_REPLANS
    except Exception:  # noqa: BLE001
        MAX_REPLANS = 1
    failing = validator_verdict in {"request_changes", "reject", "fail"}
    replan_count = int(state.get("replan_count", 0) or 0)
    return not (failing and replan_count < MAX_REPLANS)


def _ticket_stub(identifier: str, title: str, repo: str):
    """A lightweight stand-in for the ticket record_failure expects."""
    class _T:
        pass
    t = _T()
    t.identifier = identifier
    t.title = title
    t.project = repo
    return t


def _record_from_state(state) -> None:
    ticket_identifier = state.get("ticket_identifier", "")
    repo = (state.get("ticket_project")
            or os.environ.get("AIFORGE_AFM_REPO", "") or "")
    if not (ticket_identifier and repo):
        return
    vv_parsed = _validator_dict(state.get("validator_verdict") or "")
    validator_verdict = vv_parsed.get("verdict") or ""
    fb_token = _feedback_token(state.get("feedback_verdict") or "")
    # Pass only when both in-loop AND validator approve.
    if fb_token in {"pass", "pass_with_warnings"} \
            and validator_verdict in {"approve", ""}:
        return
    if not _is_terminal_failure(validator_verdict, state):
        return          # not terminal — a re-planned attempt may pass
    record_failure(
        _ticket_stub(ticket_identifier, state.get("ticket_title", ""), repo),
        verdict=fb_token or "fail",
        reason=(vv_parsed.get("rationale") or "")[:280],
        review_verdict=validator_verdict or None)


def make_failure_memory_after_callback():
    """Return an ADK ``after_agent_callback`` for the Validator.

    Reads ``state['validator_verdict']`` (parsed as JSON if string),
    plus ``state['feedback_verdict']`` and the canonical ticket
    identifier / project that adk_runner seeds into initial_state,
    and writes a failure ``Observation_v2`` whenever the run didn't
    land cleanly.

    Soft-fail. Never breaks the pipeline.
    """
    async def _callback(*, callback_context, **_kw):
        if os.environ.get("AIFORGE_FAILURE_MEMORY", "1") in {"0", "false", ""}:
            return None
        try:
            _record_from_state(callback_context.state)
        except Exception as exc:  # noqa: BLE001
            log.debug("failure_memory callback: %s", exc)
        return None

    return _callback


__all__ = ["record_failure", "make_failure_memory_after_callback"]
