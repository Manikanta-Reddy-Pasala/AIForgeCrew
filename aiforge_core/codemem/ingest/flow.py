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

from dataclasses import dataclass
from pathlib import Path

from aiforge_core.codemem.ingest import (
    edges, embed, file_summary, pack_repo, repo_summary, service_extract,
    treesitter_walk,
)
from aiforge_core.codemem.store import (
    chunk_writer, file_summary_writer, repo_writer, service_writer,
    state_db as sdb, symbol_writer,
)


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
) -> IngestResult:
    text, sha = pack_repo.pack(repo_path)
    prev = sdb.get_repo_pack_sha(state_conn, repo=repo_name)
    if prev == sha and not force:
        return IngestResult(status="skipped_unchanged", pack_sha=sha, repo=repo_name)

    # Stage 2 — repo summary + Repo node
    summary = repo_summary.summarize(text, repo_name=repo_name)
    repo_writer.upsert_repo(
        driver,
        name=repo_name,
        path=str(Path(repo_path).resolve()),
        summary=summary,
        pack_sha=sha,
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
        ccounts = symbol_writer.upsert_call_edges(
            driver, repo=repo_name, edges=call_edges,
            file_paths=[wf.path for wf in walked],
        )
        calls_count = ccounts["calls"]

    # Stage 6 — file summaries
    summaries_updated = summaries_skipped = 0
    if not skip_summaries and walked:
        summaries = file_summary.summarize_files(
            walked, repo=repo_name, repo_root=repo_path,
        )
        sumcounts = file_summary_writer.write_summaries(
            driver, repo=repo_name, summaries=summaries,
        )
        summaries_updated = sumcounts["updated"]
        summaries_skipped = sumcounts["skipped"]

    # Stage 7 — chunk embeddings
    chunks_count = 0
    if not skip_chunks and walked:
        chunks = embed.chunk_and_embed(
            walked, repo=repo_name, repo_root=repo_path,
        )
        if chunks:
            ccounts = chunk_writer.upsert_chunks(
                driver, repo=repo_name, chunks=chunks,
            )
            chunks_count = ccounts["chunks"]

    sdb.set_repo_pack_sha(state_conn, repo=repo_name, pack_sha=sha)
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
