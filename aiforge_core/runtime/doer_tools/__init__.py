"""Filesystem + shell tools the Doer LlmAgent calls during the v6 ADK
pipeline. Wired into ``runtime.adk_runner`` as
``google.adk.tools.FunctionTool``.

Modules:

* :mod:`sandbox`     — ``AIFORGE_REPO_ROOT`` resolver + traversal guard
* :mod:`syntax_guard`— pre-commit syntax sniff (see ``file_write``)
* :mod:`memory_lookup_tool` — hybrid AiForgeMemory recall

Each tool returns a JSON-serialisable dict so ADK can persist the
result in session state. Failures return ``{ok: False, error}`` instead
of raising — keeps the agent loop alive while still surfacing the
problem to the model.

This module was split (grouped by concern) into ``_fs`` / ``_web`` /
``_repo`` / ``_integrations`` / ``_tools`` / ``_wiring`` submodules; this
package re-exports the full former public+private surface so
``from aiforge_core.runtime import doer_tools`` and every
``doer_tools.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

# The module-level stdlib imports the original ``doer_tools.py`` exposed —
# kept so ``doer_tools.subprocess`` / ``doer_tools.urllib`` (etc.) remain
# patchable attributes exactly as before the split (several tests monkeypatch
# ``aiforge_core.runtime.doer_tools.subprocess.run`` and
# ``doer_tools.urllib.request``).
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

# Passthrough names the original module re-exported at top level.
from ..graphify_lookup_tool import graphify_lookup
from ..memory_block_tool import memory_block
from ..memory_lookup_tool import memory_lookup
from ..sandbox import resolve_inside_root, root
from ..syntax_guard import validate_syntax
from ..git_pr import _EXCLUDE_PATHSPECS

from ._fs import (
    _GREP_DEFAULT_EXCLUDES,
    _READ_CACHE,
    _READ_CACHE_MAX,
    _TOUCHED,
    _TOUCHED_LOCK,
    _compact_digest,
    file_patch,
    file_read,
    file_write,
    grep_repo,
    list_dir,
    read_lines,
    record_touch,
    reset_touched,
    run_shell,
    touched_paths,
)
from ._web import (
    _FETCH_MAX_BYTES,
    _FETCH_TIMEOUT_S,
    _do_fetch,
    _web_fetch_allowed,
    fetch_url,
    web_crawl,
    web_read,
    web_search,
)
from ._repo import (
    _digest_file_paths,
    _git,
    codegraph_callees,
    codegraph_callers,
    codegraph_explore,
    codegraph_impact,
    codegraph_query,
    git_blame,
    git_commit,
    git_diff,
    git_log,
    git_status,
    impacted_tests,
    rename_symbol,
    repo_map,
)
from ._integrations import (
    confluence_add_label,
    confluence_attach,
    confluence_children,
    confluence_comment,
    confluence_comments,
    confluence_create,
    confluence_descendants,
    confluence_labels,
    confluence_page_by_title,
    confluence_read,
    confluence_resolve_space,
    confluence_search,
    confluence_spaces,
    confluence_update,
    context_gather,
    email_read,
    email_send,
    gitlab_comment,
    gitlab_create,
    gitlab_mr_comment,
    gitlab_mr_create,
    gitlab_pipeline,
    gitlab_pipeline_watch,
    gitlab_pipelines,
    gitlab_read,
    gitlab_search,
    gitlab_update,
    jira_assign,
    jira_boards,
    jira_comment,
    jira_create,
    jira_dashboard_create,
    jira_dashboard_read,
    jira_dashboards,
    jira_link_issues,
    jira_log_work,
    jira_myself,
    jira_projects,
    jira_read,
    jira_remote_links,
    jira_resolve_project,
    jira_search,
    jira_sprint_issues,
    jira_sprints,
    jira_transition,
    jira_transitions,
    jira_update,
    jira_worklog,
    resolve_repo,
)
from ._tools import (
    bash,
    browse,
    commit,
    delegate_to_agent,
    edit,
    execute_ipython_cell,
    format,
    git_add_commit,
    github_pr,
    glob,
    grep,
    http_get,
    learn_skill,
    learn_workflow,
    ls,
    lsp,
    mcp,
    multi_edit,
    patch,
    read,
    run,
    run_tests,
    search,
    serve,
    shell,
    skill_search,
    stop_service,
    str_replace,
    subtask_update,
    task,
    todo_write,
    todowrite,
    typecheck,
    web_fetch,
    workflow_search,
    write,
)
from ._wiring import _adk_function_tools_impl, adk_function_tools

# Preserve the pre-split ``__file__`` so consumers that derive paths from it
# still resolve to ``aiforge_core/`` (e.g. test_agents_yaml_reconciled computes
# ``Path(doer_tools.__file__).resolve().parents[1] / "agents"``). As a package,
# the real ``__file__`` is one directory deeper (``runtime/doer_tools/__init__``),
# which would shift ``parents[1]`` — point it back at the old module location.
from pathlib import Path as _Path

__file__ = str(_Path(__file__).resolve().parent.parent / "doer_tools.py")

__all__ = [
    "record_touch", "touched_paths", "reset_touched",
    "file_read", "file_write", "file_patch", "list_dir", "run_shell",
    "grep_repo", "repo_map", "impacted_tests", "fetch_url", "git_commit",
    "codegraph_impact", "codegraph_callers", "codegraph_callees",
    "codegraph_explore", "codegraph_query",
    "memory_lookup", "memory_block", "graphify_lookup", "skill_search", "learn_skill",
    "workflow_search", "learn_workflow", "web_search", "web_crawl",
    "serve", "stop_service",
    "subtask_update",
    "confluence_search", "confluence_read", "confluence_create", "confluence_update",
    "jira_search", "jira_read", "jira_create", "jira_update", "jira_comment",
    "email_send", "email_read",
    "gitlab_search", "gitlab_read", "gitlab_create", "gitlab_update",
    "gitlab_comment", "gitlab_mr_create", "gitlab_mr_comment",
    "gitlab_pipelines", "gitlab_pipeline", "gitlab_pipeline_watch",
    "typecheck", "run_tests", "lsp", "format",
    "mcp", "browse", "execute_ipython_cell", "delegate_to_agent",
    "github_pr", "multi_edit",
    "git_status", "git_diff", "git_log", "git_blame",
    "jira_transitions", "jira_transition", "jira_assign", "jira_link_issues",
    "jira_worklog", "jira_remote_links", "resolve_repo",
    "confluence_children", "confluence_attach", "read_lines", "rename_symbol",
    "read", "write", "patch", "edit", "str_replace", "ls", "shell", "bash",
    "grep", "search", "http_get", "web_fetch", "web_read",
    "commit", "git_add_commit",
    "todo_write", "todowrite", "glob", "task",
    "adk_function_tools",
]
