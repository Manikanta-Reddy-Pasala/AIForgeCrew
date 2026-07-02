"""Unit tests for ``aiforge_core.agents``.

Network-free: parses the shipped agents.yaml and exercises the
filter / validator entirely in-process.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.agents import (
    AgentContract,
    AgentSpecError,
    load_agents,
    tools_schema_for_role,
    validate_contracts,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_YAML = REPO_ROOT / "aiforge_core" / "agents" / "agents.yaml"

CORE_ROLES = {
    "architect", "planner", "verifier", "doer", "feedback", "learner",
}
EXTENDED_ROLES = {
    "triage", "researcher", "refiner",
    # 2026-05-23: Enhancer/Validator bookend the Doer.
    "enhancer", "validator",
    # 2026-06-11: live-boot verifier + ParallelAgent fan-outs.
    "live_verifier",
    "ctx_memory", "ctx_repomap", "ctx_conventions",
    "verify_correctness", "verify_scope", "verify_risk",
    # 2026-06-18: research-completeness critic (research-gap loop).
    "gap_eval",
}
EXPECTED_ROLES = CORE_ROLES | EXTENDED_ROLES


def test_load_all_roles_succeeds() -> None:
    contracts = load_agents(SHIPPED_YAML)
    assert set(contracts.keys()) == EXPECTED_ROLES
    for role, c in contracts.items():
        assert isinstance(c, AgentContract)
        assert c.role == role
        assert c.identity.model
        assert c.contract.max_turns > 0
        assert c.termination_contract


def test_core_six_roles_still_present() -> None:
    """Backwards-compat: the original 6 archetypes must remain wired."""
    contracts = load_agents(SHIPPED_YAML)
    assert CORE_ROLES <= set(contracts.keys())


def test_default_path_loads_when_no_arg() -> None:
    contracts = load_agents()
    assert "doer" in contracts


def test_validate_shipped_yaml_has_no_violations() -> None:
    contracts = load_agents(SHIPPED_YAML)
    violations = validate_contracts(contracts)
    assert violations == [], f"shipped yaml should be clean, got: {violations}"


def test_doer_allowed_includes_oh_parity_tools() -> None:
    """Doer's allowed set = editor/bash/think/finish PLUS the real primary
    file surface (file_write/file_patch/run_shell) — its prompt teaches those
    and local models emit them, so they stay first-class (honest-config fix).
    Genuinely non-doer tools (code_run/ask_user/create_child_ticket) filter out."""
    contracts = load_agents(SHIPPED_YAML)
    full_schema = [
        {"name": "editor", "description": "..."},
        {"name": "bash", "description": "..."},
        {"name": "think", "description": "..."},
        {"name": "finish", "description": "..."},
        {"name": "file_write", "description": "..."},
        {"name": "file_patch", "description": "..."},
        {"name": "code_run", "description": "..."},
        {"name": "ask_user", "description": "..."},
        {"name": "create_child_ticket", "description": "..."},
    ]
    filtered = tools_schema_for_role("doer", full_schema, contracts)
    names = {t["name"] for t in filtered}
    assert "editor" in names
    assert "bash" in names
    assert "think" in names
    assert "finish" in names
    assert "file_write" in names      # real primary surface (not forbidden)
    assert "file_patch" in names      # real primary surface
    assert "code_run" not in names    # genuinely not a doer tool
    assert "ask_user" not in names            # forbidden
    assert "create_child_ticket" not in names  # forbidden


def test_doer_has_full_editor_access() -> None:
    contracts = load_agents()
    doer = contracts["doer"]
    assert doer.editor_commands is None  # None = full access
    assert "editor" in doer.tools.allowed
    assert "bash" in doer.tools.allowed
    assert "think" in doer.tools.allowed
    assert "finish" in doer.tools.allowed


def test_doer_keeps_real_file_surface_not_forbidden() -> None:
    """Honest-config fix: file_read/file_write/file_patch/run_shell are the
    Doer's REAL primary surface (its prompt is built on them + local models
    emit them) — they must be in `allowed`, NOT `forbidden`. Forbidding them
    while the prompt teaches them would brick the Doer if enforcement flips on."""
    contracts = load_agents()
    doer = contracts["doer"]
    allowed = set(doer.tools.allowed or [])
    forbidden = set(doer.tools.forbidden)
    for real in ("file_read", "file_write", "file_patch", "run_shell"):
        assert real in allowed, f"{real} is the Doer's real surface — must be allowed"
        assert real not in forbidden, f"{real} must NOT be forbidden for the Doer"


def test_view_only_roles_have_editor_commands_view() -> None:
    contracts = load_agents()
    for role in ("architect", "planner", "researcher"):
        c = contracts[role]
        assert c.editor_commands == ["view"], (
            f"{role} must restrict editor to view-only"
        )
        assert "editor" in c.tools.allowed


def test_planner_filter_excludes_file_write() -> None:
    contracts = load_agents(SHIPPED_YAML)
    full_schema = [
        {"name": "read_file"},
        {"name": "write_plan"},
        {"name": "create_child_ticket"},
        {"name": "file_write"},
        {"name": "edit_block"},
        {"name": "code_run"},
    ]
    filtered = tools_schema_for_role("planner", full_schema, contracts)
    names = {t["name"] for t in filtered}
    assert "file_write" not in names
    assert "edit_block" not in names
    assert "code_run" not in names
    assert "write_plan" in names
    assert "create_child_ticket" in names


def test_feedback_forbidden_all_returns_empty_schema() -> None:
    contracts = load_agents(SHIPPED_YAML)
    full_schema = [{"name": "file_read"}, {"name": "code_run"}]
    assert tools_schema_for_role("feedback", full_schema, contracts) == []
    assert tools_schema_for_role("learner", full_schema, contracts) == []


def test_unknown_role_in_filter_raises() -> None:
    contracts = load_agents(SHIPPED_YAML)
    with pytest.raises(AgentSpecError):
        tools_schema_for_role("nonexistent", [], contracts)


def test_malformed_yaml_fails_gracefully(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nagents:\n  doer:\n    identity: oops\n")
    with pytest.raises(AgentSpecError):
        load_agents(bad)


def test_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(AgentSpecError):
        load_agents(missing)


def test_yaml_syntax_error_raises(tmp_path: Path) -> None:
    bad = tmp_path / "syntax.yaml"
    bad.write_text("schema_version: 1\nagents: [unclosed\n")
    with pytest.raises(AgentSpecError):
        load_agents(bad)


def test_validate_catches_overlap(tmp_path: Path) -> None:
    spec = """
schema_version: 1
agents:
  bogus:
    identity:
      runtime: adk_agent_with_ga
      model: x
      backend: direct_litellm
      base_url: http://localhost:1234/v1
      ctx_window: 32000
    contract:
      inputs: [a]
      outputs: [b]
      max_turns: 10
      max_wall_s: 60
    tools:
      allowed: [tool_x, tool_y]
      forbidden: [tool_x]
    memory:
      read_scope: full
      write_scope: none
    rule: dummy
    termination_contract: [done]
"""
    p = tmp_path / "overlap.yaml"
    p.write_text(spec)
    contracts = load_agents(p)
    violations = validate_contracts(contracts)
    assert any("both allowed and forbidden" in v for v in violations), violations


def test_validate_catches_out_of_bounds(tmp_path: Path) -> None:
    spec = """
schema_version: 1
agents:
  toobig:
    identity:
      runtime: adk_agent_with_ga
      model: x
      backend: direct_litellm
      base_url: http://localhost:1234/v1
      ctx_window: 32000
    contract:
      inputs: [a]
      outputs: [b]
      max_turns: 9999
      max_wall_s: 99999
    tools:
      allowed: []
      forbidden: [ALL]
    memory:
      read_scope: full
      write_scope: none
    rule: dummy
    termination_contract: [done]
"""
    p = tmp_path / "oob.yaml"
    p.write_text(spec)
    contracts = load_agents(p)
    violations = validate_contracts(contracts)
    assert any("max_turns" in v for v in violations)
    assert any("max_wall_s" in v for v in violations)


