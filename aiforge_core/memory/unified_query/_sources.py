"""The individual memory sources unified_query fans out to: ticket brief
(local Postgres + graph_mcp), generic MCP calls, external docs lookup, the
global Neo4j vector/fulltext recall, the AiForgeMemory ContextBundle, prior
chat sessions, and cross-repo CALLS_REPO edges. All lazy-import their
backends and soft-fail; no cross-group imports so no import cycles."""
from __future__ import annotations

import os
from typing import Any


def _ticket_brief(identifier: str) -> dict | None:
    """Fetch ticket brief. Tries local Postgres first (canonical),
    falls back to graph_mcp ticket_brief (Linear/Jira proxy) when
    not found locally.
    """
    local = _ticket_local(identifier)
    if local:
        return local
    res = _mcp_call("ticket_brief", {"id": identifier})
    if not res:
        return None
    rows = _unpack_mcp_rows(res)
    if not rows:
        return None
    first = rows[0]
    first.setdefault("score", 1.0)
    return first


def _ticket_local(identifier: str) -> dict | None:
    """Direct read from local Postgres tickets + recent events.
    Bypasses graph_mcp's external-provider assumption."""
    try:
        import psycopg
        from psycopg.rows import dict_row

        from aiforge_core.config.env import AIFORGE_DSN
    except Exception:
        return None
    # Only the DB round-trip is treated as "ticket unavailable" → None. The
    # result-shaping below is deliberately OUTSIDE this narrow except: a renamed
    # column / key typo (KeyError) must SURFACE (it propagates to the caller,
    # which records it in the unified-query ``errors`` list) instead of being
    # silently indistinguishable from a real DB outage.
    try:
        with psycopg.connect(AIFORGE_DSN, connect_timeout=2,
                             row_factory=dict_row) as c, c.cursor() as cur:
            cur.execute(
                "SELECT id, identifier, title, status, body, "
                "       to_char(created_at,'YYYY-MM-DD HH24:MI') AS created, "
                "       to_char(updated_at,'YYYY-MM-DD HH24:MI') AS updated "
                "FROM tickets WHERE identifier = %s LIMIT 1",
                (identifier,),
            )
            t = cur.fetchone()
            if not t:
                return None
            cur.execute(
                "SELECT agent_role, kind, body, "
                "       to_char(created_at,'HH24:MI:SS') AS ts "
                "FROM ticket_events WHERE ticket_id = %s "
                "ORDER BY created_at DESC LIMIT 8",
                (t["id"],),
            )
            ev = cur.fetchall() or []
    except (psycopg.Error, OSError):
        return None
    ev_lines = "\n".join(
        f"  [{(e['ts'] or '?'):8s} {(e['agent_role'] or '?'):10s} {(e['kind'] or '?'):16s}] "
        f"{((e['body'] or '')[:140]).replace(chr(10),' ')}"
        for e in ev
    )
    text = (
        f"{t['identifier']} · {t['status']} · {t['title']}\n"
        f"Created: {t['created']} · Updated: {t['updated']}\n\n"
        f"Body:\n{(t['body'] or '')[:600]}\n\n"
        f"Recent events:\n{ev_lines or '(none)'}"
    )
    return {"text": text, "score": 1.0, "source_uri": f"ticket:{identifier}"}


def _mcp_call(tool: str, args: dict) -> Any:
    """Inline graph_rag MCP call via the existing sync helper."""
    from aiforge_core.api.api import _call_mcp_sync  # type: ignore
    raw = _call_mcp_sync(tool, args)
    if isinstance(raw, str) and raw.startswith("error:"):
        return None
    return raw


def _docs_lookup(library: str, text: str, *, top_k: int) -> list[dict]:
    from aiforge_core.indexing.docs_index import lookup_doc
    chunks = lookup_doc(library, text, top_k=top_k) or []
    out: list[dict] = []
    for c in chunks:
        out.append({
            "text": c.get("text"),
            "score": float(c.get("score") or 0.5),
            "source_uri": c.get("url"),
        })
    return out


def _unpack_mcp_rows(raw: Any) -> list[dict]:
    """MCP results come as a stringified blob from the graph helper.
    Normalise to a list of ``{text, score?, source_uri?}`` dicts."""
    import json as _j
    if isinstance(raw, str):
        try:
            data = _j.loads(raw)
        except Exception:
            return [{"text": raw[:1000], "score": 0.5}]
    else:
        data = raw
    if isinstance(data, dict):
        # Common shape: {result: {content: [{type:'text', text:'...'}]}}
        content = data.get("content") or data.get("result", {}).get("content")
        if isinstance(content, list):
            return [
                {"text": (b.get("text") or "")[:1500], "score": 0.7}
                for b in content if isinstance(b, dict)
            ]
        # Fallback: flat dict → one row
        return [{"text": _j.dumps(data, ensure_ascii=False)[:1500],
                 "score": 0.5}]
    if isinstance(data, list):
        return [
            {"text": (
                d.get("text") or _j.dumps(d, ensure_ascii=False)[:1500])[:1500],
             "score": float(d.get("score") or 0.5),
             "source_uri": d.get("uri")}
            for d in data if isinstance(d, (dict, str))
        ]
    return [{"text": str(data)[:1500], "score": 0.5}]


_OBS_VECTOR_Q = (
    "CALL db.index.vector.queryNodes('codemem_observation_embed', $k, $v) "
    "YIELD node, score "
    "RETURN node.id AS id, node.text AS text, node.kind AS kind, "
    "node.repo AS repo, score AS score")

_OBS_FULLTEXT_Q = (
    "CALL db.index.fulltext.queryNodes('codemem_observation_ft', $q) "
    "YIELD node, score "
    "RETURN node.id AS id, node.text AS text, node.kind AS kind, "
    "node.repo AS repo, score AS score LIMIT $k")


def _repo_filter(repo: str | None):
    """Scoped recall unions the scoped key (a ticket/page/repo) AND GLOBAL
    (repo-less) memory — so a ticket chat surfaces BOTH that ticket's own facts
    and cross-ticket/global knowledge. Only OTHER repos' facts are excluded.
    (Mirrors the direct recall's "repo = ? OR repo IS NULL".)"""
    from aiforge_core.runtime.repo_ident import normalize_repo as _nr
    want = _nr(repo) if repo else None

    def _ok(rrepo) -> bool:
        if want is None:
            return True
        rn = _nr(rrepo or "")
        return rn in (want, "")
    return _ok, want


def _absorb_obs_rows(records, rows: list, seen: set, ok, cap: int) -> None:
    for r in records:
        if len(rows) >= cap:
            return
        rid = r.get("id")
        if rid and rid not in seen and r.get("text") and ok(r.get("repo")):
            seen.add(rid)
            rows.append({"text": r["text"],
                         "score": float(r.get("score") or 0.5),
                         "kind": r.get("kind"), "repo": r.get("repo")})


def _obs_vector_rows(s, text: str, k: int, rows: list, seen: set, ok,
                     limit: int) -> None:
    try:
        from aiforge_core.memory.embed import embed as _embed
        qv = _embed(text)
    except Exception:  # noqa: BLE001 — embed sidecar down → FT only
        return
    if qv:
        _absorb_obs_rows(s.run(_OBS_VECTOR_Q, k=k, v=qv).data(),
                         rows, seen, ok, limit)


def _obs_fulltext_rows(s, text: str, k: int, rows: list, seen: set, ok,
                       limit: int) -> None:
    """Lexical union (catches exact tokens a paraphrased vector misses)."""
    try:
        _absorb_obs_rows(s.run(_OBS_FULLTEXT_Q, q=text, k=k).data(),
                         rows, seen, ok, limit * 2)
    except Exception:  # noqa: BLE001 — FT query syntax on odd input
        pass


def _global_vector_recall(text: str, *, limit: int,
                          repo: str | None = None) -> list[dict]:
    """Vector+fulltext recall over ``Observation_v2`` on Neo4j. When ``repo`` is
    given, results are FILTERED to that repo (over-fetch, then keep only matching
    — repo-scoped recall with NO cross-repo bleed); when None, repo-agnostic.

    The AFM ContextBundle (source 7) only surfaces vector-recalled observations
    when a ``repo`` is scoped. A GLOBAL memory search (the /api/memory/search UI,
    ingested repos, cross-wing "search everything") passes no repo, so those
    observations were invisible. This queries the ``codemem_observation_embed``
    vector index directly (semantic) and unions the ``codemem_observation_ft``
    fulltext index (lexical) across ALL repos. Soft-fail → []."""
    from aiforge_core.runtime.memory_ingest import _neo4j_driver_or_none
    drv = _neo4j_driver_or_none()
    if drv is None:
        return []
    ok, want = _repo_filter(repo)
    # Over-fetch when scoping so the repo's hits survive the top-k cut even if
    # other repos dominate the raw nearest-neighbours.
    k = min(limit * 6, 60) if want else min(limit, 20)
    rows: list[dict] = []
    seen: set[str] = set()
    try:
        with drv.session() as s:
            _obs_vector_rows(s, text, k, rows, seen, ok, limit)
            _obs_fulltext_rows(s, text, k, rows, seen, ok, limit)
    finally:
        with contextlib.suppress(Exception):
            drv.close()
    return rows


def _bundle_object(text: str, repo: str, role: str | None):
    """The AFM ContextBundle, or None when the backend is unavailable.

    Module renamed api/read.py → api/http.py in AiForgeMemory commit 32d86ad
    (feature-vertical refactor). Both are supported so older installs don't
    break the unified_query loop.
    """
    try:
        from aiforge_memory.api.http import context_bundle_object
    except Exception:  # noqa: BLE001
        try:
            from aiforge_memory.api.read import context_bundle_object
        except Exception:  # noqa: BLE001
            return None
    try:
        return context_bundle_object(text, repo=repo, role=role or "doer",
                                     token_budget=1000)
    except Exception:  # noqa: BLE001
        return None


def _chunk_score() -> float:
    """Code chunks are raw RAG evidence, DEMOTED below the curated sources
    (repo_map/conventions/notes and the OKR-DAG goal context): with a
    goal-oriented memory, a wall of code chunks should not dominate recall.
    Env-tunable AIFORGE_UMEM_CHUNK_SCORE (default 0.4, was 0.85)."""
    try:
        return max(0.0, min(1.0, float(
            os.environ.get("AIFORGE_UMEM_CHUNK_SCORE", "0.4"))))
    except (TypeError, ValueError):
        return 0.4


def _chunk_rows(chunks, repo: str) -> list[dict]:
    score = _chunk_score()
    out = []
    for c in (chunks or [])[:5]:
        path = c.get("file_path") or ""
        body = (c.get("text") or "").strip()
        if not (path and body):
            continue
        out.append({
            # Per-chunk group (not a shared "afm:chunk") so _diversify's
            # per-group cap doesn't drop 5 doer-evidence chunks down to 3.
            "text": f"[afm/chunk {path}]\n{body[:800]}",
            "group": f"afm:chunk:{path}",
            "score": score,
            "source_uri": f"afm://{repo}/{path}",
        })
    return out


def _titled_rows(items, *, repo: str, label: str, group: str, score: float,
                 default_title: str, cap: int, uri_kind: str) -> list[dict]:
    """Notes and docs differ only in their label, weight and default title."""
    out = []
    for it in (items or [])[:3]:
        body = (it.get("body") or "").strip()
        if not body:
            continue
        title = it.get("title") or default_title
        out.append({
            "text": f"[afm/{label} {title}]\n{body[:cap]}",
            "group": group,
            "score": score,
            "source_uri": (it.get("url") or
                           f"afm://{repo}/{uri_kind}/{it.get('id', '')}"),
        })
    return out


def _observation_rows(observations, repo: str) -> list[dict]:
    """Vector-recalled observations (typically agent learnings)."""
    out = []
    for o in (observations or [])[:3]:
        body = (o.get("text") or "").strip()
        if not body:
            continue
        kind = o.get("kind") or "observation"
        out.append({
            "text": f"[afm/{kind}]\n{body[:500]}",
            "group": "afm:observation",
            "score": float(o.get("score") or 0.55),
            "source_uri": f"afm://{repo}/observation/{o.get('id', '')}",
        })
    return out


def _afm_bundle(text: str, *, repo: str, role: str | None) -> list[dict]:
    """Fan-out into AiForgeMemory ContextBundle and flatten its sections
    into ranked rows. Section weights tuned so the most actionable bits
    (relevant chunks > repo_map > conventions > notes/docs > observations)
    surface first. Empty list on any backend failure.

    Sections produced:
    - repo_map        → 1 row, ranked-tag tree
    - conventions_md  → 1 row, .cursorrules
    - chunks          → up to 5 rows, one per anchor-file Chunk_v2
    - notes           → up to 3 rows, MENTIONS-linked Note_v2
    - docs            → up to 3 rows, MENTIONS-linked Doc_v2
    - observations    → up to 3 rows, vector-recalled Observation_v2
    """
    b = _bundle_object(text, repo, role)
    if b is None:
        return []
    out: list[dict] = []
    # Highest-signal first: repo_map (structure summary), then conventions
    # (project rules).
    for value, name, score in ((b.repo_map, "repo_map", 0.95),
                               (b.conventions_md, "conventions", 0.90)):
        if value:
            out.append({"text": f"[afm/{name}]\n{value[:1500]}",
                        "group": f"afm:{name}", "score": score,
                        "source_uri": f"afm://{repo}/{name}"})
    out += _chunk_rows(b.chunks, repo)
    out += _titled_rows(b.notes, repo=repo, label="note", group="afm:note",
                        score=0.70, default_title="Note", cap=600,
                        uri_kind="note")
    out += _titled_rows(b.docs, repo=repo, label="doc", group="afm:doc",
                        score=0.65, default_title="Doc", cap=600,
                        uri_kind="doc")
    out += _observation_rows(b.observations, repo)
    return out


def _chat_sessions(text: str, *, limit: int,
                   exclude_session: int | None = None) -> list[dict]:
    """Flatten prior chat-session message hits into ranked rows (gap F3).

    ``chat_store.search_messages`` returns ``[{session_id, session_title,
    role, content, created_at}]`` already ranked by relevance. We map each
    to a unified hit tagged ``source="chat"`` with a descending raw score so
    the in-source order survives min-max normalization (equal scores would
    collapse to a single value and lose the ranking). Per-session ``group``
    lets ``_diversify`` cap a single chatty session. Soft-fail → []."""
    from aiforge_core.runtime import chat_store
    # Exclude the CURRENT session so a live turn's own messages don't return
    # as "prior chat" (gap M4). Soft-fail if the backend lacks the kwarg.
    try:
        rows = chat_store.search_messages(
            text, limit=limit, exclude_session=exclude_session) or []
    except TypeError:
        rows = chat_store.search_messages(text, limit=limit) or []
    out: list[dict] = []
    n = len(rows)
    for i, r in enumerate(rows):
        content = (r.get("content") or "").strip()
        if not content:
            continue
        sid = r.get("session_id")
        title = r.get("session_title") or "chat"
        role = r.get("role") or "?"
        # Descending raw score preserves search rank through normalization.
        score = 1.0 - (i / max(1, n))
        out.append({
            "text": f"[chat {title} · {role}] {content[:600]}",
            "source": "chat",
            "score": score,
            "group": f"chat:{sid}",
            # Per-MESSAGE uri (index-qualified) — distinct messages in one
            # session are NOT the same doc, so dedup must not collapse them.
            "source_uri": f"chat://{sid}/{i}",
        })
    return out


def _cross_repo_links(text: str, *, repo: str) -> list[dict]:
    """Surface AiForgeMemory CALLS_REPO edges touching ``repo`` (gap A7).

    Cross-repo retrieval: a ticket scoped to repo X normally can't see
    that repo Y calls into it. AiForgeMemory already computes these
    ``(Repo)-[:CALLS_REPO {via, confidence}]->(Repo)`` edges; we read
    them back via the link store and flatten to ranked rows so they
    participate in unified ranking.

    Reuses the same soft-fail import shim style as ``_afm_bundle`` —
    returns ``[]`` on any backend/import failure (never raises). The
    ``text`` arg is accepted for signature parity / future filtering but
    edges are repo-scoped, not text-scoped.

    Each row: ``{"text": "[xrepo] <from> --<via>--> <to> (conf <c>)",
    "score": <c>, "source": "xrepo"}``.
    """
    try:
        from aiforge_memory.api.commands._driver import driver
        from aiforge_memory.features.link.store import list_edges
    except Exception:
        return []
    try:
        drv = driver()
        edges = list_edges(drv, repo=repo) or []
    except Exception:
        return []

    out: list[dict] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        src = e.get("src") or "?"
        dst = e.get("dst") or "?"
        via = e.get("via") or "?"
        try:
            conf = float(e.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        out.append({
            "text": f"[xrepo] {src} --{via}--> {dst} (conf {conf:.2f})",
            "score": conf,
            "source": "xrepo",
        })
    return out
