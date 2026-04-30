"""Cypher writer for the Repo node.

Single function: ``upsert_repo(driver, name, path, summary, pack_sha)``.
Idempotent: Cypher MERGE keyed on ``Repo.name``.
"""
from __future__ import annotations

import time
from dataclasses import asdict

from aiforge_core.codemem.ingest.repo_summary import RepoSummary


_CYPHER = """
MERGE (r:Repo {name: $name})
SET r.path             = $path,
    r.lang_primary     = $lang_primary,
    r.build_cmd        = $build_cmd,
    r.test_cmd         = $test_cmd,
    r.lint_cmd         = $lint_cmd,
    r.run_cmd          = $run_cmd,
    r.portforward_cmds = $portforward_cmds,
    r.conventions_md   = $conventions_md,
    r.runbook_md       = $runbook_md,
    r.last_pack_sha    = $pack_sha,
    r.last_indexed_at  = datetime({epochSeconds: toInteger($now)}),
    r.schema_version   = 'codemem-v1'
RETURN r
"""


def upsert_repo(
    driver,
    *,
    name: str,
    path: str,
    summary: RepoSummary,
    pack_sha: str,
) -> None:
    params = {
        "name": name,
        "path": path,
        "now": time.time(),
        "pack_sha": pack_sha,
        **asdict(summary),
    }
    with driver.session() as s:
        s.run(_CYPHER, **params).consume()
