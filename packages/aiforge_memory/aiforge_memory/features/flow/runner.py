"""codemem ingestion orchestrator.

Exposed surface:
    flow.ingest_repo(repo_name, repo_path, *, driver, state_conn,
                     force=False, skip_services=False, skip_symbols=False)
        -> IngestResult

Stages run in order:
    Stage 1+2  pack_repo  → repo_summary  → repo_writer.upsert_repo
    Stage 3    service_extract  → service_writer.upsert_services
    Stage 4+5  treesitter_walk  → File_v2 + Symbol_v2 + IMPORTS;
                                  edges.resolve_calls_with_source → CALLS

Idempotency: pack_sha matched against state_db.merkle_repo. When equal
and ``force=False`` we skip every stage. ``force=True`` reruns
everything (used by `aiforge codemem reset`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aiforge_memory.core import state as sdb
from aiforge_memory.features.chunk import embed as embed
from aiforge_memory.features.chunk import store as chunk_writer
from aiforge_memory.features.file import extract as file_summary
from aiforge_memory.features.file import store as file_summary_writer
from aiforge_memory.features.git_meta import extract as git_meta
from aiforge_memory.features.repo import extract as repo_summary
from aiforge_memory.features.repo import pack_repo as pack_repo
from aiforge_memory.features.repo import store as repo_writer
from aiforge_memory.features.service import extract as service_extract
from aiforge_memory.features.service import store as service_writer
from aiforge_memory.features.symbol import extract as treesitter_walk
from aiforge_memory.features.symbol import extract_calls as edges
from aiforge_memory.features.symbol import store as symbol_writer

log = logging.getLogger("aiforge_memory.flow")


@dataclass
class IngestResult:
    status: str           # "indexed" | "skipped_unchanged"
    pack_sha: str
    repo: str
    services_count: int = 0
    file_edges_count: int = 0
    files_count: int = 0
    symbols_count: int = 0
    imports_count: int = 0
    calls_count: int = 0
    summaries_updated: int = 0
    summaries_skipped: int = 0
    chunks_count: int = 0


def ingest_repo(
    *,
    repo_name: str,
    repo_path: str | Path,
    driver,
    state_conn,
    force: bool = False,
    skip_services: bool = False,
    skip_symbols: bool = False,
    skip_summaries: bool = False,
    skip_chunks: bool = False,
    use_lsp: bool = False,
) -> IngestResult:
    text, sha = pack_repo.pack(repo_path)
    prev = sdb.get_repo_pack_sha(state_conn, repo=repo_name)
    if prev == sha and not force:
        return IngestResult(status="skipped_unchanged", pack_sha=sha, repo=repo_name)

    # Stage 2 — repo summary + Repo node (with git metadata)
    summary = repo_summary.summarize(text, repo_name=repo_name)
    gmeta = git_meta.read(repo_path)
    repo_writer.upsert_repo(
        driver,
        name=repo_name,
        path=str(Path(repo_path).resolve()),
        summary=summary,
        pack_sha=sha,
        git_meta=gmeta,
    )

    # Stage 3 — services
    services_count = 0
    file_edges_count = 0
    if not skip_services:
        drafts = service_extract.extract_services(
            text, repo_path=repo_path, repo_name=repo_name,
        )
        counts = service_writer.upsert_services(
            driver, repo=repo_name, services=drafts,
        )
        services_count = counts["services"]
        file_edges_count = counts["file_edges"]

    # Stage 4+5 — symbols + edges
    files_count = symbols_count = imports_count = calls_count = 0
    walked: list = []
    if not skip_symbols:
        walked = treesitter_walk.walk_repo(repo_path, repo=repo_name)
        scounts = symbol_writer.upsert_files_and_symbols(
            driver, repo=repo_name, walked_files=walked,
        )
        files_count = scounts["files"]
        symbols_count = scounts["symbols"]
        imports_count = scounts["imports"]

        call_edges = edges.resolve_calls_with_source(
            walked, repo=repo_name, repo_root=repo_path,
        )
        # Optional LSP pass — adds high-confidence (1.0) edges that the
        # tree-sitter heuristic missed. Merged via _merge_calls so the
        # higher confidence wins on duplicate (caller, callee) pairs.
        if use_lsp:
            try:
                from aiforge_memory.features.lsp import resolve_calls as lsp_resolve
                lsp_edges = lsp_resolve(
                    walked, repo=repo_name, repo_root=repo_path,
                )
                call_edges = _merge_calls(call_edges, lsp_edges)
            except Exception:  # noqa: BLE001 — LSP is best-effort
                pass
        ccounts = symbol_writer.upsert_call_edges(
            driver, repo=repo_name, edges=call_edges,
            file_paths=[wf.path for wf in walked],
        )
        calls_count = ccounts["calls"]

    # Stage 6 — file summaries. Only re-summarize files whose content
    # changed since the last ingest (state-db merkle hash) or that have
    # no summary in the graph yet — a full ingest on an already-indexed
    # repo otherwise re-runs the LLM over every file.
    summaries_updated = summaries_skipped = 0
    if not skip_summaries and walked:
        to_summarize = _files_needing_summary(
            walked, driver=driver, state_conn=state_conn, repo=repo_name,
        )
        if to_summarize:
            summaries = file_summary.summarize_files(
                to_summarize, repo=repo_name, repo_root=repo_path,
            )
            sumcounts = file_summary_writer.write_summaries(
                driver, repo=repo_name, summaries=summaries,
            )
            summaries_updated = sumcounts["updated"]
            summaries_skipped = sumcounts["skipped"]

    # Stage 7 — chunk embeddings
    chunks_count = 0
    embed_failed: list[str] = []
    if not skip_chunks and walked:
        chunks, embed_failed = embed.chunk_and_embed(
            walked, repo=repo_name, repo_root=repo_path,
        )
        if chunks:
            ccounts = chunk_writer.upsert_chunks(
                driver, repo=repo_name, chunks=chunks,
            )
            chunks_count = ccounts["chunks"]

    # Persist per-file hashes + git head so subsequent --delta runs have
    # state to diff against. Without this, delta hits cold_start every time.
    # Files whose embed failed keep no/stale hash so the next delta
    # retries them.
    surviving_hashes: dict = {}
    if walked:
        failed = set(embed_failed)
        surviving_hashes = {wf.path: wf.hash for wf in walked
                            if wf.path not in failed}
        sdb.upsert_file_hashes(
            state_conn, repo=repo_name, hashes=surviving_hashes,
        )
    # Cold-loop guard: on a FIRST ingest with the embed sidecar fully
    # down, every walked file fails → zero hashes written. Recording
    # pack_sha then traps the repo: next delta sees no hashes → cold
    # start → full ingest sees pack unchanged → skipped_unchanged,
    # forever (until repo content changes). Skip the pack_sha write in
    # that state so the next sweep retries the full ingest.
    if walked and not surviving_hashes and embed_failed:
        log.warning(
            "ingest %s: ALL %d files failed embedding — not recording "
            "pack_sha so the next run retries", repo_name,
            len(embed_failed),
        )
    else:
        sdb.set_repo_pack_sha(state_conn, repo=repo_name, pack_sha=sha)
    try:
        gmeta = git_meta.read(repo_path)
        if gmeta.head_sha:
            sdb.set_repo_git_head(
                state_conn, repo=repo_name,
                head_sha=gmeta.head_sha, branch=gmeta.branch,
            )
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return IngestResult(
        status="indexed", pack_sha=sha, repo=repo_name,
        services_count=services_count,
        file_edges_count=file_edges_count,
        files_count=files_count,
        symbols_count=symbols_count,
        imports_count=imports_count,
        calls_count=calls_count,
        summaries_updated=summaries_updated,
        summaries_skipped=summaries_skipped,
        chunks_count=chunks_count,
    )


def _files_needing_summary(walked, *, driver, state_conn, repo):
    """Subset of ``walked`` whose hash changed since the previous ingest
    or whose File_v2 node has no (non-empty) summary yet. Best-effort:
    any lookup failure falls back to summarizing everything (the prior
    behaviour)."""
    try:
        prev_hashes = sdb.get_file_hashes(state_conn, repo=repo)
        if not prev_hashes:
            return walked
        cy = (
            "MATCH (f:File_v2 {repo:$repo}) "
            "WHERE f.summary IS NOT NULL AND f.summary <> '' "
            "RETURN f.path AS path"
        )
        with driver.session() as s:
            summarized = {r["path"] for r in s.run(cy, repo=repo)}
        return [
            wf for wf in walked
            if prev_hashes.get(wf.path) != wf.hash
            or wf.path not in summarized
        ]
    except Exception:  # noqa: BLE001 — skip-optimisation is best-effort
        return walked


def _merge_calls(primary, secondary):
    """Merge two CallEdge lists keyed on (caller, callee). Higher
    confidence wins. Used to layer LSP-confirmed edges on top of the
    tree-sitter heuristic without duplicating rows."""
    by_key: dict[tuple[str, str], object] = {}
    for e in list(primary) + list(secondary):
        key = (e.caller_fqname, e.callee_fqname)
        existing = by_key.get(key)
        if existing is None or e.confidence > existing.confidence:
            by_key[key] = e
    return list(by_key.values())
