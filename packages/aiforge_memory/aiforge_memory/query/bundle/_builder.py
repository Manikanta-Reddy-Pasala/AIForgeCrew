from __future__ import annotations

from aiforge_memory.query import fastpath, translator

from ._helpers import _count_tokens
from ._model import ContextBundle
from ._hydrators import (
    _call_neighbours,
    _chunks_for,
    _cross_repo_for,
    _decisions_for,
    _docs_for,
    _domains_for,
    _files_rows,
    _flows_for,
    _notes_for,
    _observations_for,
    _repo_docs_for,
    _repo_map_for,
    _services_rows,
    _symbols_by_terminal_name,
    _symbols_rows,
    _vector_observations,
)


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
