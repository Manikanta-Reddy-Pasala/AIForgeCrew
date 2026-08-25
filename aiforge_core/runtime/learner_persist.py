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


class _OkrDagDisabled(Exception):
    """Raised to skip OKR-DAG authoring when AIFORGE_OKR_DAG != '1'.

    Caught by the surrounding best-effort ``except Exception`` blocks, so the
    flat compacted-<scope> briefs stay the single OKR memory by default.
    """


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


def _write_one_fact_sqlite(fact: dict, repo: str, ticket_identifier,
                           session_id: str, event_time, sqlmem, out: dict) -> None:
    """Write one fact as a SQLite unit (decision for DECISION: prefix, else
    learning). Skips an empty fact; records a per-fact error on failure."""
    text = (fact.get("text") or "").strip()
    if not text:
        return
    tags = list(fact.get("tags") or [])
    if ticket_identifier:
        tags.append(f"ticket:{ticket_identifier}")
    meta = {"refs": list(fact.get("about") or []), "session_id": session_id}
    try:
        if text.startswith(_DECISION_PREFIX):
            title = text[len(_DECISION_PREFIX):].strip()[:120] or "decision"
            sqlmem.write_unit(text=text, kind="decision", source="learner",
                              title=title, tags=tags, metadata=meta, repo=repo,
                              ticket=ticket_identifier, event_time=event_time)
            out["written_decisions"] += 1
        else:
            sqlmem.write_unit(text=text, kind="learning", source="learner",
                              tags=tags, metadata=meta, repo=repo,
                              ticket=ticket_identifier, event_time=event_time)
            out["written_observations"] += 1
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"sqlite_write_failed: {exc}")


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
        _write_one_fact_sqlite(fact, repo, ticket_identifier, session_id,
                               event_time, _sqlmem, out)
    log.info(
        "learner_persist[sqlite]: repo=%s ticket=%s observations=%d decisions=%d",
        repo, ticket_identifier or "-",
        out["written_observations"], out["written_decisions"],
    )
    return out


def _mirror_one_fact(f, repo: str, session_id: str, md) -> None:
    """Capture one fact into md memory (repo + topic stamped). Skips an empty
    fact."""
    txt = ((f.get("text") if isinstance(f, dict) else str(f)) or "").strip()
    if not txt:
        return
    kind = ("project_learning" if txt.upper().startswith("DECISION:")
            else "learning")
    topic = None
    if isinstance(f, dict):
        topic = f.get("topic") or ((f.get("tags") or [None]) or [None])[0]
    md.capture(kind, txt, repo=repo, topic=topic,
               source=f"learner:{session_id or ''}", ingest=False)


def _mirror_facts_to_md(facts: list, repo: str, session_id: str) -> None:
    """Mirror every fact to an md memory (repo + topic stamped) REGARDLESS of
    backend — the DB/Neo4j write alone never reaches the md compactor, so a
    learning not written to md never rolls up into the project brief or topic
    notes. DECISION:-prefixed facts are repo-scoped project learnings; the rest
    are general learnings. Best-effort, never raises."""
    try:
        from aiforge_core.memory import md_store as _md
        for f in facts:
            _mirror_one_fact(f, repo, session_id, _md)
    except Exception:  # noqa: BLE001 — md mirror is best-effort
        pass


def _solution_theme(f: dict) -> str:
    """The fact's theme: its topic, unless the generic 'task-history', in which
    case the first tag with any 'topic:' prefix stripped."""
    tp = f.get("topic")
    if tp == "task-history":
        tag = ((f.get("tags") or [""]) or [""])[0] or ""
        return tag.split("topic:", 1)[-1] if tag else ""
    return tp or ""


def _author_one_solution(f: dict, repo: str, ticket_identifier, date: str,
                         okr_author) -> None:
    """Author an OKF solution node for one completed feature/fix fact. No-op for
    a fact that is not a solution record."""
    txt = (f.get("text") or "").strip()
    kind = str(f.get("kind") or "").lower()
    is_sol = (kind in ("feature", "fix")
              or txt.upper().startswith("DID:")
              or f.get("topic") == "task-history")
    if not is_sol or not txt:
        return
    summary = txt[4:].strip() if txt.upper().startswith("DID:") else txt
    okr_author.record_solution(
        kind=kind or "feature", summary=summary, workspace=repo,
        topic=_solution_theme(f), tables=f.get("tables"),
        services=f.get("services"), about=f.get("about"),
        files=f.get("files"), ticket=ticket_identifier or "", date=date)


def _author_okr_solutions(facts: list, repo: str, ticket_identifier: "str | None",
                          event_time: "float | None") -> None:
    """For each completed feature/fix (the DID / task-history record) author an
    OKF ``solution`` node + a dated log.md entry — a queryable changelog of what
    was SOLVED, mapped to workspace/repo, topic, and the tables + services the
    change touched. Best-effort; never raises. No-op unless AIFORGE_OKR_DAG=1."""
    try:
        if os.environ.get("AIFORGE_OKR_DAG", "0") != "1":
            raise _OkrDagDisabled
        import datetime as _dt
        from aiforge_core.memory.okf import author as _okr_author
        date = _dt.datetime.fromtimestamp(
            event_time, _dt.UTC).strftime("%Y-%m-%d") if event_time \
            else _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
        for f in facts:
            if isinstance(f, dict):
                _author_one_solution(f, repo, ticket_identifier, date, _okr_author)
    except Exception:  # noqa: BLE001 — solution authoring is best-effort
        pass


_REPO_CARD_FIELD = {"build": "build", "ci-cd": "build", "testing": "test",
                    "structure": "structure", "architecture": "structure",
                    "deploy": "deploy"}


def _card_fields_for(f: dict, repo: str, date: str, oa) -> None:
    """Accrete one structural fact onto the repo card. No-op for a fact with no
    card-worthy field (a non-structural topic and no services/tables)."""
    tp = str(f.get("topic") or "").lower()
    txt = (f.get("text") or "").strip()
    if not txt:
        return
    kw = {"services": f.get("services"), "tables": f.get("tables"), "date": date}
    fld = _REPO_CARD_FIELD.get(tp)
    if fld:
        kw[fld] = txt
    elif tp in ("conventions", "patterns"):
        kw["conventions"] = [txt]
    elif not (f.get("services") or f.get("tables")):
        return          # nothing card-worthy in this fact
    oa.record_repo_profile(repo, **kw)


def _update_repo_card(facts: list, repo: str, event_time: "float | None") -> None:
    """Keep the repo hub profile current from structural facts (build / test /
    structure / deploy); services/tables mentioned accrete onto it. Best-effort;
    only when we know the repo. No-op unless AIFORGE_OKR_DAG=1."""
    try:
        if os.environ.get("AIFORGE_OKR_DAG", "0") != "1":
            raise _OkrDagDisabled
        if not repo or repo in ("notes", "shared", "repo"):
            return
        import datetime as _dt2
        from aiforge_core.memory.okf import author as _oa
        date = _dt2.datetime.fromtimestamp(
            event_time, _dt2.UTC).strftime("%Y-%m-%d") if event_time else ""
        for f in facts:
            if isinstance(f, dict):
                _card_fields_for(f, repo, date, _oa)
    except Exception:  # noqa: BLE001 — card upkeep never breaks the learner
        pass


def _load_afm_store(out: dict, driver):
    """Import AFM's (recall_observations, upsert_decision, upsert_observation)
    — the module was renamed in AiForgeMemory commit 32d86ad, so try both. On
    failure, records the error in ``out``, closes ``driver`` and returns None."""
    try:
        from aiforge_memory.features.memory.store import (
            recall_observations, upsert_decision, upsert_observation)
        return recall_observations, upsert_decision, upsert_observation
    except ImportError:
        pass
    try:
        from aiforge_memory.memory.store import (  # type: ignore
            recall_observations, upsert_decision, upsert_observation)
        return recall_observations, upsert_decision, upsert_observation
    except ImportError:
        out["errors"].append("aiforge_memory_not_installed")
        try:
            driver.close()
        except Exception:
            pass
        return None


def _envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _load_embed_fn():
    """The embedding fn for semantic dedupe, or None (disabled via env, or the
    bge-m3 sidecar import failed). Pure-text dedupe at AFM stays active either
    way."""
    if os.environ.get("AIFORGE_SEMANTIC_DEDUPE", "1") in {"0", "false", ""}:
        return None
    try:
        from aiforge_core.memory.embed import embed as embed_fn  # type: ignore
        return embed_fn
    except Exception as exc:  # noqa: BLE001
        log.debug("semantic dedupe disabled — embed import failed: %s", exc)
        return None


def _nearest_existing(driver, repo: str, text: str, embed_fn,
                      recall_observations) -> "tuple[list, str | None, float]":
    """(embed_vec, similar_id, similar_score) for one fact — the nearest existing
    Observation_v2 in the same repo by cosine similarity. Empty/none when there
    is no embed fn, the text is a DECISION, embedding fails, or recall fails."""
    if embed_fn is None or text.startswith(_DECISION_PREFIX):
        return None, None, 0.0
    try:
        embed_vec = embed_fn(text)
    except Exception as exc:  # noqa: BLE001
        log.debug("embed failed for fact: %s", exc)
        return None, None, 0.0
    if not embed_vec:
        return None, None, 0.0
    try:
        hits = recall_observations(driver, repo=repo, query_vec=embed_vec, k=1)
    except Exception as exc:  # noqa: BLE001
        log.debug("recall_observations failed: %s", exc)
        hits = []
    if not hits:
        return embed_vec, None, 0.0
    top = hits[0]
    return embed_vec, top.get("id"), float(top.get("score") or 0.0)


def _write_one_fact(fact: dict, *, driver, repo: str, ticket_identifier,
                    session_id: str, event_time, embed_fn, hard: float,
                    soft: float, store, out: dict) -> None:
    """Semantic-dedupe + upsert one fact into Neo4j (Observation_v2 or
    Decision_v2), recording the outcome on ``out``.

    Thresholds: sim >= ``hard`` (0.95) -> skip as a duplicate; ``hard`` > sim >=
    ``soft`` (0.85) -> still write, but tag ``superseded-check:<id>`` and mark the
    stale near-dup superseded (last-write-wins, so it stops co-surfacing)."""
    recall_observations, upsert_decision, upsert_observation = store
    text = (fact.get("text") or "").strip()
    if not text:
        return
    about = fact.get("about") or []
    tags = list(fact.get("tags") or [])
    if ticket_identifier:
        tags.append(f"ticket:{ticket_identifier}")
        refs = list(about) + [f"ticket:{ticket_identifier}"]
    else:
        refs = list(about)

    embed_vec, similar_id, similar_score = _nearest_existing(
        driver, repo, text, embed_fn, recall_observations)

    if similar_id and similar_score >= hard:
        # Hard-duplicate — skip the upsert entirely. AFM upsert_observation
        # would have bumped seen_count if texts matched exactly; for paraphrases
        # we don't touch the existing node and simply count it.
        out.setdefault("skipped_semantic_dupes", 0)
        out["skipped_semantic_dupes"] += 1
        log.info(
            "learner_persist.skip_semantic_dupe repo=%s score=%.3f existing=%s",
            repo, similar_score, similar_id)
        return
    supersedes_ids: list[str] = []
    if similar_id and similar_score >= soft:
        tags.append(f"superseded-check:{similar_id}")
        supersedes_ids = [similar_id]

    try:
        if text.startswith(_DECISION_PREFIX):
            title = text[len(_DECISION_PREFIX):].strip()[:120] or "decision"
            upsert_decision(driver, repo=repo, title=title, body=text,
                            rationale="learner-emit", author="learner",
                            session_id=session_id, tags=tags, refs=refs)
            out["written_decisions"] += 1
        else:
            upsert_observation(driver, repo=repo, text=text, kind="learning",
                               author="learner", session_id=session_id,
                               tags=tags, refs=refs, embed_vec=embed_vec,
                               event_time=event_time, supersedes=supersedes_ids)
            out["written_observations"] += 1
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"upsert_failed: {exc}")


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

    _mirror_facts_to_md(facts, repo, session_id)
    _author_okr_solutions(facts, repo, ticket_identifier, event_time)
    _update_repo_card(facts, repo, event_time)

    # Embedded (zero-infra) path — write learnings to the SQLite memory store
    # instead of Neo4j/AFM. Same result-dict shape, never raises.
    from aiforge_core.memory import backend_select as _bsel
    if _bsel.embedded():
        return _persist_facts_embedded(
            facts=facts, repo=repo, ticket_identifier=ticket_identifier,
            session_id=session_id, event_time=event_time)

    driver = _open_driver()
    if driver is None:
        out["errors"].append("neo4j_unreachable")
        return out
    store = _load_afm_store(out, driver)
    if store is None:
        return out

    embed_fn = _load_embed_fn()
    hard = _envf("AIFORGE_SEMANTIC_DEDUPE_HARD", 0.95)
    soft = _envf("AIFORGE_SEMANTIC_DEDUPE_SOFT", 0.85)
    try:
        for fact in facts:
            _write_one_fact(fact, driver=driver, repo=repo,
                            ticket_identifier=ticket_identifier,
                            session_id=session_id, event_time=event_time,
                            embed_fn=embed_fn, hard=hard, soft=soft,
                            store=store, out=out)
    finally:
        try:
            driver.close()
        except Exception:
            pass

    log.info(
        "learner_persist: repo=%s ticket=%s observations=%d decisions=%d errors=%d",
        repo, ticket_identifier or "-",
        out["written_observations"], out["written_decisions"],
        len(out["errors"]))
    return out
def _ticket_event_time(ticket_identifier: str) -> "float | None":
    """Lift a ticket's created_at into an Observation_v2.event_time timestamp so
    bi-temporal queries can hop by "when did this happen?" separate from the
    ingest moment. None when there is no ticket or it can't be read."""
    if not ticket_identifier:
        return None
    try:
        from aiforge_core.tickets.store import get as ticket_get
        t = ticket_get(ticket_identifier)
        ca = getattr(t, "created_at", None) if t else None
        return ca.timestamp() if ca is not None else None
    except Exception:
        return None


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
            repo = (state.get("ticket_project")
                    or os.environ.get("AIFORGE_AFM_REPO", "") or "")
            persist_facts(
                facts=facts, repo=repo,
                ticket_identifier=ticket_identifier,
                session_id=state.get("session_id", ""),
                event_time=_ticket_event_time(ticket_identifier),
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
