"""ContextBundle builder — translator → Cypher → token-budget pack.

Public surface:
    bundle.query(text, *, repo, driver, role='doer', token_budget=4000)
        -> ContextBundle
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aiforge_memory.core.neo4j import vector_overfetch_k
from aiforge_memory.query import fastpath, translator


def _count_tokens(text: str) -> int:
    """Exact tokens via tiktoken cl100k_base; chars/4 fallback if not installed."""
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except ImportError:
        return len(text) // 4


@dataclass
class ContextBundle:
    repo: str = ""
    intent: str = ""
    fastpath_hit: str = ""           # kind:value if matched
    services: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)
    callers: list[dict] = field(default_factory=list)
    callees: list[dict] = field(default_factory=list)
    runbook_md: str = ""
    conventions_md: str = ""
    repo_map: str = ""
    # Memory layer
    decisions: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    docs: list[dict] = field(default_factory=list)
    # Cross-repo edges crossing this query's surface
    cross_repo: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    # Semantic domains (repo-level) + flows touching the query's symbols
    domains: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Markdown-ready prompt section. Empty pieces are skipped."""
        out: list[str] = []

        if self.intent:
            out.append(f"## Intent\n- {self.intent}")
        if self.fastpath_hit:
            out.append(f"## Fastpath\n- {self.fastpath_hit}")

        if self.services:
            lines = ["## Services"]
            for svc in self.services[:3]:
                lines.append(
                    f"- **{svc['name']}** ({svc.get('role','?')})"
                    f" — {svc.get('description','')}"
                )
            out.append("\n".join(lines))

        if self.domains:
            lines = ["## Domains"]
            for d in self.domains[:5]:
                desc = (d.get("description") or "").strip()
                svcs = ", ".join(d.get("services", [])[:6])
                tail = (f" — {desc}" if desc else "") + (f" [{svcs}]" if svcs else "")
                lines.append(f"- **{d['name']}**{tail}")
            out.append("\n".join(lines))

        if self.flows:
            lines = ["## Flows"]
            for f in self.flows[:5]:
                steps = " → ".join(f.get("steps", [])[:8])
                lines.append(f"- **{f['name']}**: {steps}")
            out.append("\n".join(lines))

        if self.runbook_md:
            out.append("## Runbook (top of repo)\n" + self.runbook_md[:2000])

        if self.conventions_md:
            out.append("## Conventions (.cursorrules)\n" + self.conventions_md[:2000])

        if self.repo_map:
            out.append("## Repo Map\n```\n" + self.repo_map + "\n```")

        if self.chunks:
            lines = ["## Relevant Code Chunks"]
            for c in self.chunks[:5]:
                # Compact output
                path = c.get("file_path", "")
                text = (c.get("text") or "").strip()
                if path and text:
                    lines.append(f"### `{path}`\n```\n{text}\n```")
            out.append("\n".join(lines))

        if self.files:
            lines = ["## Anchor files"]
            for f in self.files[:8]:
                summary = (f.get("summary") or "").strip()
                if summary:
                    lines.append(f"- `{f['path']}` — {summary}")
                else:
                    lines.append(f"- `{f['path']}`")
            out.append("\n".join(lines))

        if self.symbols:
            import json as _json
            lines = ["## Symbols"]
            for s in self.symbols[:12]:
                sig = s.get("signature", "")
                summ = (s.get("summary") or "").strip()
                fallback = (s.get("doc") or "").strip()
                describe = summ or fallback
                tag = " ⚠ DEPRECATED" if s.get("deprecated") else ""
                # Compact params line — agent-friendly stub generation.
                params_str = ""
                pj = s.get("params_json") or ""
                if pj:
                    try:
                        ps = _json.loads(pj)
                        if isinstance(ps, list) and ps:
                            params_str = " params: " + ", ".join(
                                f"{p.get('name','?')}:{p.get('type','?')}"
                                for p in ps[:8]
                            )
                    except (ValueError, TypeError):
                        pass
                if describe:
                    lines.append(
                        f"- `{s['fqname']}`{tag} — `{sig}` — {describe}{params_str}"
                    )
                else:
                    lines.append(f"- `{s['fqname']}`{tag} — `{sig}`{params_str}")
            out.append("\n".join(lines))

        if self.callers or self.callees:
            lines = ["## Call neighbours"]
            for c in (self.callers or [])[:6]:
                lines.append(f"- caller of {c['target']}: `{c['fqname']}`")
            for c in (self.callees or [])[:6]:
                lines.append(f"- callee of {c['source']}: `{c['fqname']}`")
            out.append("\n".join(lines))

        if self.decisions:
            lines = ["## Decisions"]
            for d in self.decisions[:5]:
                title = d.get("title", "")
                rationale = (d.get("rationale") or "").strip()
                status = d.get("status") or "active"
                head = f"- **{title}** ({status})"
                if rationale:
                    head += f" — {rationale[:200]}"
                lines.append(head)
            out.append("\n".join(lines))

        if self.observations:
            lines = ["## Observations"]
            for o in self.observations[:5]:
                kind = o.get("kind") or "note"
                text = (o.get("text") or "").strip()
                lines.append(f"- *{kind}* — {text[:240]}")
            out.append("\n".join(lines))

        if self.notes:
            lines = ["## Notes"]
            for n in self.notes[:5]:
                title = n.get("title") or "Note"
                body = (n.get("body") or "").strip()
                lines.append(f"- **{title}** — {body[:240]}")
            out.append("\n".join(lines))

        if self.docs:
            lines = ["## External Docs"]
            for d in self.docs[:5]:
                title = d.get("title") or "Doc"
                url = d.get("url") or ""
                body = (d.get("body") or "").strip()
                lines.append(f"- **{title}** ({url}) — {body[:240]}")
            out.append("\n".join(lines))

        if self.cross_repo:
            lines = ["## Related repos"]
            for e in self.cross_repo[:5]:
                ev = ", ".join(e.get("evidence", [])[:3])
                lines.append(
                    f"- `{e['src']}` → `{e['dst']}` via {e['via']} "
                    f"(conf {e.get('confidence',0):.2f}; {ev})"
                )
            out.append("\n".join(lines))

        if self.sources_used:
            out.append("_sources: " + ", ".join(self.sources_used) + "_")
        if self.errors:
            out.append("_errors: " + "; ".join(self.errors) + "_")

        return "\n\n".join(out)


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


def query(
    text: str,
    *,
    repo: str,
    driver,
    role: str = "doer",
    token_budget: int = 4000,
) -> ContextBundle:
    """Build a ContextBundle for ``text`` scoped to ``repo``.

    ``role`` is currently unused — kept for API compatibility with
    callers (UnifiedContext) that already pass it; a future change may
    use it to weight sections per consumer role.
    """
    bundle = ContextBundle(repo=repo)

    # Fastpath
    hit = fastpath.detect(text)
    if hit:
        bundle.fastpath_hit = f"{hit.kind}:{hit.value}"

    # Translator (always run — fastpath is auxiliary)
    g = translator.translate(text, repo=repo, driver=driver)
    bundle.intent = g.intent
    bundle.errors.extend(g.errors)
    if g.errors:
        bundle.sources_used.append("translator(partial)")
    else:
        bundle.sources_used.append("translator")

    # Hydrate Service rows. Each hydration step below is individually
    # guarded — a Neo4j hiccup in one source degrades that source to
    # empty (recorded in bundle.errors) instead of failing the whole
    # bundle query.
    if g.services:
        try:
            bundle.services = _services_rows(
                driver, repo=repo, names=g.services)
            bundle.sources_used.append("services")
        except Exception as exc:  # noqa: BLE001
            bundle.errors.append(f"services: {exc}")

    # Hydrate File rows (with summary). Decorate each row with the
    # retrieval score the translator computed so the UI can show
    # "how confident" each anchor is.
    file_paths = list(g.files)
    if hit and hit.kind == "file":
        file_paths = [hit.value] + file_paths
    if file_paths:
        try:
            bundle.files = _files_rows(driver, repo=repo, paths=file_paths)
            for row in bundle.files:
                row["score"] = float(
                    getattr(g, "file_scores", {}).get(row.get("path"), 0.0)
                )
            bundle.sources_used.append("files")
        except Exception as exc:  # noqa: BLE001
            bundle.errors.append(f"files: {exc}")

    # Hydrate Symbol rows
    sym_fqnames = list(g.symbols)
    try:
        if hit and hit.kind == "symbol":
            # fastpath symbol guess — search by terminal name
            bundle.symbols = _symbols_by_terminal_name(
                driver, repo=repo, name=hit.value.rsplit(".", 1)[-1],
            )
        if sym_fqnames:
            bundle.symbols = _symbols_rows(
                driver, repo=repo, fqnames=sym_fqnames) + bundle.symbols
            bundle.sources_used.append("symbols")
    except Exception as exc:  # noqa: BLE001
        bundle.errors.append(f"symbols: {exc}")
    # Decorate symbol rows with retrieval score (0.0 when unranked).
    for row in bundle.symbols:
        row["score"] = float(
            getattr(g, "symbol_scores", {}).get(row.get("fqname"), 0.0)
        )

    # Call neighbours (1 hop) for top symbol
    if bundle.symbols:
        try:
            primary = bundle.symbols[0]["fqname"]
            bundle.callers, bundle.callees = _call_neighbours(
                driver, repo=repo, fqname=primary, hops=g.hops,
            )
            bundle.sources_used.append("calls")
        except Exception as exc:  # noqa: BLE001
            bundle.errors.append(f"calls: {exc}")

    # Repo runbook (always cheap to fetch)
    try:
        bundle.runbook_md, bundle.conventions_md = _repo_docs_for(
            driver, repo=repo)
    except Exception as exc:  # noqa: BLE001
        bundle.errors.append(f"repo_docs: {exc}")
    if bundle.runbook_md:
        bundle.sources_used.append("runbook")
    if bundle.conventions_md:
        bundle.sources_used.append("conventions")

    # Aider Repo Map
    if file_paths:
        bundle.repo_map = _repo_map_for(
            driver, repo=repo, focal_paths=file_paths, errors=bundle.errors,
        )
        if bundle.repo_map:
            bundle.sources_used.append("repo_map")

    # Memory layer — decisions/observations linked to anchor files/symbols
    # + Vector recall over observations
    anchor_paths = [f["path"] for f in bundle.files]
    anchor_syms = [s["fqname"] for s in bundle.symbols]

    # Semantic domains (repo-level orientation) + flows touching the
    # query's anchor symbols. Guarded — degrade to empty on any hiccup.
    try:
        bundle.domains = _domains_for(driver, repo=repo)
        if bundle.domains:
            bundle.sources_used.append("domains")
    except Exception as exc:  # noqa: BLE001
        bundle.errors.append(f"domains: {exc}")
    if anchor_syms:
        try:
            bundle.flows = _flows_for(driver, repo=repo, fqnames=anchor_syms)
            if bundle.flows:
                bundle.sources_used.append("flows")
        except Exception as exc:  # noqa: BLE001
            bundle.errors.append(f"flows: {exc}")
    # Raw code chunks for focal files (Top 5 chunks)
    if anchor_paths:
        bundle.chunks = _chunks_for(
            driver, repo=repo, paths=anchor_paths[:3], errors=bundle.errors,
        )
        if bundle.chunks:
            bundle.sources_used.append("chunks")

    if anchor_paths or anchor_syms:
        bundle.decisions = _decisions_for(
            driver, repo=repo, paths=anchor_paths, fqnames=anchor_syms,
            errors=bundle.errors,
        )
        bundle.observations = _observations_for(
            driver, repo=repo, paths=anchor_paths, fqnames=anchor_syms,
            errors=bundle.errors,
        )

        try:
            vec = translator._embed_query(text)
            if vec:
                vec_obs = _vector_observations(
                    driver, repo=repo, query_vec=vec, errors=bundle.errors,
                )
                # Deduplicate observations based on ID
                existing_obs_ids = {o.get("id") for o in bundle.observations if o.get("id")}
                for vo in vec_obs:
                    if vo.get("id") not in existing_obs_ids:
                        bundle.observations.append(vo)
        except Exception as e:
            bundle.errors.append(f"vector_observations: {e}")

        if bundle.decisions:
            bundle.sources_used.append("decisions")
        if bundle.observations:
            bundle.sources_used.append("observations")

    # Notes / Docs — only when anchors exist (MENTIONS edges to file/symbol)
    if anchor_paths or anchor_syms:
        bundle.notes = _notes_for(
            driver, repo=repo, paths=anchor_paths, fqnames=anchor_syms,
            errors=bundle.errors,
        )
        bundle.docs = _docs_for(
            driver, repo=repo, paths=anchor_paths, fqnames=anchor_syms,
            errors=bundle.errors,
        )
        if bundle.notes:
            bundle.sources_used.append("notes")
        if bundle.docs:
            bundle.sources_used.append("docs")

    # Cross-repo edges originating or terminating at this repo
    bundle.cross_repo = _cross_repo_for(driver, repo=repo,
                                        errors=bundle.errors)
    if bundle.cross_repo:
        bundle.sources_used.append("cross_repo")

    _trim_to_budget(bundle, token_budget)

    return bundle


def _trim_to_budget(bundle: ContextBundle, token_budget: int) -> None:
    """Drop low-priority sections until the rendered bundle fits the
    token budget (exact token counts)."""
    if _count_tokens(bundle.render()) <= token_budget:
        return
    # Priority 1: drop callers/callees
    bundle.callers = []
    bundle.callees = []
    if _count_tokens(bundle.render()) <= token_budget:
        return

    # Priority 2: trim chunks — raw code is re-readable from disk,
    # memory facts (decisions/observations/notes) are not.
    bundle.chunks = bundle.chunks[:2]
    if _count_tokens(bundle.render()) <= token_budget:
        return

    bundle.chunks = []
    if _count_tokens(bundle.render()) <= token_budget:
        return

    # Priority 3: drop cross-repo and decisions/observations
    bundle.cross_repo = []
    bundle.decisions = []
    bundle.observations = []
    bundle.notes = []
    bundle.docs = []
    if _count_tokens(bundle.render()) <= token_budget:
        return

    # Priority 4: trim symbols and files
    bundle.symbols = bundle.symbols[:6]
    bundle.files = bundle.files[:4]
    if _count_tokens(bundle.render()) <= token_budget:
        return

    # Priority 5: hard trim
    bundle.symbols = []
    bundle.files = []
    bundle.services = []
    bundle.runbook_md = bundle.runbook_md[:500]
    bundle.conventions_md = bundle.conventions_md[:500]


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
