"""Persist the Learner's ``facts_json`` output into the memory layer.

The Learner archetype emits ``state['facts_json']`` — a JSON array of
``{text, about, tags}`` dicts — only on ``feedback_verdict.verdict ==
'pass'``. This module wires those facts into Neo4j by:

1. Writing each fact as an ``:Observation_v2`` node via AiForgeMemory's
   ``upsert_observation`` helper (kind=``learning``, tags + ticket id).
2. Optionally writing a ``:Decision_v2`` node when the fact text starts
   with ``DECISION:`` — keeps the small bar for promoting an
   observation into a durable architectural choice.
3. Linking each new node to the ticket's ``:Repo`` (whichever repo
   ``ticket.project`` resolves to) via the ``RECORDS`` edge.

Without this callback the Learner's output is dropped on the floor —
the agents/learner.py docstring references a "server-side write_fact
plugin" that never landed in this codebase. Memory stayed near-empty
(1 observation + 1 note + 0 decisions) across 8 days of ticket runs.

Wire as the Learner agent's ``after_agent_callback`` in
``runtime/pipeline.py``.

Env knobs:
  AIFORGE_LEARNER_PERSIST_DISABLE=1   skip persistence (debug)
  AIFORGE_NEO4J_URI / USER / PASSWORD legacy AFM connection vars
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any


log = logging.getLogger("aiforge.learner_persist")


_DECISION_PREFIX = "DECISION:"


def _is_disabled() -> bool:
    return os.environ.get("AIFORGE_LEARNER_PERSIST_DISABLE", "0") in ("1", "true")


def _coerce_facts(raw: Any) -> list[dict]:
    """Learner emits a JSON string OR a Python list depending on which
    LLM backend wrote the state slot. Normalise to a list of dicts."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _open_driver():
    """Connect to Neo4j with the same env vars AiForgeMemory uses.
    Returns ``None`` on import / connection failure."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    try:
        return GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.warning("learner_persist.driver_failed: %s", exc)
        return None


def persist_facts(
    *,
    facts: list[dict],
    repo: str,
    ticket_identifier: str | None = None,
    session_id: str = "",
) -> dict:
    """Persist a list of Learner-emitted facts into Neo4j as
    Observation_v2 (default) or Decision_v2 (DECISION: prefix).

    Returns ``{written_observations, written_decisions, errors}``.
    Soft-fails on any backend error — never raises into the agent loop.
    """
    out = {"written_observations": 0, "written_decisions": 0, "errors": []}
    if _is_disabled():
        out["errors"].append("disabled_via_env")
        return out
    if not facts or not repo:
        return out

    driver = _open_driver()
    if driver is None:
        out["errors"].append("neo4j_unreachable")
        return out

    # Module renamed in AiForgeMemory commit 32d86ad — try both.
    try:
        from aiforge_memory.features.memory.store import (
            upsert_decision, upsert_observation,
        )
    except ImportError:
        try:
            from aiforge_memory.memory.store import (  # type: ignore
                upsert_decision, upsert_observation,
            )
        except ImportError:
            out["errors"].append("aiforge_memory_not_installed")
            try:
                driver.close()
            except Exception:
                pass
            return out

    try:
        for fact in facts:
            text = (fact.get("text") or "").strip()
            if not text:
                continue
            about = fact.get("about") or []
            tags = list(fact.get("tags") or [])
            if ticket_identifier:
                tags.append(f"ticket:{ticket_identifier}")
                refs = list(about) + [f"ticket:{ticket_identifier}"]
            else:
                refs = list(about)
            try:
                if text.startswith(_DECISION_PREFIX):
                    title = text[len(_DECISION_PREFIX):].strip()[:120] or "decision"
                    upsert_decision(
                        driver, repo=repo, title=title, body=text,
                        rationale="learner-emit",
                        author="learner",
                        session_id=session_id,
                        tags=tags, refs=refs,
                    )
                    out["written_decisions"] += 1
                else:
                    upsert_observation(
                        driver, repo=repo, text=text,
                        kind="learning",
                        author="learner",
                        session_id=session_id,
                        tags=tags, refs=refs,
                    )
                    out["written_observations"] += 1
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"upsert_failed: {exc}")
    finally:
        try:
            driver.close()
        except Exception:
            pass

    log.info(
        "learner_persist: repo=%s ticket=%s observations=%d decisions=%d errors=%d",
        repo, ticket_identifier or "-",
        out["written_observations"], out["written_decisions"],
        len(out["errors"]),
    )
    return out


def make_learner_after_callback():
    """Return an ADK ``after_agent_callback`` for the Learner agent.

    The callback reads ``state['facts_json']`` plus ticket / repo
    context and persists each fact. Wrapped in try/except so a memory
    backend hiccup never breaks the pipeline's terminal verdict path.
    """
    async def _callback(*, callback_context, **_kw):
        try:
            state = callback_context.state
            facts = _coerce_facts(state.get("facts_json"))
            if not facts:
                return None
            ticket_identifier = state.get("ticket_identifier", "")
            repo = (
                state.get("ticket_project")
                or os.environ.get("AIFORGE_AFM_REPO", "")
                or ""
            )
            session_id = state.get("session_id", "")
            persist_facts(
                facts=facts, repo=repo,
                ticket_identifier=ticket_identifier,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("learner_persist.callback_failed: %s", exc)
        return None
    return _callback


__all__ = [
    "persist_facts",
    "make_learner_after_callback",
    "_coerce_facts",
]
