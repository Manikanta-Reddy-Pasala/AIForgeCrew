"""Agent tool-wrapper functions for the deploy-anywhere chat agent.

This module was split (grouped by concern) into private submodules —
``_shared`` (cross-cutting helpers), ``_memory`` (memory/find/grep/rules),
``_jira`` / ``_confluence`` / ``_gitlab`` / ``_git`` / ``_web`` (integrations),
``_code`` (codegraph/read/rename), ``_skills`` (skill/workflow + strong
editor tools), ``_pipeline`` (mcp/browser/ipython/delegate) and ``_misc``.
This package re-exports the full former top-level surface so
``from ._tools import <name>`` (used by ``__init__``, ``_registry``, ``_loop``,
``_context``) keeps working IDENTICALLY.
"""

from __future__ import annotations

from ._shared import (
    _GIT_TOPLEVEL_CACHE,
    _git_toplevel,
    _chat_repo_key,
    _ELABORATE_PROMPT,
    _elaborate_body,
    _ROOT_SCOPED_TOOLS,
    _scoped_root,
    _coerce_int,
    _git_cli,
    _chat_run_id,
)
from ._memory import (
    _t_memory_lookup,
    _t_search_chat_sessions,
    _t_memory_write,
    _SKIP_DIRS,
    _t_find,
    _t_grep,
    _t_remember_rule,
    _BULLET_TRIGGERS_RE,
    _parse_bullet,
    _source_path_cache,
    _cached_find_by_source,
    _preferences_context,
    _rules_context,
)
from ._jira import (
    _t_jira_search,
    _t_jira_read,
    _t_jira_worklog,
    _t_jira_remote_links,
    _t_jira_resolve_project,
    _t_jira_log_work,
    _t_jira_myself,
    _t_jira_projects,
    _t_jira_boards,
    _t_jira_sprints,
    _t_jira_sprint_issues,
    _t_jira_dashboards,
    _t_jira_dashboard_read,
    _t_jira_dashboard_create,
    _t_jira_create,
    _t_jira_update,
    _t_jira_comment,
    _t_jira_transitions,
    _t_jira_transition,
    _t_jira_assign,
    _t_jira_link_issues,
)
from ._confluence import (
    _t_confluence_search,
    _t_confluence_read,
    _t_confluence_create,
    _t_confluence_update,
    _t_confluence_attach,
    _t_confluence_resolve_space,
    _t_confluence_children,
    _t_confluence_spaces,
    _t_confluence_page_by_title,
    _t_confluence_labels,
    _t_confluence_add_label,
    _t_confluence_comments,
    _t_confluence_comment,
    _t_confluence_descendants,
)
from ._gitlab import (
    _t_gitlab_search,
    _t_gitlab_read,
    _t_gitlab_create,
    _t_gitlab_update,
    _t_gitlab_comment,
    _t_gitlab_mr_create,
    _t_gitlab_mr_comment,
    _t_github_pr,
)
from ._git import (
    _t_git_status,
    _t_git_diff,
    _t_git_log,
    _t_git_blame,
)
from ._code import (
    _t_codegraph_query,
    _t_codegraph_callers,
    _t_codegraph_callees,
    _t_codegraph_impact,
    _t_codegraph_explore,
    _t_read_lines,
    _t_rename_symbol,
    _t_summarize_doc,
)
from ._skills import (
    _t_skill_search,
    _t_learn_skill,
    _t_workflow_search,
    _t_learn_workflow,
    _t_editor,
    _t_multi_edit,
    _t_typecheck,
    _t_format,
    _t_lsp,
    _t_run_tests,
)
from ._web import (
    _t_web_search,
    _t_web_fetch,
    _t_web_crawl,
)
from ._pipeline import (
    _t_mcp,
    _t_browse,
    _t_ipython,
    _t_delegate,
)
from ._misc import (
    _t_create_job_script,
    _t_ensure_runtime,
    _t_project,
    _t_set_repo_folder,
    _t_set_repo_root,
    _t_list_repos,
    _t_set_integration_default,
    _t_resolve_repo,
    _t_context_gather,
    _t_note_curate,
    _t_note_consolidate,
    _t_email_send,
    _t_email_read,
    _t_serve,
    _t_stop_service,
    _t_list_services,
)
