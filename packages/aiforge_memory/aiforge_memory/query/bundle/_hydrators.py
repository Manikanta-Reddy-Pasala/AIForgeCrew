from __future__ import annotations

from aiforge_memory.core.neo4j import vector_overfetch_k


_Q_DOMAINS = """
MATCH (d:Domain {repo:$repo})
OPTIONAL MATCH (d)-[:COVERS]->(s:Service)
WITH d, collect(DISTINCT s.name) AS services
RETURN d.name AS name, d.description AS description, services
ORDER BY size(services) DESC, d.name LIMIT 5
"""

_Q_FLOWS = """
MATCH (fl:Flow {repo:$repo})-[st:STEP]->(y:Symbol_v2)
WHERE y.fqname IN $fqnames
WITH fl, st ORDER BY st.order
WITH fl, collect(st.label) AS steps
RETURN fl.name AS name, fl.description AS description, steps LIMIT 5
"""


def _domains_for(driver, *, repo: str) -> list[dict]:
    with driver.session() as s:
        return [dict(r) for r in s.run(_Q_DOMAINS, repo=repo)]


def _flows_for(driver, *, repo: str, fqnames: list[str]) -> list[dict]:
    if not fqnames:
        return []
    with driver.session() as s:
        return [dict(r) for r in s.run(_Q_FLOWS, repo=repo, fqnames=fqnames)]


def _services_rows(driver, *, repo: str, names: list[str]) -> list[dict]:
    cy = (
        "MATCH (s:Service {repo:$repo}) WHERE s.name IN $names "
        "RETURN s.name AS name, s.role AS role, s.description AS description, "
        "       s.port AS port, s.tech_stack AS tech_stack, s.source AS source"
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, names=names)]


def _files_rows(driver, *, repo: str, paths: list[str]) -> list[dict]:
    cy = (
        "MATCH (f:File_v2 {repo:$repo}) WHERE f.path IN $paths "
        "RETURN f.path AS path, f.lang AS lang, f.lines AS lines, "
        "       coalesce(f.summary,'') AS summary, "
        "       coalesce(f.purpose_tags,[]) AS purpose_tags"
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, paths=paths)]


_SYM_FIELDS = (
    " s.fqname AS fqname, s.kind AS kind, "
    " s.file_path AS file_path, s.signature AS signature, "
    " coalesce(s.summary, '') AS summary, "
    " coalesce(s.doc_first_line, '') AS doc, "
    " coalesce(s.modifiers, []) AS modifiers, "
    " coalesce(s.deprecated, false) AS deprecated, "
    " coalesce(s.return_type, '') AS return_type, "
    " coalesce(s.params_json, '') AS params_json "
)


def _symbols_rows(driver, *, repo: str, fqnames: list[str]) -> list[dict]:
    cy = (
        "MATCH (s:Symbol_v2 {repo:$repo}) WHERE s.fqname IN $fqnames "
        "RETURN" + _SYM_FIELDS
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, fqnames=fqnames)]


def _symbols_by_terminal_name(driver, *, repo: str, name: str) -> list[dict]:
    cy = (
        "MATCH (s:Symbol_v2 {repo:$repo}) "
        "WHERE s.fqname ENDS WITH $suffix "
        "RETURN" + _SYM_FIELDS + " LIMIT 6"
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, suffix=f"::{name}")]


def _call_neighbours(
    driver, *, repo: str, fqname: str, hops: int = 1,
) -> tuple[list[dict], list[dict]]:
    callers_cy = (
        "MATCH (caller:Symbol_v2 {repo:$repo})-[:CALLS]->(t:Symbol_v2 {fqname:$fq}) "
        "RETURN caller.fqname AS fqname LIMIT 8"
    )
    callees_cy = (
        "MATCH (s:Symbol_v2 {repo:$repo, fqname:$fq})-[:CALLS]->(callee:Symbol_v2) "
        "RETURN callee.fqname AS fqname LIMIT 8"
    )
    with driver.session() as sess:
        callers = [
            {"fqname": r["fqname"], "target": fqname}
            for r in sess.run(callers_cy, repo=repo, fq=fqname)
        ]
        callees = [
            {"fqname": r["fqname"], "source": fqname}
            for r in sess.run(callees_cy, repo=repo, fq=fqname)
        ]
    return callers, callees


def _repo_docs_for(driver, *, repo: str) -> tuple[str, str]:
    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) "
            "RETURN coalesce(r.runbook_md,'') AS rb, coalesce(r.conventions_md,'') AS cm",
            n=repo,
        ).single()
    if row:
        return row["rb"], row["cm"]
    return "", ""


def _repo_map_for(
    driver, *, repo: str, focal_paths: list[str],
    errors: list[str] | None = None,
) -> str:
    # Build a simple tree-like string of files and symbols for focal paths
    cy = (
        "MATCH (f:File_v2 {repo:$repo}) "
        "WHERE f.path IN $paths "
        "OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol_v2) "
        "RETURN f.path AS path, collect(s.fqname) AS symbols"
    )
    try:
        lines = []
        with driver.session() as s:
            for r in s.run(cy, repo=repo, paths=focal_paths):
                path = r["path"]
                lines.append(f"{path}:")
                for sym in r["symbols"]:
                    if sym:
                        lines.append(f"  - {sym.split('::')[-1]}")
        return "\n".join(lines)
    except Exception as exc:
        if errors is not None:
            errors.append(f"repo_map: {exc}")
        return ""


def _chunks_for(
    driver, *, repo: str, paths: list[str], limit: int = 5,
    errors: list[str] | None = None,
) -> list[dict]:
    if not paths:
        return []
    cy = (
        "MATCH (f:File_v2 {repo:$repo})-[:CHUNKED_AS]->(c:Chunk_v2) "
        "WHERE f.path IN $paths "
        "RETURN c.file_path AS file_path, c.text AS text "
        "ORDER BY c.line_start ASC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(cy, repo=repo, paths=paths, limit=limit)]
    except Exception as exc:
        if errors is not None:
            errors.append(f"chunks: {exc}")
        return []


def _decisions_for(
    driver, *, repo: str, paths: list[str], fqnames: list[str], limit: int = 5,
    errors: list[str] | None = None,
) -> list[dict]:
    """Decisions whose MENTIONS edges land on any anchor file/symbol,
    OR are repo-wide and active. Newest first."""
    cy = (
        "MATCH (d:Decision_v2 {repo:$repo}) "
        "WHERE d.status IN ['active','superseded'] AND ( "
        "  EXISTS { MATCH (d)-[:MENTIONS]->(f:File_v2 {repo:$repo}) "
        "           WHERE f.path IN $paths } OR "
        "  EXISTS { MATCH (d)-[:MENTIONS]->(s:Symbol_v2 {repo:$repo}) "
        "           WHERE s.fqname IN $fqnames } OR "
        "  NOT EXISTS { MATCH (d)-[:MENTIONS]->() } "
        ") "
        "RETURN d.id AS id, d.title AS title, "
        "       coalesce(d.rationale,'') AS rationale, "
        "       coalesce(d.body,'') AS body, "
        "       coalesce(d.status,'active') AS status, "
        "       coalesce(d.tags,[]) AS tags "
        "ORDER BY d.created_at DESC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(
                cy, repo=repo, paths=paths or [""],
                fqnames=fqnames or [""], limit=limit,
            )]
    except Exception as exc:
        if errors is not None:
            errors.append(f"decisions: {exc}")
        return []


def _vector_observations(
    driver, *, repo: str, query_vec: list[float], k: int = 5,
    errors: list[str] | None = None,
) -> list[dict]:
    """Pull the bundle's vector-recalled observations.

    Uses the PPR-lite reranker from
    :func:`aiforge_memory.features.memory.store.recall_observations_ppr`
    so an Observation_v2 that shares a ``:MENTIONS`` neighbour with the
    seed (file/symbol) ranks alongside ones with high direct vector
    similarity. Falls back to vanilla vector recall when the PPR query
    fails (e.g. vector index missing on legacy installs)."""
    try:
        from aiforge_memory.features.memory.store import (
            recall_observations_ppr,
        )
        rows = recall_observations_ppr(
            driver, repo=repo, query_vec=query_vec,
            k=k, seed_k=max(k * 3, 15), alpha=0.7,
        )
        if rows:
            return rows
    except Exception as exc:
        # fall through to vanilla recall
        if errors is not None:
            errors.append(f"vector_observations(ppr): {exc}")

    # Over-fetch the global vector stage — Neo4j filters repo *after*
    # ranking, so $k alone can come back empty on multi-repo graphs.
    cy = (
        "CALL db.index.vector.queryNodes('codemem_observation_embed', $k_query, $vec) "
        "YIELD node AS o, score "
        "WHERE o.repo = $repo "
        "RETURN o.id AS id, coalesce(o.kind,'note') AS kind, "
        "       o.text AS text, coalesce(o.tags,[]) AS tags, score "
        "ORDER BY score DESC LIMIT $k"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(
                cy, repo=repo, vec=query_vec,
                k=k, k_query=vector_overfetch_k(k),
            )]
    except Exception as exc:
        if errors is not None:
            errors.append(f"vector_observations: {exc}")
        return []


def _observations_for(
    driver, *, repo: str, paths: list[str], fqnames: list[str], limit: int = 5,
    errors: list[str] | None = None,
) -> list[dict]:
    """Observations linked to anchor files/symbols. Vector recall is
    handled by translator; here we use direct MENTIONS edges only."""
    cy = (
        "MATCH (o:Observation_v2 {repo:$repo}) "
        "WHERE EXISTS { MATCH (o)-[:MENTIONS]->(f:File_v2 {repo:$repo}) "
        "               WHERE f.path IN $paths } OR "
        "      EXISTS { MATCH (o)-[:MENTIONS]->(s:Symbol_v2 {repo:$repo}) "
        "               WHERE s.fqname IN $fqnames } "
        "RETURN o.id AS id, coalesce(o.kind,'note') AS kind, "
        "       o.text AS text, coalesce(o.tags,[]) AS tags "
        "ORDER BY o.created_at DESC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(
                cy, repo=repo, paths=paths or [""],
                fqnames=fqnames or [""], limit=limit,
            )]
    except Exception as exc:
        if errors is not None:
            errors.append(f"observations: {exc}")
        return []



def _notes_for(
    driver, *, repo: str, paths: list[str], fqnames: list[str], limit: int = 5,
    errors: list[str] | None = None,
) -> list[dict]:
    if not paths and not fqnames:
        return []
    cy = (
        "MATCH (n:Note_v2 {repo:$repo}) "
        "WHERE EXISTS { MATCH (n)-[:MENTIONS]->(f:File_v2 {repo:$repo}) "
        "               WHERE f.path IN $paths } OR "
        "      EXISTS { MATCH (n)-[:MENTIONS]->(s:Symbol_v2 {repo:$repo}) "
        "               WHERE s.fqname IN $fqnames } "
        "RETURN n.id AS id, coalesce(n.title,'') AS title, "
        "       coalesce(n.body,'') AS body, coalesce(n.tags,[]) AS tags "
        "ORDER BY n.created_at DESC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(
                cy, repo=repo, paths=paths, fqnames=fqnames, limit=limit,
            )]
    except Exception as exc:
        if errors is not None:
            errors.append(f"notes: {exc}")
        return []


def _docs_for(
    driver, *, repo: str, paths: list[str], fqnames: list[str], limit: int = 5,
    errors: list[str] | None = None,
) -> list[dict]:
    if not paths and not fqnames:
        return []
    cy = (
        "MATCH (d:Doc_v2 {repo:$repo}) "
        "WHERE EXISTS { MATCH (d)-[:MENTIONS]->(f:File_v2 {repo:$repo}) "
        "               WHERE f.path IN $paths } OR "
        "      EXISTS { MATCH (d)-[:MENTIONS]->(s:Symbol_v2 {repo:$repo}) "
        "               WHERE s.fqname IN $fqnames } "
        "RETURN d.id AS id, coalesce(d.title,'') AS title, "
        "       coalesce(d.body,'') AS body, coalesce(d.url,'') AS url "
        "ORDER BY d.created_at DESC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(
                cy, repo=repo, paths=paths, fqnames=fqnames, limit=limit,
            )]
    except Exception as exc:
        if errors is not None:
            errors.append(f"docs: {exc}")
        return []


def _cross_repo_for(
    driver, *, repo: str, limit: int = 8,
    errors: list[str] | None = None,
) -> list[dict]:
    """Edges where this repo is on either side. Highest confidence first."""
    cy = (
        "MATCH (a:Repo)-[r:CALLS_REPO]->(b:Repo) "
        "WHERE a.name = $repo OR b.name = $repo "
        "RETURN a.name AS src, b.name AS dst, r.via AS via, "
        "       coalesce(r.confidence, 0.0) AS confidence, "
        "       coalesce(r.evidence, []) AS evidence "
        "ORDER BY confidence DESC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(cy, repo=repo, limit=limit)]
    except Exception as exc:
        if errors is not None:
            errors.append(f"cross_repo: {exc}")
        return []
