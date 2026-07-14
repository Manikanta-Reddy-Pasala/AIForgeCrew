"""Anti-drift + no-starvation guard for per-agent tool scoping.

Two independent guarantees:

(a) **ANTI-DRIFT** — for every role that is WIRED through
    ``adk_function_tools(role=...)`` (i.e. its agent module actually
    hands the model a role-filtered tool list), every name in that
    role's ``agents.yaml`` ``tools.allowed`` / ``tools.forbidden`` list
    is a REAL tool name from the ``doer_tools`` ground-truth surface.
    This is the anti-regression guard that catches the exact class of bug
    that motivated this work: the YAML said ``grep_repos`` but the real
    function is ``grep_repo`` — strict enforcement then stripped grep
    entirely and silently starved the context gatherers.

    NOTE ON SCOPE: only the roles routed through ``adk_function_tools`` are
    checked. The other archetypes (architect / planner / verifier / …) are
    either external, tool-less (``forbidden: ALL``), or use the GA
    text-protocol tool vocabulary — their allow/forbid names live in a
    DIFFERENT namespace than the doer-tool surface, so checking them
    against it would be a category error. The set of enforced roles is
    discovered from source, so wiring a new role auto-extends this guard.

(b) **NO-STARVATION** — for each wired role, the enforced tool set still
    contains the specific tools that role's prompt/behaviour needs, and
    excludes the clearly-inappropriate write/exec tools. This is the
    critical safety check: reconciliation must never remove a tool an
    agent actually calls.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from aiforge_core.config import agent_config
from aiforge_core.runtime import doer_tools

_AGENTS_DIR = pathlib.Path(doer_tools.__file__).resolve().parents[1] / "agents"

# The roles this feature scopes down. Kept explicit so the anti-drift check
# runs against them regardless of whether the wiring landed yet (RED-first),
# and so a reviewer sees exactly which agents are enforced.
EXPECTED_ENFORCED = {"ctx_memory", "ctx_repomap", "ctx_conventions", "researcher"}


def _ground_truth() -> set[str]:
    """Every real tool name (``FunctionTool.name`` == ``fn.__name__``).

    Union of the base (no-role) set AND role-injected tools (the researcher
    gets web_search/web_read that aren't in the base list — role-scoped web)."""
    out: set[str] = set()
    for role in (None, "researcher"):
        for t in doer_tools.adk_function_tools(role=role):
            n = getattr(t, "name", None) or getattr(
                getattr(t, "func", None), "__name__", "")
            if n:
                out.add(n)
    return out


def _names(tools) -> set[str]:
    out: set[str] = set()
    for t in tools:
        n = getattr(t, "name", None) or getattr(
            getattr(t, "func", None), "__name__", "")
        if n:
            out.add(n)
    return out


def _wired_roles() -> dict[str, str]:
    """Discover roles whose agent module passes ``role=`` to the factory."""
    found: dict[str, str] = {}
    for py in sorted(_AGENTS_DIR.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        if "adk_function_tools(role=" not in src:
            continue
        m = re.search(r'^ROLE\s*=\s*["\'](\w+)["\']', src, re.M)
        if m:
            found[m.group(1)] = py.name
    return found


# ── (a) anti-drift: every allow/forbid name is a real tool ────────────────


@pytest.mark.parametrize("role", sorted(EXPECTED_ENFORCED))
def test_enforced_role_allowlist_names_are_real_tools(role):
    gt = _ground_truth()
    allowed, forbidden = agent_config.allowed_tools_for(role)
    bad_allowed = (set(allowed) - gt) if allowed is not None else set()
    bad_forbidden = set(forbidden) - gt
    assert not bad_allowed, (
        f"{role}.tools.allowed references non-existent tool name(s): "
        f"{sorted(bad_allowed)} — real names: {sorted(gt)}")
    assert not bad_forbidden, (
        f"{role}.tools.forbidden references non-existent tool name(s): "
        f"{sorted(bad_forbidden)} — real names: {sorted(gt)}")


def test_expected_roles_are_actually_wired():
    """Each scoped role's module must pass role= to the tool factory."""
    wired = set(_wired_roles())
    missing = EXPECTED_ENFORCED - wired
    assert not missing, (
        f"these roles are supposed to be tool-scoped but their module still "
        f"calls adk_function_tools() with no role=: {sorted(missing)}")


def test_every_wired_role_has_a_clean_allowlist():
    """Anti-regression: ANY role discovered as wired (now or future) must
    reference only real tool names — so wiring a new role can't silently
    reintroduce the grep_repos starvation bug."""
    gt = _ground_truth()
    problems: list[str] = []
    for role in sorted(_wired_roles()):
        allowed, forbidden = agent_config.allowed_tools_for(role)
        bad = ((set(allowed) - gt) if allowed is not None else set()) | (
            set(forbidden) - gt)
        if bad:
            problems.append(f"{role}: {sorted(bad)}")
    assert not problems, f"wired roles with non-tool names: {problems}"


# ── (b) no-starvation: each wired role keeps the tools it needs ───────────


def test_ctx_repomap_keeps_navigation_tools():
    names = _names(doer_tools.adk_function_tools(role="ctx_repomap"))
    # prompt: repo_map → graphify_lookup → grep + editor view
    assert {"grep_repo", "repo_map", "graphify_lookup", "editor"} <= names
    # clearly-inappropriate write/exec tools scoped out
    for absent in ("git_commit", "file_write", "file_patch", "bash", "run_shell"):
        assert absent not in names, f"{absent} should be scoped out of ctx_repomap"


def test_ctx_conventions_keeps_read_tools():
    names = _names(doer_tools.adk_function_tools(role="ctx_conventions"))
    assert {"grep_repo", "editor"} <= names
    for absent in ("git_commit", "file_write", "bash", "run_shell"):
        assert absent not in names, f"{absent} should be scoped out of ctx_conventions"


def test_researcher_keeps_read_tools():
    names = _names(doer_tools.adk_function_tools(role="researcher"))
    # prompt references file_read / list_dir / memory_lookup / graphify_lookup
    assert {"file_read", "list_dir", "memory_lookup", "graphify_lookup"} <= names
    for absent in ("file_write", "file_patch", "bash", "run_shell", "git_commit"):
        assert absent not in names, f"{absent} should be scoped out of researcher"


def test_ctx_memory_scoped_to_memory_lookup_only():
    names = _names(doer_tools.adk_function_tools(role="ctx_memory"))
    assert names == {"memory_lookup"}


def test_doer_keeps_full_working_surface():
    """The Doer is intentionally NOT scoped (left full-set): its prompt
    references the legacy read/write/exec tools heavily, so scoping would
    starve it. Guard that the full surface still carries the doer essentials."""
    names = _names(doer_tools.adk_function_tools())
    # web_search/web_crawl ARE in the base surface now (66784fc); only the
    # ungated web_read stays researcher-scoped.
    for t in ("project", "subtask_update", "serve", "editor",
              "run_shell", "file_write", "file_patch", "file_read",
              "git_commit", "bash", "ensure_runtime", "web_search", "web_crawl"):
        assert t in names, f"doer full surface is missing {t}"
    assert "web_read" not in names, "ungated web_read stays researcher-only"
    # doer.py must remain unwired (full set) — not in the scoped set.
    assert "doer" not in _wired_roles()
