"""Tests for ``aiforge_core.runtime.verifier_strict``."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import verifier_strict as vs


def _plan_with(subtickets: list[dict]) -> dict:
    return {"child_subtickets": subtickets}


def test_clean_plan_passes():
    plan = _plan_with([
        {"id": "feature-x", "scope_allowlist_globs": ["src/**"]},
        {"id": "test-feature-x", "scope_allowlist_globs": ["tests/**"]},
    ])
    out = vs.apply(plan, base_verdict={"verdict": "pass", "issues": [],
                                       "rationale": "looks good"})
    assert out["verdict"] == "pass"
    assert out["issues"] == []


def test_too_many_subtickets_rejected():
    plan = _plan_with([
        {"id": f"s{i}", "scope_allowlist_globs": ["src/**"]}
        for i in range(vs.MAX_SUBTICKETS + 1)
    ])
    out = vs.apply(plan, base_verdict={"verdict": "pass", "issues": []})
    assert out["verdict"] == "reject"
    assert any(i["kind"] == "strict_too_many_subtickets" for i in out["issues"])


def test_overscoped_subticket_flagged():
    plan = _plan_with([{
        "id": "huge",
        "scope_allowlist_globs": ["src/**"],
        "files": [f"src/f{i}.py" for i in range(vs.MAX_FILES_PER_SUBTICKET + 1)],
    }, {"id": "test-huge", "scope_allowlist_globs": ["tests/**"]}])
    out = vs.apply(plan, base_verdict={"verdict": "pass", "issues": []})
    assert out["verdict"] == "reject"
    assert any(i["kind"] == "strict_overscoped_subticket" for i in out["issues"])


def test_missing_scope_allowlist_flagged():
    plan = _plan_with([
        {"id": "lazy"},
        {"id": "test-lazy", "scope_allowlist_globs": ["tests/**"]},
    ])
    out = vs.apply(plan, base_verdict={"verdict": "pass", "issues": []})
    kinds = [i["kind"] for i in out["issues"]]
    assert "strict_missing_scope_allowlist" in kinds


def test_no_test_subticket_flagged():
    plan = _plan_with([
        {"id": "feature-x", "scope_allowlist_globs": ["src/**"]},
    ])
    out = vs.apply(plan, base_verdict={"verdict": "pass", "issues": []})
    assert out["verdict"] == "reject"
    assert any(i["kind"] == "strict_no_test_subticket" for i in out["issues"])


def test_rule_error_does_not_crash():
    """A buggy rule should produce a strict_rule_error issue, not raise."""
    def broken_rule(plan):
        raise RuntimeError("kaboom")
    rules = vs.RULES
    try:
        vs.RULES = (broken_rule,)
        out = vs.apply({}, base_verdict={"verdict": "pass", "issues": []})
        assert any(i["kind"] == "strict_rule_error" for i in out["issues"])
    finally:
        vs.RULES = rules


def test_existing_issues_preserved():
    """LLM-emitted issues must survive the strict pass."""
    base = {
        "verdict": "reject",
        "issues": [{"kind": "llm_issue", "message": "missing rationale"}],
        "rationale": "LLM said reject",
    }
    plan = _plan_with([
        {"id": "feature-x", "scope_allowlist_globs": ["src/**"]},
        {"id": "test-feature-x", "scope_allowlist_globs": ["tests/**"]},
    ])
    out = vs.apply(plan, base)
    assert out["verdict"] == "reject"
    assert any(i.get("kind") == "llm_issue" for i in out["issues"])
    assert out["rationale"] == "LLM said reject"


def test_none_base_verdict_treated_as_pass():
    """Apply works when no LLM verdict is supplied yet."""
    plan = _plan_with([
        {"id": "feature-x", "scope_allowlist_globs": ["src/**"]},
        {"id": "test-feature-x", "scope_allowlist_globs": ["tests/**"]},
    ])
    out = vs.apply(plan, base_verdict=None)
    assert out["verdict"] == "pass"
