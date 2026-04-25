"""Cypher query templates for the v5 code graph.

Thin, typed wrappers around frequently-used Cypher patterns. Callers pass a
``neo4j.Driver`` and get back plain dicts (or ``None``) — no driver objects
leak past these functions, so they're safe to call from agent tools.

All queries are LIMIT-bounded to keep tool budgets predictable.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("aiforge.memory.cypher_templates")


_LOOKUP_LIMIT = 20
_CALLERS_LIMIT = 20


# ─────────────── lookup_symbol ───────────────

# Match by exact fqn first, then fall back to short-name match. The OR is
# expressed as a UNION to let the planner use ``symbol_fqn`` index for the
# fast path before scanning by ``simple``.
_LOOKUP_SYMBOL_FQN = (
    "MATCH (s:Symbol {fqn: $needle}) "
    "WHERE $repo IS NULL OR s.repo = $repo "
    "RETURN s.fqn AS fqn, s.simple AS simple, s.kind AS kind, "
    "       s.file_path AS file_path, s.repo AS repo, s.start_line AS start_line, "
    "       s.end_line AS end_line, 1.0 AS match_score "
    "LIMIT $limit"
)

_LOOKUP_SYMBOL_SIMPLE = (
    "MATCH (s:Symbol) "
    "WHERE s.simple = $needle "
    "  AND ($repo IS NULL OR s.repo = $repo) "
    "RETURN s.fqn AS fqn, s.simple AS simple, s.kind AS kind, "
    "       s.file_path AS file_path, s.repo AS repo, s.start_line AS start_line, "
    "       s.end_line AS end_line, 0.5 AS match_score "
    "LIMIT $limit"
)


def lookup_symbol(
    driver, name: str, repo: str | None = None, limit: int = _LOOKUP_LIMIT
) -> list[dict[str, Any]]:
    """Find a :Symbol by exact ``fqn`` or short ``simple`` name.

    Exact-fqn matches are returned first (match_score=1.0), then short-name
    matches (match_score=0.5). De-duplicated by fqn.
    """
    if not name:
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    with driver.session() as session:
        for cypher in (_LOOKUP_SYMBOL_FQN, _LOOKUP_SYMBOL_SIMPLE):
            for r in session.run(cypher, needle=name, repo=repo, limit=limit):
                fqn = r["fqn"]
                if fqn in seen:
                    continue
                seen.add(fqn)
                out.append(dict(r))
                if len(out) >= limit:
                    return out
    return out


# ─────────────── find_callers ───────────────

_FIND_CALLERS = (
    "MATCH (callee:Symbol {fqn: $fqn})<-[:CALLS]-(caller:Symbol) "
    "RETURN caller.fqn AS fqn, caller.simple AS simple, caller.kind AS kind, "
    "       caller.file_path AS file_path, caller.repo AS repo, "
    "       caller.start_line AS start_line "
    "LIMIT $limit"
)


def find_callers(
    driver, fqn: str, limit: int = _CALLERS_LIMIT
) -> list[dict[str, Any]]:
    """Return up to ``limit`` :Symbol nodes that ``:CALLS`` the target fqn."""
    if not fqn:
        return []
    with driver.session() as session:
        return [dict(r) for r in session.run(_FIND_CALLERS, fqn=fqn, limit=limit)]


# ─────────────── find_definition ───────────────

# A symbol is :DEFINES'd by exactly one :File (the containing source). We
# follow the relation in either direction so callers don't need to know the
# orientation.
_FIND_DEFINITION = (
    "MATCH (s:Symbol {fqn: $fqn}) "
    "OPTIONAL MATCH (f:File)-[:DEFINES]->(s) "
    "RETURN s.fqn AS fqn, s.simple AS simple, s.kind AS kind, "
    "       s.file_path AS symbol_file_path, s.start_line AS start_line, "
    "       s.end_line AS end_line, "
    "       f.path AS file_path, f.repo AS file_repo, f.sha1 AS file_sha1 "
    "LIMIT 1"
)


def find_definition(driver, fqn: str) -> dict[str, Any] | None:
    """Return the :Symbol + its defining :File, or ``None`` if absent."""
    if not fqn:
        return None
    with driver.session() as session:
        rec = session.run(_FIND_DEFINITION, fqn=fqn).single()
    return dict(rec) if rec else None
