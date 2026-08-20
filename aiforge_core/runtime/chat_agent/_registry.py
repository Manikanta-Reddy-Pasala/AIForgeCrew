from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from ._shell import (_t_file_patch, _t_file_read, _t_file_write, _t_list_dir, _t_read_files, _t_run_command)
from ._tools._watch import _t_schedule_task, _t_watch_until
from ._tools import (_t_browse, _t_codegraph_callees, _t_codegraph_callers, _t_codegraph_explore, _t_codegraph_impact, _t_codegraph_query, _t_confluence_add_label, _t_confluence_attach, _t_confluence_children, _t_confluence_comment, _t_confluence_comments, _t_confluence_create, _t_confluence_descendants, _t_confluence_labels, _t_confluence_page_by_title, _t_confluence_read, _t_confluence_resolve_space, _t_confluence_search, _t_confluence_spaces, _t_confluence_update, _t_context_gather, _t_create_job_script, _t_delegate, _t_editor, _t_email_read, _t_email_send, _t_ensure_runtime, _t_find, _t_format, _t_git_blame, _t_git_diff, _t_git_log, _t_git_status, _t_github_pr, _t_gitlab_comment, _t_gitlab_create, _t_gitlab_mr_comment, _t_gitlab_mr_create, _t_gitlab_read, _t_gitlab_search, _t_gitlab_update, _t_grep, _t_ipython, _t_jira_assign, _t_jira_boards, _t_jira_comment, _t_jira_comments, _t_jira_create, _t_jira_dashboard_create, _t_jira_dashboard_read, _t_jira_dashboards, _t_jira_link_issues, _t_jira_log_work, _t_jira_myself, _t_jira_projects, _t_jira_read, _t_jira_remote_links, _t_jira_resolve_project, _t_jira_search, _t_jira_sprint_issues, _t_jira_sprints, _t_jira_transition, _t_jira_transitions, _t_jira_update, _t_jira_worklog, _t_learn_skill, _t_learn_workflow, _t_list_repos, _t_list_services, _t_lsp, _t_mcp, _t_memory_lookup, _t_memory_write, _t_multi_edit, _t_note_consolidate, _t_note_curate, _t_project, _t_read_lines, _t_remember_rule, _t_rename_symbol, _t_resolve_repo, _t_run_tests, _t_summarize_doc, _t_search_chat_sessions, _t_serve, _t_set_integration_default, _t_set_repo_folder, _t_set_repo_root, _t_skill_search, _t_stop_service, _t_typecheck, _t_web_crawl, _t_web_fetch, _t_web_search, _t_workflow_search)

TOOLS: dict[str, Callable[[dict, str], dict]] = {
    "file_read": _t_file_read,
    "read_files": _t_read_files,
    "file_write": _t_file_write,
    "file_create": _t_file_write,   # alias
    "file_patch": _t_file_patch,
    "list_dir": _t_list_dir,
    "find": _t_find,
    "grep": _t_grep,
    "run_command": _t_run_command,
    "watch_until": _t_watch_until,
    "schedule_task": _t_schedule_task,
    "ensure_runtime": _t_ensure_runtime,
    "project": _t_project,
    "remember_rule": _t_remember_rule,
    "memory_lookup": _t_memory_lookup,
    "memory_write": _t_memory_write,
    "search_chat_sessions": _t_search_chat_sessions,
    "skill_search": _t_skill_search,
    "learn_skill": _t_learn_skill,
    "confluence_search": _t_confluence_search,
    "confluence_read": _t_confluence_read,
    "confluence_create": _t_confluence_create,
    "confluence_update": _t_confluence_update,
    "confluence_attach": _t_confluence_attach,
    "jira_search": _t_jira_search,
    "jira_read": _t_jira_read,
    "jira_worklog": _t_jira_worklog,
    "jira_log_work": _t_jira_log_work,
    "jira_remote_links": _t_jira_remote_links,
    "context_gather": _t_context_gather,
    "note_curate": _t_note_curate,
    "note_consolidate": _t_note_consolidate,
    "codegraph_query": _t_codegraph_query,
    "codegraph_callers": _t_codegraph_callers,
    "codegraph_callees": _t_codegraph_callees,
    "codegraph_impact": _t_codegraph_impact,
    "codegraph_explore": _t_codegraph_explore,
    "resolve_repo": _t_resolve_repo,
    "jira_resolve_project": _t_jira_resolve_project,
    "confluence_resolve_space": _t_confluence_resolve_space,
    "jira_myself": _t_jira_myself,
    "jira_projects": _t_jira_projects,
    "jira_boards": _t_jira_boards,
    "jira_sprints": _t_jira_sprints,
    "jira_sprint_issues": _t_jira_sprint_issues,
    "jira_dashboards": _t_jira_dashboards,
    "jira_dashboard_read": _t_jira_dashboard_read,
    "jira_dashboard_create": _t_jira_dashboard_create,
    "jira_create": _t_jira_create,
    "jira_update": _t_jira_update,
    "jira_comments": _t_jira_comments,
    "jira_comment": _t_jira_comment,
    "jira_transitions": _t_jira_transitions,
    "jira_transition": _t_jira_transition,
    "jira_assign": _t_jira_assign,
    "jira_link_issues": _t_jira_link_issues,
    "confluence_children": _t_confluence_children,
    "confluence_attach": _t_confluence_attach,
    "confluence_spaces": _t_confluence_spaces,
    "confluence_page_by_title": _t_confluence_page_by_title,
    "confluence_labels": _t_confluence_labels,
    "confluence_add_label": _t_confluence_add_label,
    "confluence_comments": _t_confluence_comments,
    "confluence_comment": _t_confluence_comment,
    "confluence_descendants": _t_confluence_descendants,
    "set_integration_default": _t_set_integration_default,
    "set_repo_folder": _t_set_repo_folder,
    "set_repo_root": _t_set_repo_root,
    "list_repos": _t_list_repos,
    "git_status": _t_git_status,
    "git_diff": _t_git_diff,
    "git_log": _t_git_log,
    "git_blame": _t_git_blame,
    "read_lines": _t_read_lines,
    "summarize_doc": _t_summarize_doc,
    "rename_symbol": _t_rename_symbol,
    "email_send": _t_email_send,
    "email_read": _t_email_read,
    "gitlab_search": _t_gitlab_search,
    "gitlab_read": _t_gitlab_read,
    "gitlab_mr_create": _t_gitlab_mr_create,
    "gitlab_mr_comment": _t_gitlab_mr_comment,
    "github_pr": _t_github_pr,
    "gitlab_create": _t_gitlab_create,
    "gitlab_update": _t_gitlab_update,
    "gitlab_comment": _t_gitlab_comment,
    "web_search": _t_web_search,
    "web_fetch": _t_web_fetch,
    "web_crawl": _t_web_crawl,
    "workflow_search": _t_workflow_search,
    "learn_workflow": _t_learn_workflow,
    "create_job_script": _t_create_job_script,
    "serve": _t_serve,
    "stop_service": _t_stop_service,
    "list_services": _t_list_services,
    # Shared "strong" tools (now available to the chat agent, not just the team
    # pipeline): structured editor (undo + syntax-check), symbols, types, tests.
    "editor": _t_editor,
    "multi_edit": _t_multi_edit,
    "typecheck": _t_typecheck,
    "format": _t_format,
    "lsp": _t_lsp,
    "run_tests": _t_run_tests,
    # Pipeline-parity tools (mcp / browser / jupyter / sub-agent delegate).
    "mcp": _t_mcp,
    "browse": _t_browse,
    "execute_ipython_cell": _t_ipython,
    "delegate_to_agent": _t_delegate,
    "delegate": _t_delegate,   # alias
}

_SEARCH_TOOLS = ("grep", "find", "repo_map", "graphify_lookup", "memory_lookup")
_FILE_TOOLS = ("file_read", "read_files", "file_write", "file_create",
               "file_patch", "list_dir", "editor")


def _perf_family(name: str) -> str:
    """Map a tool name to a Perf-page family label (Search / File / Tool)."""
    if name in _SEARCH_TOOLS or "search" in name:
        return "Search"
    if name in _FILE_TOOLS:
        return "File"
    return "Tool"


# PLAN mode (#2): read-only tool subset — inspect + recall, never mutate.
_READONLY_TOOLS = ("file_read", "read_files", "list_dir", "find", "grep", "memory_lookup",
                   "search_chat_sessions", "graphify_lookup", "repo_map",
                   "codegraph_query", "codegraph_callers", "codegraph_callees",
                   "codegraph_impact", "codegraph_explore",
                   "skill_search", "confluence_search", "confluence_read",
                   "jira_search", "jira_read",
                   "email_read",
                   "gitlab_search", "gitlab_read",
                   "web_search", "web_fetch", "web_crawl", "workflow_search",
                   "lsp", "typecheck", "summarize_doc",  # read-only, OK in plan mode
                   # git inspect + line-range read + jira/confluence reads +
                   # list_services — all read-only (also in tool_policy's
                   # _READONLY_ALWAYS_ALLOW); keep the two classifications in sync
                   # so Plan mode doesn't block a tool the policy calls read-only.
                   "git_status", "git_diff", "git_log", "git_blame",
                   "read_lines", "jira_transitions", "confluence_children",
                   "list_services",
                   # Jira/Confluence READ suite + resolvers + the cross-entity
                   # dossier — all read-only. These were ADDED after this list
                   # was written and drifted out of sync, so PLAN MODE blocked
                   # them ("can't read jira in plan mode"): the builtin
                   # jira-read/confluence-read skills route through
                   # context_gather, which this gate refused.
                   "context_gather", "resolve_repo", "jira_resolve_project",
                   "confluence_resolve_space", "jira_worklog", "jira_projects",
                   "jira_remote_links", "jira_boards", "jira_sprints",
                   "jira_sprint_issues", "jira_dashboards",
                   "jira_dashboard_read", "jira_myself",
                   "confluence_spaces", "confluence_page_by_title",
                   "confluence_labels", "confluence_comments",
                   "confluence_descendants")

# Builder-finalize tools — a successful call ENDS the interview (one per builder
# kind: job/skill/workflow/rule). Emitting `builder_done` lets the UI drop the
# session's sticky builder mode so follow-ups are normal chat.
_FINALIZE_TOOLS = frozenset({
    "create_job_script", "learn_skill", "learn_workflow", "remember_rule"})

# Builder finalize tool per kind + how many interview turns before we nudge the
# model to call it (a local model can otherwise chat forever without finalizing).
_BUILDER_FINALIZE_TOOL = {
    "job": "create_job_script", "skill": "learn_skill",
    "workflow": "learn_workflow", "rule": "remember_rule"}
try:
    _BUILDER_NUDGE_AFTER = max(2, int(os.environ.get("AIFORGE_BUILDER_NUDGE_AFTER", "6")))
except (TypeError, ValueError):
    _BUILDER_NUDGE_AFTER = 6

# File-mutating tools that the pre-apply "Review edits" gate (Gap D) holds for
# human Approve/Reject even when policy would auto-allow them.
_MUTATING = ("file_write", "file_create", "file_patch", "editor", "multi_edit",
             "format", "rename_symbol")

# The ``editor`` tool multiplexes read + write sub-commands on one tool NAME;
# only the WRITE sub-commands mutate (view/read/list are read-only and must
# NOT be held by the review-edits gate).
_EDITOR_READONLY_CMDS = ("view", "read", "list", "ls", "cat", "open")


def _editor_is_write(args: dict) -> bool:
    cmd = str((args or {}).get("command")
              or (args or {}).get("sub_command") or "").strip().lower()
    return cmd not in _EDITOR_READONLY_CMDS


def _is_mutating(name: str, args: dict) -> bool:
    """True when this tool call actually writes — ``editor`` view/read is
    read-only; every other name in ``_MUTATING`` always mutates."""
    if name not in _MUTATING:
        return False
    if name == "editor":
        return _editor_is_write(args)
    return True

_PLAN_BANNER = (
    "PLAN MODE — you are READ-ONLY this turn. You may inspect the repo "
    "(file_read, list_dir, find, grep) and recall memory (memory_lookup), but "
    "you CANNOT write files, run commands, install, or change anything. "
    "Investigate, then produce a concrete step-by-step PLAN in FINAL: (files "
    "to touch, commands to run, tests, risks). The user switches to Act mode "
    "to execute it. ASK if you need input to plan well."
)

_ANALYZE_BANNER = (
    "ANALYSIS MODE — you are READ-ONLY this turn. Inspect the repo (file_read, "
    "list_dir, find, grep, repo_map, codegraph_*) and recall memory, but you "
    "CANNOT write files, run commands, install, or change anything. Your job is "
    "to UNDERSTAND and REPORT: produce a clear, structured FINDINGS report in "
    "FINAL: (what the code does, how it's organized, key files/symbols as "
    "path:line, notable risks/gaps). Do NOT propose a change-plan or edits — "
    "this is analysis for the user to read, not an implementation task."
)


