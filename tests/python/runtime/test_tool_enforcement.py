"""Per-agent tool allow/deny enforcement.

``agents.yaml`` declares a per-role ``tools.allowed`` / ``tools.forbidden``
contract. Historically ``adk_function_tools()`` ignored it — every agent got
the FULL tool surface, enforced only by prompt contract. These tests pin the
tool factory to actually honour the lists when a caller passes its role, while
staying byte-for-byte backward-compatible when no role is given.

Semantics under test:
  * ``allowed_tools_for(role)`` parses ``(allowed_or_None, forbidden)`` from
    the real ``agents.yaml``.
  * ``adk_function_tools(role=None)`` → full set (unchanged).
  * ``adk_function_tools(role=<restricted>)`` → allowed − forbidden only.
  * unknown role → full set (backward-compatible default).
  * ``AIFORGE_TOOL_ENFORCE=0`` → full set even for a restricted role.
  * forbidden overrides allowed.
"""
from __future__ import annotations

import pytest

from aiforge_core.config import agent_config
from aiforge_core.runtime import doer_tools


def _names(tools) -> set[str]:
    out: set[str] = set()
    for t in tools:
        n = getattr(t, "name", None) or getattr(
            getattr(t, "func", None), "__name__", "")
        if n:
            out.add(n)
    return out


# ── (a) accessor parses allow/forbid from the real agents.yaml ────────────


def test_allowed_tools_for_parses_restricted_role():
    # ctx_memory: allowed=[memory_lookup], forbidden=[file_write, file_patch,
    # bash, run_shell, ask_user].
    allowed, forbidden = agent_config.allowed_tools_for("ctx_memory")
    assert allowed is not None
    assert "memory_lookup" in allowed
    assert "file_write" in forbidden
    assert "bash" in forbidden


def test_allowed_tools_for_empty_allowed_is_no_allowlist():
    # enhancer: allowed=[] (no allowlist) + a forbidden list → allowed is None.
    allowed, forbidden = agent_config.allowed_tools_for("enhancer")
    assert allowed is None
    assert "file_write" in forbidden


def test_allowed_tools_for_forbidden_all_is_empty_allowlist():
    # verifier: forbidden=ALL → zero tools (explicit empty allowlist).
    allowed, forbidden = agent_config.allowed_tools_for("verifier")
    assert allowed == frozenset()


def test_allowed_tools_for_unknown_role_allows_all():
    allowed, forbidden = agent_config.allowed_tools_for("nonexistent_role_xyz")
    assert allowed is None
    assert forbidden == frozenset()


# ── (b) role=None → full set, unchanged ───────────────────────────────────


def test_role_none_returns_full_set():
    full = doer_tools.adk_function_tools()
    also_full = doer_tools.adk_function_tools(role=None)
    assert _names(full) == _names(also_full)
    assert "git_commit" in _names(full)
    assert "bash" in _names(full)


# ── (c) restricted role → allowed − forbidden only ────────────────────────


def test_restricted_role_filters_to_allowlist():
    full_names = _names(doer_tools.adk_function_tools())
    ctx = _names(doer_tools.adk_function_tools(role="ctx_memory"))
    # allowlist is exactly [memory_lookup]
    assert "memory_lookup" in ctx
    # forbidden / non-allowed tools stripped
    assert "git_commit" not in ctx
    assert "bash" not in ctx
    assert "editor" not in ctx
    assert ctx < full_names  # strict subset


# ── (d) unknown role → full set (backward-compat) ─────────────────────────


def test_unknown_role_returns_full_set():
    full = _names(doer_tools.adk_function_tools())
    unknown = _names(doer_tools.adk_function_tools(role="nonexistent_role_xyz"))
    assert unknown == full


# ── (e) master opt-out AIFORGE_TOOL_ENFORCE=0 → full set ──────────────────


def test_enforcement_opt_out_returns_full_set(monkeypatch):
    monkeypatch.setenv("AIFORGE_TOOL_ENFORCE", "0")
    full = _names(doer_tools.adk_function_tools())
    ctx = _names(doer_tools.adk_function_tools(role="ctx_memory"))
    assert ctx == full  # enforcement disabled → nothing stripped


# ── (f) forbidden overrides allowed ───────────────────────────────────────


def test_forbidden_overrides_allowed(monkeypatch):
    # A tool present in BOTH allowed and forbidden must be removed.
    monkeypatch.setattr(
        agent_config, "allowed_tools_for",
        lambda role: (frozenset({"git_commit", "editor"}),
                      frozenset({"git_commit"})),
    )
    names = _names(doer_tools.adk_function_tools(role="doer"))
    assert "editor" in names
    assert "git_commit" not in names


def test_allowlist_matching_nothing_fails_open(monkeypatch):
    # A non-empty allowlist whose names match NO registered tool is a config
    # typo. It must FAIL OPEN to the full set — never leave the agent tool-less
    # (a zero-tool agent makes the model hallucinate calls that hard-fail with
    # "Tool 'X' not found. Available tools:" — empty).
    monkeypatch.setenv("AIFORGE_TOOL_ENFORCE", "1")
    monkeypatch.setattr(
        agent_config, "allowed_tools_for",
        lambda role: (frozenset({"search_knowledge_bases"}), frozenset()))
    full = _names(doer_tools.adk_function_tools(role=None))
    got = _names(doer_tools.adk_function_tools(role="chat"))
    assert len(got) > 0, "fail-open must not yield a tool-less agent"
    assert got == full


def test_deliberately_toolless_role_stays_empty(monkeypatch):
    # forbidden=ALL → allowed==frozenset() (empty). That's an INTENTIONAL
    # tool-less role and must be respected (not failed open).
    monkeypatch.setenv("AIFORGE_TOOL_ENFORCE", "1")
    monkeypatch.setattr(
        agent_config, "allowed_tools_for",
        lambda role: (frozenset(), frozenset()))
    got = _names(doer_tools.adk_function_tools(role="verify_scope"))
    assert got == set()


def test_all_integrations_wired_into_pipeline_surface(monkeypatch):
    # Every configured integration (Jira / Confluence / Email / GitLab) must be
    # a real tool the pipeline agents can call. GitLab was defined in gitlab.py
    # but never wrapped into adk_function_tools, so team-mode agents couldn't use
    # GitLab at all even when it was configured in the UI.
    monkeypatch.setenv("AIFORGE_TOOL_ENFORCE", "0")
    names = _names(doer_tools.adk_function_tools(role=None))
    for t in ("jira_search", "jira_read", "confluence_search", "email_send",
              "gitlab_search", "gitlab_read", "gitlab_create", "gitlab_update",
              "gitlab_comment", "gitlab_mr_create", "gitlab_mr_comment"):
        assert t in names, f"integration tool {t} missing from pipeline surface"


def test_readonly_fs_search_tools_never_ask():
    # Folder listing / grep / read / recall must run WITHOUT an approval prompt.
    from aiforge_core.runtime.tools import tool_policy as tp
    for t in ("grep_repo", "grep", "list_dir", "ls", "find", "file_read",
              "read", "repo_map", "search", "memory_lookup"):
        assert tp.decide(t, {})["policy"] == tp.ALLOW, t
    # Mutating / external-write tools still gate.
    assert tp.decide("jira_create", {})["policy"] == tp.ASK
    assert tp.decide("email_send", {})["policy"] == tp.ASK


def test_explicit_env_policy_still_overrides_readonly(monkeypatch):
    # An operator can still force an ask on a read tool via env — the pin is a
    # default, not a hard override.
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "grep_repo=ask")
    from aiforge_core.runtime.tools import tool_policy as tp
    assert tp.decide("grep_repo", {})["policy"] == tp.ASK


def test_code_quality_tools_wired_into_pipeline(monkeypatch):
    # The team-mode Doer must be able to VERIFY its own work — type-check, run
    # tests, query the LSP, auto-format — not just edit + shell.
    monkeypatch.setenv("AIFORGE_TOOL_ENFORCE", "0")
    names = _names(doer_tools.adk_function_tools(role=None))
    for t in ("typecheck", "run_tests", "lsp", "format"):
        assert t in names, f"code-quality tool {t} missing from pipeline surface"


def test_power_tools_wired_into_pipeline(monkeypatch):
    # OH-parity power tools that used to be chat-only are now on the pipeline
    # surface too. web_search is intentionally excluded (researcher-only egress).
    monkeypatch.setenv("AIFORGE_TOOL_ENFORCE", "0")
    names = _names(doer_tools.adk_function_tools(role=None))
    for t in ("mcp", "browse", "execute_ipython_cell", "delegate_to_agent",
              "github_pr", "multi_edit"):
        assert t in names, f"power tool {t} missing from pipeline surface"
    assert "web_search" not in names, "web egress stays researcher-only"


def test_git_and_jira_workflow_tools(monkeypatch):
    # New git-inspect + jira-workflow tools are wired into the pipeline; git
    # reads never gate, jira mutations (transition/assign) do.
    monkeypatch.setenv("AIFORGE_TOOL_ENFORCE", "0")
    names = _names(doer_tools.adk_function_tools(role=None))
    for t in ("git_status", "git_diff", "git_log",
              "jira_transitions", "jira_transition", "jira_assign"):
        assert t in names, f"{t} missing from pipeline surface"
    from aiforge_core.runtime.tools import tool_policy as tp
    for t in ("git_status", "git_diff", "git_log", "jira_transitions"):
        assert tp.decide(t, {})["policy"] == tp.ALLOW, t
    for t in ("jira_transition", "jira_assign"):
        assert tp.decide(t, {})["policy"] == tp.ASK, t
