"""Unit tests for ``aiforge_core.agents`` and ``aiforge_core.eval.rule_checker``.

Network-free: parses the shipped agents.yaml and exercises the
filter / validator / rule-checker entirely in-process.
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
from aiforge_core.eval.rule_checker import check_run


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_YAML = REPO_ROOT / "aiforge_core" / "agents.yaml"

EXPECTED_ROLES = {"architect", "planner", "doer", "feedback", "learner"}


def test_load_all_five_roles_succeeds() -> None:
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


def test_rule_checker_passes_clean_doer_run() -> None:
    contracts = load_agents(SHIPPED_YAML)
    doer = contracts["doer"]
    events = [
        {"tool_calls": [{"name": "file_read"}]},
        {"tool_calls": [{"name": "file_write"}]},
        {"tool_calls": [{"name": "code_run"}]},
    ]
    result = check_run("doer", events, doer, wall_clock_s=120.0, turn_count=3)
    assert result.passed, result.violations
    assert result.stats["tool_calls_total"] == 3


def test_rule_checker_flags_forbidden_tool() -> None:
    contracts = load_agents(SHIPPED_YAML)
    doer = contracts["doer"]
    events = [
        {"tool_calls": [{"name": "file_write"}]},
        {"tool_calls": [{"name": "ask_user"}]},
    ]
    result = check_run("doer", events, doer, wall_clock_s=10.0, turn_count=2)
    assert not result.passed
    assert any("ask_user" in v for v in result.violations)


def test_rule_checker_flags_turn_budget() -> None:
    contracts = load_agents(SHIPPED_YAML)
    doer = contracts["doer"]
    events = [{"tool_calls": [{"name": "file_read"}]}]
    result = check_run("doer", events, doer, wall_clock_s=10.0,
                       turn_count=doer.contract.max_turns + 1)
    assert not result.passed
    assert any("max_turns" in v for v in result.violations)


def test_rule_checker_flags_wall_budget() -> None:
    contracts = load_agents(SHIPPED_YAML)
    doer = contracts["doer"]
    events: list[dict] = []
    result = check_run("doer", events, doer,
                       wall_clock_s=doer.contract.max_wall_s + 1, turn_count=0)
    assert not result.passed
    assert any("wall clock" in v for v in result.violations)


def test_rule_checker_forbidden_all_blocks_any_tool() -> None:
    contracts = load_agents(SHIPPED_YAML)
    feedback = contracts["feedback"]
    events = [{"tool_calls": [{"name": "file_read"}]}]
    result = check_run("feedback", events, feedback,
                       wall_clock_s=1.0, turn_count=1)
    assert not result.passed
    assert any("forbidden=ALL" in v for v in result.violations)


def test_rule_checker_parses_ga_marker_strings() -> None:
    contracts = load_agents(SHIPPED_YAML)
    doer = contracts["doer"]
    events = [
        {"raw": "🛠️ Tool: `file_write`\nDoing the thing"},
        {"raw": "🛠️ Tool: `ask_user`\noops"},
    ]
    result = check_run("doer", events, doer, wall_clock_s=10.0, turn_count=2)
    assert not result.passed
    assert any("ask_user" in v for v in result.violations)
