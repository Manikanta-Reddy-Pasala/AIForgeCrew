"""ADK wiring: ``adk_function_tools`` builds the role-scoped list of
``google.adk.tools.FunctionTool`` instances from the tools defined across the
sibling submodules.

Split out of the former ``doer_tools`` module — moved verbatim.
"""
from __future__ import annotations

import os

from ..graphify_lookup_tool import graphify_lookup
from ..memory_block_tool import memory_block
from ..memory_lookup_tool import memory_lookup

from ._fs import (
    file_patch, file_read, file_write, grep_repo, list_dir, read_lines,
    run_shell,
)
from ._web import fetch_url, web_crawl, web_read
from ._repo import (
    codegraph_callees, codegraph_callers, codegraph_explore, codegraph_impact,
    codegraph_query, git_blame, git_commit, git_diff, git_log, git_status,
    impacted_tests, rename_symbol, repo_map,
)
from ._integrations import (
    confluence_attach, confluence_children, confluence_create,
    confluence_read, confluence_search, confluence_update, email_read,
    email_send, gitlab_comment, gitlab_create, gitlab_mr_comment,
    gitlab_mr_create, gitlab_pipeline, gitlab_pipeline_watch, gitlab_pipelines,
    gitlab_read, gitlab_search, gitlab_update, jira_assign,
    jira_comment, jira_create, jira_link_issues, jira_read, jira_remote_links,
    jira_search, jira_transition, jira_transitions, jira_update, jira_worklog,
    resolve_repo,
)
from ._tools import (
    bash, browse, commit, delegate_to_agent, edit, execute_ipython_cell,
    format, git_add_commit, github_pr, glob, grep, http_get, learn_skill,
    learn_workflow, lsp, ls, mcp, multi_edit, patch, read, run, run_tests,
    search, serve, shell, skill_search, stop_service, str_replace,
    subtask_update, task, todo_write, todowrite, typecheck, web_fetch, write,
    workflow_search,
)


def adk_function_tools(role: "str | None" = None) -> list:
    """Resolve the role's tool list, LOG the granted set (so operators can see
    exactly what each agent can do before it picks up work), and return it."""
    tools = _adk_function_tools_impl(role)
    try:
        import logging
        names = sorted(
            (getattr(t, "name", None)
             or getattr(getattr(t, "func", None), "__name__", "")) for t in tools)
        logging.getLogger("aiforge.tools").info(
            "tools[role=%s] granted %d: %s",
            role or "*", len(names), ", ".join(n for n in names if n))
    except Exception:  # noqa: BLE001 — logging must never break tool wiring
        pass
    return tools


def _adk_function_tools_impl(role: "str | None" = None) -> list:
    """Return the tool list as ADK ``FunctionTool`` instances.

    Lazy import keeps unit tests ADK-free.

    Order — OpenHands-parity tools first (editor/bash/think/finish from
    :mod:`aiforge_core.runtime.tools`), then legacy canonical names, then
    aliases.

    ``role`` — when ``None`` (the default), the FULL set is returned,
    byte-for-byte identical to the historical behaviour (all existing
    callers that pass no role are unchanged). When a role is given, the
    list is filtered by that role's ``tools.allowed`` / ``tools.forbidden``
    contract from ``agents.yaml`` (see
    :func:`aiforge_core.config.agent_config.allowed_tools_for`): a tool
    passes only if its function name is in ``allowed`` (or ``allowed`` is
    unrestricted) AND not in ``forbidden``. This is the real per-agent
    scoping backstop — a ctx-gatherer no longer merely *shouldn't* call
    git_commit, it literally never receives the schema.

    Master opt-out: ``AIFORGE_TOOL_ENFORCE=0`` disables filtering entirely
    (returns the full set) so a misconfigured allowlist can be neutralised
    without a redeploy. Unknown / unconfigured roles allow all (safe,
    backward-compatible default). Soft-fail: any accessor error → full set.

    Legacy tools (file_read/file_write/file_patch/list_dir/run_shell)
    are DEPRECATED — kept one release as escape hatches for hallucinated
    names. Doer's ``forbidden`` list in ``agents.yaml`` blocks them.
    """
    from google.adk.tools import FunctionTool

    # New OH-parity surface (sub-project #1)
    from aiforge_core.runtime.tools.bash import bash as new_bash
    from aiforge_core.runtime.tools.cognition import finish, think
    from aiforge_core.runtime.tools.editor import editor
    from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
    from aiforge_core.runtime.tools.project_runner import project

    new_canonical = [editor, new_bash, think, finish, ensure_runtime, project]
    legacy_canonical = [file_read, file_write, file_patch, list_dir, run_shell,
                        grep_repo, repo_map, impacted_tests, fetch_url,
                        git_commit, memory_lookup, memory_block, graphify_lookup,
                        skill_search, learn_skill,
                        workflow_search, learn_workflow, serve, stop_service,
                        subtask_update,
                        confluence_search, confluence_read, confluence_create,
                        confluence_update, jira_search, jira_read, jira_create,
                        jira_update, jira_comment, email_send, email_read,
                        gitlab_search, gitlab_read, gitlab_create, gitlab_update,
                        gitlab_comment, gitlab_mr_create, gitlab_mr_comment,
                        gitlab_pipelines, gitlab_pipeline, gitlab_pipeline_watch,
                        codegraph_impact, codegraph_callers, codegraph_callees,
                        codegraph_explore, codegraph_query,
                        typecheck, run_tests, lsp, format,
                        mcp, browse, execute_ipython_cell, delegate_to_agent,
                        github_pr, multi_edit,
                        git_status, git_diff, git_log, git_blame,
                        jira_transitions, jira_transition, jira_assign,
                        jira_link_issues, jira_worklog, jira_remote_links,
                        resolve_repo,
                        confluence_children, confluence_attach,
                        read_lines, rename_symbol]
    aliases = [read, write, patch, edit, str_replace, ls, shell, bash, run,
               grep, search, http_get, web_fetch, web_crawl,
               commit, git_add_commit,
               todo_write, todowrite, glob, task]
    tools = [FunctionTool(func=fn)
             for fn in new_canonical + legacy_canonical + aliases]
    # Web egress:
    #   * web_fetch + web_crawl are in the base list — every agent gets them,
    #     behind the AIFORGE_ALLOW_WEB_FETCH gate + the AIFORGE_WEB_FETCH_DISABLE
    #     hard-off (SSRF-guarded). web_crawl USED to skip that gate for every
    #     role via a hardcoded sanctioned=True; it no longer does.
    #   * there is NO web SEARCH tool: the query string is outbound data and
    #     nothing filtered it, so the capability was removed (2026-09-03). An
    #     agent reads a URL it was GIVEN; it cannot go looking for one.
    #   * web_read (raw page read) stays RESEARCHER-only. It is no longer
    #     UNGATED: an unattended pre-planner role with gate-free egress was the
    #     widest hole in the system once search was removed.
    if role == "researcher":
        tools = tools + [FunctionTool(func=web_read)]
    tools = _apply_codegraph_gate(tools)
    if role is None:
        return tools
    if os.environ.get("AIFORGE_TOOL_ENFORCE", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return tools
    return _filter_tools_by_role(tools, role)


def _tool_name(t) -> str:
    return getattr(t, "name", None) or getattr(
        getattr(t, "func", None), "__name__", "")


def _apply_codegraph_gate(tools: list) -> list:
    """Drop the codegraph_* tools unless codegraph is usable on this run — the
    SINGLE shared gate (binary + real index for the repo + not env-disabled +
    not opted out per-ticket), the same one the Doer seed mandate and chat
    catalog use, so all three agree."""
    cg_names = {"codegraph_impact", "codegraph_callers", "codegraph_callees",
                "codegraph_explore", "codegraph_query"}
    try:
        from aiforge_core.runtime.tools import codegraph as _cg
        cg_on = _cg.enabled_for_run()
    except Exception:  # noqa: BLE001
        cg_on = False
    if cg_on:
        return tools
    return [t for t in tools if _tool_name(t) not in cg_names]


def _filter_tools_by_role(tools: list, role: str) -> list:
    """Filter ``tools`` by the role's allowed/forbidden contract from
    agents.yaml. A tool passes only if its name is in ``allowed`` (or allowed is
    unrestricted) AND not in ``forbidden``. Soft-fail: any accessor error → full
    set. A NON-EMPTY allowlist that matched zero registered tools is almost
    always a config typo — fail OPEN to the base set + log, so the agent isn't
    tool-less. A DELIBERATELY tool-less role uses forbidden=ALL → allowed ==
    frozenset() (falsy here), still respected."""
    try:
        from aiforge_core.config.agent_config import allowed_tools_for
        allowed, forbidden = allowed_tools_for(role)
    except Exception:  # noqa: BLE001 — never break the build over enforcement
        return tools
    if allowed is None and not forbidden:
        return tools  # unrestricted role → full set
    out = []
    for t in tools:
        name = _tool_name(t)
        if name in forbidden:
            continue
        if allowed is not None and name not in allowed:
            continue
        out.append(t)
    if not out and allowed:
        import logging
        logging.getLogger("aiforge.tools").warning(
            "adk_function_tools(role=%s): allowlist %s matched no registered "
            "tool — failing open to the full set so the agent isn't tool-less.",
            role, sorted(allowed))
        return tools
    return out
