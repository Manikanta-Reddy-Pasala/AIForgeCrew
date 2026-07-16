"""Auto-commit + push + open-PR helper for the v6 ticket runner.

Lives separate from ``adk_runner`` so the orchestrator stays focused on
ticket lifecycle. Two public entry points:

* :func:`run_git`              — thin subprocess wrapper (5-min cap).
* :func:`commit_push_open_pr`  — full happy/sad path for the runner.

The PR step short-circuits cleanly when:

* the workspace isn't a git repo
* the working tree has no Doer-authored changes (transient cache dirs
  excluded — see ``_EXCLUDE_PATHSPECS``)
* ``gh`` CLI isn't installed (push still happens, PR step is skipped
  with a logged hint)

Returns a metadata patch dict the runner merges into ticket metadata
so the human triage loop sees ``pr_url``, ``branch_pushed``, and
``pr_skip_reason`` together.

This module was split (grouped by concern) into ``_excludes`` /
``_gitcmd`` / ``_pr`` submodules; this package re-exports the full former
top-level surface so ``from aiforge_core.runtime import git_pr`` and every
``git_pr.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

from ._excludes import (
    _ARTIFACT_IGNORE_LINES,
    _EXCLUDE_BASENAMES,
    _EXCLUDE_DIR_SEGMENTS,
    _EXCLUDE_PATHSPECS,
    _EXCLUDE_SUFFIXES,
    _EXCLUDE_TOPLEVEL,
    _TEST_ALWAYS_PATHS,
    _TEST_PATH_FRAGMENTS,
    _TEST_SUFFIXES,
    _is_test_path,
    ensure_artifact_gitignore,
    is_excluded_path,
    log,
)
from ._gitcmd import (
    _classify_head_diff,
    _default_base_branch,
    _has_unpushed_commits,
    _resolve_repo_root,
    run_git,
)
from ._pr import (
    _DEFAULT_GITIGNORE,
    _checkout_branch,
    _commit_changes,
    _ensure_gitignore,
    _fire_delta_ingest,
    _has_doer_changes,
    _has_reachable_remote,
    _open_pr,
    _push,
    _stage_doer_changes,
    commit_push_open_pr,
    merge_pr,
)

__all__ = ["run_git", "commit_push_open_pr", "merge_pr",
           "is_excluded_path", "ensure_artifact_gitignore",
           "_EXCLUDE_PATHSPECS"]
