"""codemem ingestion orchestrator.

Exposed surface:
    flow.ingest_repo(repo_name, repo_path, *, driver, state_conn,
                     force=False, skip_services=False) -> IngestResult

Stages run in order:
    Stage 1+2  pack_repo  → repo_summary  → repo_writer.upsert_repo
    Stage 3    service_extract  → service_writer.upsert_services

Idempotency: pack_sha matched against state_db.merkle_repo. When equal
and ``force=False`` we skip every stage. ``force=True`` reruns
everything (used by `aiforge codemem reset`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiforge_core.codemem.ingest import pack_repo, repo_summary, service_extract
from aiforge_core.codemem.store import repo_writer, service_writer, state_db as sdb


@dataclass
class IngestResult:
    status: str           # "indexed" | "skipped_unchanged"
    pack_sha: str
    repo: str
    services_count: int = 0
    file_edges_count: int = 0


def ingest_repo(
    *,
    repo_name: str,
    repo_path: str | Path,
    driver,
    state_conn,
    force: bool = False,
    skip_services: bool = False,
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

    sdb.set_repo_pack_sha(state_conn, repo=repo_name, pack_sha=sha)
    return IngestResult(
        status="indexed", pack_sha=sha, repo=repo_name,
        services_count=services_count,
        file_edges_count=file_edges_count,
    )
