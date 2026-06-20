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
    from aiforge_core.memory.neo4j_conn import neo4j_params
    uri, user, pw = neo4j_params()
    try:
        return GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.warning("learner_persist.driver_failed: %s", exc)
        return None


def _persist_facts_embedded(
    *, facts: list[dict], repo: str, ticket_identifier: str | None,
    session_id: str, event_time: float | None,
) -> dict:
    """SQLite-backed Learner persistence (zero-infra). DECISION: facts
    become ``decision`` units, the rest ``learning`` units. Mirrors the
    Neo4j path's result-dict shape; soft-fails per fact."""
    from aiforge_core.memory import sqlite_memory as _sqlmem
    out = {"written_observations": 0, "written_decisions": 0, "errors": []}
    for fact in facts:
        text = (fact.get("text") or "").strip()
        if not text:
            continue
        tags = list(fact.get("tags") or [])
        if ticket_identifier:
            tags.append(f"ticket:{ticket_identifier}")
        refs = list(fact.get("about") or [])
        try:
            if text.startswith(_DECISION_PREFIX):
                title = text[len(_DECISION_PREFIX):].strip()[:120] or "decision"
                _sqlmem.write_unit(
                    text=text, kind="decision", source="learner", title=title,
                    tags=tags, metadata={"refs": refs, "session_id": session_id},
                    repo=repo, ticket=ticket_identifier, event_time=event_time,
                )
                out["written_decisions"] += 1
            else:
                _sqlmem.write_unit(
                    text=text, kind="learning", source="learner",
                    tags=tags, metadata={"refs": refs, "session_id": session_id},
                    repo=repo, ticket=ticket_identifier, event_time=event_time,
                )
                out["written_observations"] += 1
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"sqlite_write_failed: {exc}")
    log.info(
        "learner_persist[sqlite]: repo=%s ticket=%s observations=%d decisions=%d",
        repo, ticket_identifier or "-",
        out["written_observations"], out["written_decisions"],
    )
    return out


def persist_facts(
    *,
    facts: list[dict],
    repo: str,
    ticket_identifier: str | None = None,
    session_id: str = "",
    event_time: float | None = None,
) -> dict:
    """Persist a list of Learner-emitted facts into Neo4j as
    Observation_v2 (default) or Decision_v2 (DECISION: prefix).

    ``event_time`` (gap-7) is the epoch-seconds timestamp of when the
    underlying ticket / run actually happened. When supplied it lands
    on every Observation_v2 we write, separate from the ingest
    ``created_at``, so bi-temporal Cypher hops (``WHERE
    o.event_time > datetime('2025-...')``) work. Decisions don't carry
    event_time yet; they're typically "decided now".

    Returns ``{written_observations, written_decisions, errors}``.
    Soft-fails on any backend error — never raises into the agent loop.
    """
    out = {"written_observations": 0, "written_decisions": 0, "errors": []}
    if _is_disabled():
        out["errors"].append("disabled_via_env")
        return out
    if not facts or not repo:
        return out

    # Embedded (zero-infra) path — write learnings to the SQLite memory
    # store instead of Neo4j/AFM. Same result-dict shape, never raises.
    from aiforge_core.memory import backend_select as _bsel
    if _bsel.embedded():
        return _persist_facts_embedded(
            facts=facts, repo=repo, ticket_identifier=ticket_identifier,
            session_id=session_id, event_time=event_time,
        )

    driver = _open_driver()
    if driver is None:
        out["errors"].append("neo4j_unreachable")
        return out

    # Module renamed in AiForgeMemory commit 32d86ad — try both.
    try:
        from aiforge_memory.features.memory.store import (
            recall_observations, upsert_decision, upsert_observation,
        )
    except ImportError:
        try:
            from aiforge_memory.memory.store import (  # type: ignore
                recall_observations, upsert_decision, upsert_observation,
            )
        except ImportError:
            out["errors"].append("aiforge_memory_not_installed")
            try:
                driver.close()
            except Exception:
                pass
            return out

    # Semantic dedupe (v2): when bge-m3 sidecar is reachable, compute
    # each fact's embedding once and check the existing Observation_v2
    # vector index for a near-duplicate in the same repo before write.
    # ``AFM PR #4`` already deduplicates on exact ``(repo, text)``
    # match; this adds the cosine-similarity layer on top so paraphrased
    # restatements ("README had 3 occurrences of X" vs "README contained
    # 3 X references") also collapse instead of bloating the graph.
    #
    # Thresholds:
    #   sim ≥ ``AIFORGE_SEMANTIC_DEDUPE_HARD`` (default 0.95) → skip
    #     entirely; treat as duplicate of the existing fact.
    #   ``HARD`` > sim ≥ ``AIFORGE_SEMANTIC_DEDUPE_SOFT`` (default 0.85)
    #     → still write, but tag the new fact with
    #     ``superseded-check:<existing_id>`` so a human / future
    #     compactor can reconcile or merge.
    #
    # Disable wholesale with ``AIFORGE_SEMANTIC_DEDUPE=0`` — pure-text
    # dedupe at AFM remains active either way.
    semantic_dedupe = (
        os.environ.get("AIFORGE_SEMANTIC_DEDUPE", "1") not in {"0", "false", ""}
    )
    hard = float(os.environ.get("AIFORGE_SEMANTIC_DEDUPE_HARD", "0.95"))
    soft = float(os.environ.get("AIFORGE_SEMANTIC_DEDUPE_SOFT", "0.85"))

    embed_fn = None
    if semantic_dedupe:
        try:
            from aiforge_core.memory.embed import embed as embed_fn  # type: ignore
        except Exception as exc:  # noqa: BLE001
            log.debug("semantic dedupe disabled — embed import failed: %s", exc)
            embed_fn = None

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

            embed_vec: list[float] | None = None
            similar_id: str | None = None
            similar_score: float = 0.0
            if embed_fn is not None and not text.startswith(_DECISION_PREFIX):
                try:
                    embed_vec = embed_fn(text)
                except Exception as exc:  # noqa: BLE001
                    log.debug("embed failed for fact: %s", exc)
                    embed_vec = None
                if embed_vec:
                    try:
                        hits = recall_observations(
                            driver, repo=repo, query_vec=embed_vec, k=1,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("recall_observations failed: %s", exc)
                        hits = []
                    if hits:
                        top = hits[0]
                        similar_score = float(top.get("score") or 0.0)
                        similar_id = top.get("id")

            if similar_id and similar_score >= hard:
                # Hard-duplicate — skip the upsert entirely. AFM
                # ``upsert_observation`` would have bumped seen_count
                # if texts matched exactly; for paraphrases we don't
                # touch the existing node and simply count it.
                out.setdefault("skipped_semantic_dupes", 0)
                out["skipped_semantic_dupes"] += 1
                log.info(
                    "learner_persist.skip_semantic_dupe repo=%s score=%.3f existing=%s",
                    repo, similar_score, similar_id,
                )
                continue
            supersedes_ids: list[str] = []
            if similar_id and similar_score >= soft:
                tags.append(f"superseded-check:{similar_id}")
                # Gap #2: last-write-wins — the fresh fact supersedes the
                # stale near-dup so it stops co-surfacing in recall.
                supersedes_ids = [similar_id]

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
                        embed_vec=embed_vec,
                        event_time=event_time,
                        supersedes=supersedes_ids,
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
            # Gap-7 wire: lift the ticket's created_at into Observation_v2.event_time
            # so bi-temporal queries can hop by "when did this happen?"
            # separate from the ingest moment.
            event_time = None
            if ticket_identifier:
                try:
                    from aiforge_core.tickets.store import get as ticket_get
                    t = ticket_get(ticket_identifier)
                    ca = getattr(t, "created_at", None) if t else None
                    if ca is not None:
                        event_time = ca.timestamp()
                except Exception:
                    event_time = None
            persist_facts(
                facts=facts, repo=repo,
                ticket_identifier=ticket_identifier,
                session_id=session_id,
                event_time=event_time,
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
