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

    try:
        from neo4j import GraphDatabase
        from aiforge_memory.features.memory.store import upsert_observation
    except ImportError:
        return {"ok": False, "error": "deps_missing"}

    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"driver: {exc}"}

    text_parts = [
        f"FAILURE ticket={ticket.identifier} project={ticket.project} "
        f"title={(ticket.title or '')[:120]!r}",
        f"verdict={verdict}",
    ]
    if reason:
        text_parts.append(f"reason={reason[:300]}")
    if ci_status:
        text_parts.append(f"ci={ci_status}")
    if review_verdict:
        text_parts.append(f"review={review_verdict}")
    text = " | ".join(text_parts)

    tags = [
        f"ticket:{ticket.identifier}",
        "kind:failure",
        f"verdict:{verdict}",
    ]
    if ci_status:
        tags.append(f"ci:{ci_status}")
    if review_verdict:
        tags.append(f"review:{review_verdict}")

    try:
        out = upsert_observation(
            drv, repo=ticket.project, text=text,
            kind="failure",
            author="failure_memory",
            tags=tags,
        )
        log.info(
            "failure_memory: ticket=%s id=%s deduped=%s",
            ticket.identifier, out.get("id"), out.get("deduped"),
        )
        return {"ok": True, "id": out.get("id"),
                "deduped": out.get("deduped", False)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"upsert: {exc}"}
    finally:
        try:
            drv.close()
        except Exception:
            pass


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
            import json
            state = callback_context.state
            ticket_identifier = state.get("ticket_identifier", "")
            repo = (
                state.get("ticket_project")
                or os.environ.get("AIFORGE_AFM_REPO", "")
                or ""
            )
            if not (ticket_identifier and repo):
                return None
            fb = state.get("feedback_verdict") or ""
            vv = state.get("validator_verdict") or ""
            if isinstance(vv, str):
                try:
                    vv_parsed = json.loads(vv)
                except Exception:
                    vv_parsed = {}
            else:
                vv_parsed = vv or {}
            validator_verdict = (vv_parsed.get("verdict") or "")
            # Pass only when both in-loop AND validator approve.
            ok = fb == "pass" and validator_verdict in {"approve", ""}
            if ok:
                return None

            # Build a lightweight stub for record_failure.
            class _T:
                pass
            t = _T()
            t.identifier = ticket_identifier
            t.title = state.get("ticket_title", "")
            t.project = repo
            verdict = fb if fb else "fail"
            reason = (vv_parsed.get("rationale") or "")[:280]
            record_failure(
                t, verdict=verdict, reason=reason,
                review_verdict=validator_verdict or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("failure_memory callback: %s", exc)
        return None

    return _callback


__all__ = ["record_failure", "make_failure_memory_after_callback"]
