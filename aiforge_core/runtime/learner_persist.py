"""Persist the Learner's ``facts_json`` output into the memory layer.

The Learner archetype emits ``state['facts_json']`` — a JSON array of
``{text, about, tags}`` dicts — only on ``feedback_verdict.verdict ==
'pass'``. This module wires those facts into the embedded SQLite memory
store:

1. Writing each fact as a ``learning`` unit via ``sqlite_memory``
   (tags + ticket id).
2. Writing a ``decision`` unit when the fact text starts with
   ``DECISION:`` — keeps the small bar for promoting an observation into
   a durable architectural choice.
3. Mirroring every fact into md memory so it rolls up into the topic /
   project briefs.

Wire as the Learner agent's ``after_agent_callback`` in
``runtime/pipeline.py``.

Env knobs:
  AIFORGE_LEARNER_PERSIST_DISABLE=1   skip persistence (debug)
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
    become ``decision`` units, the rest ``learning`` units. Soft-fails
    per fact."""
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
    """Mirror every fact to an md memory (repo + topic stamped) — the SQLite
    write alone never reaches the md compactor, so a learning not written to md
    never rolls up into the project brief or topic notes. DECISION:-prefixed
    facts are repo-scoped project learnings; the rest are general learnings.
    Best-effort, never raises."""
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


def persist_facts(
    *,
    facts: list[dict],
    repo: str,
    ticket_identifier: str | None = None,
    session_id: str = "",
    event_time: float | None = None,
) -> dict:
    """Persist a list of Learner-emitted facts into the embedded SQLite
    memory store as ``learning`` (default) or ``decision`` (DECISION:
    prefix) units.

    ``event_time`` is the epoch-seconds timestamp of when the underlying
    ticket / run actually happened. When supplied it lands on every unit
    we write, separate from the ingest ``created_at``.

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

    # Embedded (zero-infra) path — write learnings to the SQLite memory store.
    return _persist_facts_embedded(
        facts=facts, repo=repo, ticket_identifier=ticket_identifier,
        session_id=session_id, event_time=event_time)


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
