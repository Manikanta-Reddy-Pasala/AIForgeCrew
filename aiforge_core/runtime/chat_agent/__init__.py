"""Conversational full-filesystem coding agent (deploy-anywhere chat).

A lightweight, provider-agnostic ReAct loop — NOT the ticket pipeline.
Streams steps back to the Chat UI. The model talks a plain text
protocol (no native tool-calling) so it works across every backend the
home page can point at (LM Studio, OpenRouter, Groq, vLLM, cloud).

Tools run with TOTAL filesystem + exec freedom by design (the operator
chose whole-machine access). An optional ``AIFORGE_WORKSPACE_DIR``
clamps file/exec operations to a root for cautious deploys.

Protocol — each model turn must be either a tool call:

    THOUGHT: <reasoning>
    ACTION: <tool_name>
    ARGS_JSON: {"path": "..."}

or a final answer:

    THOUGHT: <reasoning>
    FINAL: <message to the user>

Public surface:
    run_chat_agent(messages, *, cwd, role, max_steps, complete_fn)
        -> Iterator[dict]   # SSE-ready event dicts
"""

from __future__ import annotations

# Re-export the stdlib names the original single-file module exposed as
# top-level attributes (``chat_agent.time``, ``chat_agent.os`` …) so callers /
# tests that monkeypatch or reference them keep working after the package split.
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from ._shell import (
    _ACTION_RE,
    _ARGS_RE,
    _FINAL_RE,
    _ASK_RE,
    _THOUGHT_RE,
    _MAX_OBS,
    _MAX_OBS_READ,
    _READ_OBS_TOOLS,
    _BLANKET_ADD_SELECTORS,
    _GIT_GLOBAL_VALUE_OPTS,
    _ENV_ASSIGN_RE,
    _SEGMENT_SPLIT_RE,
    _mask_noncode,
    _is_blanket_git,
    _is_server_start,
    _workspace_root,
    _resolve,
    _t_file_read,
    _SYNTAX_EXTS,
    _syntax_check,
    _t_file_write,
    _t_file_patch,
    _t_list_dir,
    _SCRIPT_RUNNERS,
    _SCRIPT_EXTS,
    _preflight_missing_path,
    _OBS_TEXT_KEYS,
    _smart_truncate_obs,
    _t_run_command,
    _kill_proc,
)
from ._tools import (
    _GIT_TOPLEVEL_CACHE,
    _git_toplevel,
    _chat_repo_key,
    _t_memory_lookup,
    _t_search_chat_sessions,
    _t_memory_write,
    _t_create_job_script,
    _SKIP_DIRS,
    _t_find,
    _t_grep,
    _ELABORATE_PROMPT,
    _elaborate_body,
    _t_remember_rule,
    _BULLET_TRIGGERS_RE,
    _parse_bullet,
    _source_path_cache,
    _cached_find_by_source,
    _preferences_context,
    _rules_context,
    _t_ensure_runtime,
    _t_project,
    _t_confluence_search,
    _t_confluence_read,
    _t_confluence_create,
    _t_confluence_update,
    _t_set_repo_folder,
    _t_set_repo_root,
    _t_list_repos,
    _t_set_integration_default,
    _t_jira_search,
    _t_jira_read,
    _t_jira_worklog,
    _t_jira_remote_links,
    _t_resolve_repo,
    _t_jira_resolve_project,
    _t_confluence_resolve_space,
    _t_context_gather,
    _t_note_curate,
    _t_note_consolidate,
    _t_codegraph_query,
    _t_codegraph_callers,
    _t_codegraph_callees,
    _t_codegraph_impact,
    _t_codegraph_explore,
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
    _t_email_send,
    _t_email_read,
    _t_gitlab_search,
    _t_gitlab_read,
    _t_gitlab_create,
    _t_gitlab_update,
    _t_gitlab_comment,
    _t_gitlab_mr_create,
    _t_gitlab_mr_comment,
    _t_gitlab_pipeline,
    _t_gitlab_pipeline_watch,
    _t_gitlab_pipelines,
    _t_github_pr,
    _t_web_search,
    _t_web_fetch,
    _t_web_crawl,
    _t_serve,
    _t_stop_service,
    _t_list_services,
    _t_skill_search,
    _t_learn_skill,
    _t_workflow_search,
    _t_learn_workflow,
    _ROOT_SCOPED_TOOLS,
    _scoped_root,
    _coerce_int,
    _t_editor,
    _t_multi_edit,
    _t_typecheck,
    _t_format,
    _t_lsp,
    _t_run_tests,
    _git_cli,
    _t_git_status,
    _t_git_diff,
    _t_git_log,
    _t_jira_transitions,
    _t_jira_transition,
    _t_jira_assign,
    _t_jira_link_issues,
    _t_confluence_children,
    _t_confluence_attach,
    _t_confluence_spaces,
    _t_confluence_page_by_title,
    _t_confluence_labels,
    _t_confluence_add_label,
    _t_confluence_comments,
    _t_confluence_comment,
    _t_confluence_descendants,
    _t_git_blame,
    _t_read_lines,
    _t_rename_symbol,
    _chat_run_id,
    _t_mcp,
    _t_browse,
    _t_ui_check,
    _t_ui_ask,
    _t_ipython,
    _t_delegate,
)
from ._registry import (
    TOOLS,
    _SEARCH_TOOLS,
    _FILE_TOOLS,
    _perf_family,
    _READONLY_TOOLS,
    _FINALIZE_TOOLS,
    _BUILDER_FINALIZE_TOOL,
    _BUILDER_NUDGE_AFTER,
    _MUTATING,
    _EDITOR_READONLY_CMDS,
    _editor_is_write,
    _is_mutating,
    _PLAN_BANNER,
    _ANALYZE_BANNER,
)
from ._preview import (
    _DIFF_COMPUTE_MAX,
    _fence,
    _xhtml_to_md,
    _change_diff,
    _fetch_current,
    _diff_preview,
)
from ._prompt import (
    _SYSTEM,
    _balanced_json,
    _REASONING_PREFIX_RE,
    _strip_reasoning_prefix,
    _PROTOCOL_NOISE_RE,
    _strip_protocol_noise,
    _parse,
)
from ._context import (
    _LOOP_REPEAT,
    _OUTPUT_REPEAT,
    _CONDENSE_OPEN,
    _CONDENSE_CLOSE,
    _GOAL_PIN_OPEN,
    _GOAL_PIN_CLOSE,
    _WEB_INTENT_STRONG_RE,
    _WEB_INTENT_WEAK_RE,
    _LOCAL_CODE_CTX_RE,
    _has_web_intent,
    _WEB_LOOKUP_DIRECTIVE,
    _CANCELLED,
    _GEN_SEM,
    _gen_sem,
    _complete_cancellable,
    _resolved_window,
    _window_scaled,
    _cave_mode,
    _compress_prompt,
    _SYSTEM_PROMPT_CHARS,
    _CTX_BUDGET_FLOOR_CHARS,
    _ctx_budget_chars,
    _SYS_PROMPT_FLOOR_CHARS,
    _sys_prompt_frac,
    _sys_prompt_budget_chars,
    _SYS_CAP_MARK,
    _cap_system_prompt,
    _compact_mode,
    _COMPACT_SYS,
    _text_of,
    _condense_timeout_s,
    _llm_summarize_middle,
    _compact_convo,
    _ctx_on,
    _repomap_max_chars,
    _SYM_PATTERNS,
    _build_symbol_map,
    _fmt_symbol_rows,
    _build_repo_map,
    _repo_name,
    _TOOL_TAG_HINTS,
    _tool_tags,
    _ASK_LEAD_RE,
    _split_asks,
    _memory_recall,
    _chat_session_recall,
    _repo_context,
    _fire_stop,
    _EDIT_TOOL_NAMES,
    _verify_on_final_enabled,
    _verify_max_rounds,
    _run_project_verify,
    _post_edit_syntax_error,
    _verify_fix_message,
)
from ._loop import (
    run_chat_agent,
)

__all__ = [
    'TOOLS',
    'run_chat_agent',
]
