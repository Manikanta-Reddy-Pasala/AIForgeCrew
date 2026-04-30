"""codemem ingestion orchestrator (Stages 1+2 in plan 1).

Exposed surface:
    flow.ingest_repo(repo_name, repo_path, *, driver, state_conn, force=False)
        -> IngestResult

Idempotency: pack_sha matched against state_db.merkle_repo. When equal
and ``force=False`` we skip Stages 2+. ``force=True`` reruns everything
(used by `aiforge codemem reset`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiforge_core.codemem.ingest import pack_repo, repo_summary
from aiforge_core.codemem.store import repo_writer, state_db as sdb


@dataclass
class IngestResult:
    status: str           # "indexed" | "skipped_unchanged"
    pack_sha: str
    repo: str


def ingest_repo(
    *,
    repo_name: str,
    repo_path: str | Path,
    driver,
    state_conn,
    force: bool = False,
) -> IngestResult:
    text, sha = pack_repo.pack(repo_path)
    prev = sdb.get_repo_pack_sha(state_conn, repo=repo_name)
    if prev == sha and not force:
        return IngestResult(status="skipped_unchanged", pack_sha=sha, repo=repo_name)

    summary = repo_summary.summarize(text, repo_name=repo_name)
    repo_writer.upsert_repo(
        driver,
        name=repo_name,
        path=str(Path(repo_path).resolve()),
        summary=summary,
        pack_sha=sha,
    )
    sdb.set_repo_pack_sha(state_conn, repo=repo_name, pack_sha=sha)
    return IngestResult(status="indexed", pack_sha=sha, repo=repo_name)
