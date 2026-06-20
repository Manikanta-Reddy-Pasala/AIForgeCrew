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

    # Embedded (zero-infra) path — write the failure to the SQLite memory
    # store so prior failures still surface on the next similar ticket.
    from aiforge_core.memory import backend_select as _bsel
    if _bsel.embedded():
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

    try:
        from aiforge_memory.features.memory.store import upsert_observation
        from neo4j import GraphDatabase
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

    # Embed the failure text — without embed_vec the observation is
    # invisible to vector recall AND the PPR seed stage, so the whole
    # "prior failures surface on the next similar ticket" loop was
    # broken at the write site. Soft: no sidecar → write without vector.
    embed_vec = None
    try:
        from aiforge_core.memory.embed import embed as _embed
        embed_vec = _embed(text)
    except Exception:  # noqa: BLE001
        embed_vec = None

    try:
        out = upsert_observation(
            drv, repo=ticket.project, text=text,
            kind="failure",
            author="failure_memory",
            tags=tags,
            embed_vec=embed_vec,
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
            # Feedback's contract is "verdict token on the FIRST LINE,
            # rationale below" — the raw value is e.g. "pass\nlooks good",
            # never exactly "pass". Compare the leading token, not the
            # whole string, or every clean pass writes a fake failure.
            fb_token = (fb.split()[0].lower() if isinstance(fb, str)
                        and fb.split() else "")
            # Pass only when both in-loop AND validator approve.
            ok = fb_token in {"pass", "pass_with_warnings"} \
                and validator_verdict in {"approve", ""}
            if ok:
                return None

            # Don't record a failure that's about to be re-planned — the
            # Validator runs once PER attempt, so a mid-replan write would
            # pile up near-duplicate failure observations. Only the
            # TERMINAL validator pass (replan budget spent) records.
            try:
                from .graph_pipeline import MAX_REPLANS
            except Exception:
                MAX_REPLANS = 1
            failing = validator_verdict in {"request_changes", "reject", "fail"}
            replan_count = int(state.get("replan_count", 0) or 0)
            if failing and replan_count < MAX_REPLANS:
                return None  # not terminal — a re-planned attempt may pass

            # Build a lightweight stub for record_failure.
            class _T:
                pass
            t = _T()
            t.identifier = ticket_identifier
            t.title = state.get("ticket_title", "")
            t.project = repo
            verdict = fb_token or "fail"
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
