"""Cypher writer for the Repo node.

Single function: ``upsert_repo(driver, name, path, summary, pack_sha,
                               git_meta=None)``.
Idempotent: Cypher MERGE keyed on ``Repo.name``.

Git metadata is optional — when supplied, the Repo node also carries
``head_sha``, ``branch``, ``default_branch``, ``remote_url``, ``dirty``.
Pass ``GitMeta()`` (default) to leave these empty.
"""
from __future__ import annotations

import time
from dataclasses import asdict

from aiforge_memory.features.git_meta.extract import GitMeta
from aiforge_memory.features.repo.extract import RepoSummary

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
    r.head_sha         = $head_sha,
    r.branch           = $branch,
    r.default_branch   = $default_branch,
    r.remote_url       = $remote_url,
    r.dirty            = $dirty,
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
    git_meta: GitMeta | None = None,
) -> None:
    g = git_meta or GitMeta()
    params = {
        "name": name,
        "path": path,
        "now": time.time(),
        "pack_sha": pack_sha,
        "head_sha": g.head_sha,
        "branch": g.branch,
        "default_branch": g.default_branch,
        "remote_url": g.remote_url,
        "dirty": g.dirty,
        **asdict(summary),
    }
    with driver.session() as s:
        s.run(_CYPHER, **params).consume()


_UPDATE_GIT_META = """
MATCH (r:Repo {name: $name})
SET r.head_sha       = $head_sha,
    r.branch         = $branch,
    r.default_branch = $default_branch,
    r.remote_url     = $remote_url,
    r.dirty          = $dirty,
    r.last_indexed_at = datetime({epochSeconds: toInteger($now)})
"""


def update_git_meta(driver, *, name: str, git_meta: GitMeta) -> None:
    """Refresh only git fields on the Repo node — used by delta ingest /
    scheduler after a fetch/pull updates HEAD."""
    with driver.session() as s:
        s.run(
            _UPDATE_GIT_META,
            name=name,
            head_sha=git_meta.head_sha,
            branch=git_meta.branch,
            default_branch=git_meta.default_branch,
            remote_url=git_meta.remote_url,
            dirty=git_meta.dirty,
            now=time.time(),
        ).consume()
