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

EXPECTED_ROLES = {
    "architect", "planner", "verifier", "doer", "feedback", "learner",
}


def test_load_all_six_roles_succeeds() -> None:
    contracts = load_agents(SHIPPED_YAML)
    assert set(contracts.keys()) == EXPECTED_ROLES
    for role, c in contracts.items():
        assert isinstance(c, AgentContract)
        assert c.role == role
        assert c.identity.model
        assert c.contract.max_turns > 0
        assert c.termination_contract


def test_default_path_loads_when_no_arg() -> None:
    contracts = load_agents()
    assert "doer" in contracts


def test_validate_shipped_yaml_has_no_violations() -> None:
    contracts = load_agents(SHIPPED_YAML)
    violations = validate_contracts(contracts)
    assert violations == [], f"shipped yaml should be clean, got: {violations}"


def test_doer_allowed_includes_file_write() -> None:
    contracts = load_agents(SHIPPED_YAML)
    full_schema = [
        {"name": "file_read", "description": "..."},
        {"name": "file_write", "description": "..."},
        {"name": "file_patch", "description": "..."},
        {"name": "code_run", "description": "..."},
        {"name": "ask_user", "description": "..."},
        {"name": "create_child_ticket", "description": "..."},
    ]
    filtered = tools_schema_for_role("doer", full_schema, contracts)
    names = {t["name"] for t in filtered}
    assert "file_write" in names
    assert "file_patch" in names
    assert "code_run" in names
    assert "ask_user" not in names
    assert "create_child_ticket" not in names


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


