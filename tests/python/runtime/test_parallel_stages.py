"""Tests for the ParallelAgent context + verifier stages and their merge
callbacks (runtime.parallel_stages)."""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime import parallel_stages as ps


class _Ctx:
    """Minimal stand-in for an ADK callback_context."""

    def __init__(self, state: dict) -> None:
        self.state = state


def _run(coro):
    return asyncio.run(coro)


# ── context-brief merge ────────────────────────────────────────────────

def test_merge_context_briefs_concats_present_briefs() -> None:
    state = {
        "research_brief_md": "found foo.py",
        "memory_brief_md": "past failure X",
        "repo_brief_md": "bar.py::Baz",
        # conventions intentionally missing
    }
    ctx = _Ctx(state)
    _run(ps.merge_context_briefs(callback_context=ctx))
    merged = state["context_brief_md"]
    assert "## Researcher" in merged
    assert "found foo.py" in merged
    assert "## Memory" in merged
    assert "## Repo map" in merged
    # missing/blank briefs are skipped, no empty Conventions header
    assert "## Conventions" not in merged


def test_merge_context_briefs_no_briefs_is_noop() -> None:
    state: dict = {}
    _run(ps.merge_context_briefs(callback_context=_Ctx(state)))
    assert "context_brief_md" not in state


def test_merge_context_briefs_skips_blank() -> None:
    state = {"research_brief_md": "   ", "memory_brief_md": "real"}
    _run(ps.merge_context_briefs(callback_context=_Ctx(state)))
    assert "## Researcher" not in state["context_brief_md"]
    assert "## Memory" in state["context_brief_md"]


# ── verifier-verdict merge ─────────────────────────────────────────────

def test_merge_verifier_all_pass() -> None:
    state = {
        "verify_correctness": '{"verdict": "pass"}',
        "verify_scope": '{"verdict": "pass"}',
        "verify_risk": {"verdict": "pass"},
    }
    _run(ps.merge_verifier_verdicts(callback_context=_Ctx(state)))
    assert state["verifier_verdict"]["verdict"] == "pass"


def test_merge_verifier_any_reject_rejects() -> None:
    state = {
        "verify_correctness": '{"verdict": "pass"}',
        "verify_scope": '{"verdict": "reject", "rationale": "empty globs"}',
        "verify_risk": '{"verdict": "pass"}',
    }
    _run(ps.merge_verifier_verdicts(callback_context=_Ctx(state)))
    v = state["verifier_verdict"]
    assert v["verdict"] == "reject"
    assert "scope" in v["rationale"]
    # the rejecting axis's rationale is surfaced as an issue
    assert any("empty globs" in i.get("message", "") for i in v["issues"])


def test_merge_verifier_fenced_json_parses() -> None:
    state = {
        "verify_correctness": '```json\n{"verdict": "reject", "rationale": "x"}\n```',
        "verify_scope": "pass",
        "verify_risk": "pass",
    }
    _run(ps.merge_verifier_verdicts(callback_context=_Ctx(state)))
    assert state["verifier_verdict"]["verdict"] == "reject"


def test_merge_verifier_unparseable_defaults_pass() -> None:
    state = {
        "verify_correctness": "garbage not json",
        "verify_scope": "",
        "verify_risk": None,
    }
    _run(ps.merge_verifier_verdicts(callback_context=_Ctx(state)))
    # a formatting slip must not block the pipeline
    assert state["verifier_verdict"]["verdict"] == "pass"


def test_merge_verifier_matches_runner_extractor() -> None:
    """The merged dict must be readable by adk_runner._extract_verifier."""
    from aiforge_core.runtime.adk_runner import _extract_verifier
    state = {
        "verify_correctness": '{"verdict": "reject", "rationale": "r"}',
        "verify_scope": '{"verdict": "pass"}',
        "verify_risk": '{"verdict": "pass"}',
    }
    _run(ps.merge_verifier_verdicts(callback_context=_Ctx(state)))
    assert _extract_verifier(state) == "reject"


# ── builders ───────────────────────────────────────────────────────────

@pytest.fixture
def _no_escalate(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")


def test_build_context_parallel_branches(_no_escalate) -> None:
    from aiforge_core.runtime.pipeline import build_litellm_model
    stage = ps.build_context_parallel(build_litellm_model)
    names = [s.name for s in stage.sub_agents]
    assert names == ["researcher", "ctx_memory", "ctx_repomap", "ctx_conventions"]
    assert stage.name == "context_gather"


def test_build_context_parallel_skip_researcher(_no_escalate) -> None:
    from aiforge_core.runtime.pipeline import build_litellm_model
    stage = ps.build_context_parallel(build_litellm_model, skip_researcher=True)
    names = [s.name for s in stage.sub_agents]
    assert "researcher" not in names
    assert names == ["ctx_memory", "ctx_repomap", "ctx_conventions"]


def test_build_verifier_parallel_branches(_no_escalate) -> None:
    from aiforge_core.runtime.pipeline import build_litellm_model
    stage = ps.build_verifier_parallel(build_litellm_model)
    names = [s.name for s in stage.sub_agents]
    assert names == ["verify_correctness", "verify_scope", "verify_risk"]
    assert stage.name == "verifier"
