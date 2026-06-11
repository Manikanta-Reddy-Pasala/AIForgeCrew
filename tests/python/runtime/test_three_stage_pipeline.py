"""Unit tests for the Claude-Enhance → local-Doer → Claude-Validate
pattern, now wired through proper ADK LlmAgents (2026-05-23 refactor).

Covers:
* pipeline.build_pipeline — Enhancer at index 0, Validator at end
* lm_health.check_lm_health — both endpoints OK / fail / restart
* failure_memory.record_failure + after-callback shape
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime import failure_memory, lm_health, pipeline

# ── Pipeline shape ─────────────────────────────────────────────────────

def _edge_set(p):
    """Return {(from, to, route)} for the Workflow graph."""
    return {(e.from_node.name, e.to_node.name, e.route) for e in p.graph.edges}


def test_pipeline_is_workflow_graph_with_core_nodes() -> None:
    p = pipeline.build_pipeline(skip_researcher=True)
    from google.adk.workflow import Workflow
    assert isinstance(p, Workflow)
    names = {n.name for n in p.graph.nodes}
    # core agents + routers + joins present
    for n in ("triage", "enhancer", "planner", "doer", "validator", "learner",
              "triage_gate", "loop_gate", "validator_gate",
              "context_join", "verifier_join", "merge_context",
              "merge_verdicts"):
        assert n in names, (n, names)
    # live_verifier runs standalone post-PR, not in the graph
    assert "live_verifier" not in names


def test_pipeline_routing_edges() -> None:
    p = pipeline.build_pipeline(skip_researcher=True)
    edges = _edge_set(p)
    # triage runs first and feeds the gate (populates triage_verdict)
    assert ("__START__", "triage", None) in edges
    assert ("triage", "triage_gate", None) in edges
    # fast-path + full-path switch off triage
    assert ("triage_gate", "doer", "trivial") in edges
    assert ("triage_gate", "enhancer", "full") in edges
    # doer loop: feedback → loop_gate ⟲ doer, exit → validator
    assert ("loop_gate", "doer", "loop") in edges
    assert ("loop_gate", "validator", "exit") in edges
    # replan edge back to planner; done → learner
    assert ("validator_gate", "planner", "replan") in edges
    assert ("validator_gate", "learner", "done") in edges


def test_pipeline_researcher_is_parallel_branch() -> None:
    p = pipeline.build_pipeline(skip_researcher=False)
    edges = _edge_set(p)
    names = {n.name for n in p.graph.nodes}
    assert "researcher" in names
    # researcher fans out from enhancer and converges at context_join
    assert ("enhancer", "researcher", None) in edges
    assert ("researcher", "context_join", None) in edges


def test_pipeline_skip_researcher_drops_branch() -> None:
    p = pipeline.build_pipeline(skip_researcher=True)
    names = {n.name for n in p.graph.nodes}
    assert "researcher" not in names
    # the other three gatherers remain
    assert {"ctx_memory", "ctx_repomap", "ctx_conventions"} <= names


def test_build_live_verifier_agent_standalone() -> None:
    """The runner builds the live_verifier on its own, post-PR."""
    lv = pipeline.build_live_verifier_agent(project="PosClientBackend")
    assert lv.name == "live_verifier"
    inst = lv.instruction
    assert callable(inst)  # InstructionProvider — recipe braces survive
    assert "%{http_code}" in inst(None)


# ── LM health ────────────────────────────────────────────────────────────

def test_lm_health_both_endpoints_ok(monkeypatch) -> None:
    monkeypatch.setattr(lm_health, "_probe", lambda url, timeout=3.0: True)
    monkeypatch.setattr(lm_health, "_restart_tunnel", lambda u: True)
    out = lm_health.check_lm_health()
    assert out["ok"] is True
    assert out["doer_ok"] is True
    assert out["planner_ok"] is True
    assert out["restarted"] == []


def test_lm_health_doer_down_triggers_restart(monkeypatch) -> None:
    probe_calls = {"count": 0}

    def _probe(url, timeout=3.0):
        probe_calls["count"] += 1
        # First two probes (doer first call + planner first call) fail;
        # post-restart probes return True so the function logs the
        # recovery path.
        return probe_calls["count"] > 2

    monkeypatch.setattr(lm_health, "_probe", _probe)
    monkeypatch.setattr(lm_health, "_restart_tunnel", lambda u: True)
    out = lm_health.check_lm_health(restart_on_fail=True)
    assert "doer" in out["restarted"]
    assert "planner" in out["restarted"]
    assert out["doer_ok"] is True
    assert out["planner_ok"] is True


def test_lm_health_no_restart_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(lm_health, "_probe", lambda url, timeout=3.0: False)
    monkeypatch.setattr(
        lm_health, "_restart_tunnel",
        lambda u: pytest.fail("must not be called"),
    )
    out = lm_health.check_lm_health(restart_on_fail=False)
    assert out["ok"] is False
    assert out["restarted"] == []


# ── Failure memory ───────────────────────────────────────────────────────

class _StubTicket:
    def __init__(self, body="goal: do X", title="T", project="repo"):
        self.title = title
        self.body = body
        self.project = project
        self.metadata = None
        self.identifier = "ONE-99"


def test_failure_memory_skips_on_pass() -> None:
    out = failure_memory.record_failure(_StubTicket(), verdict="pass")
    assert out["ok"] is False
    assert out["error"] == "not_a_failure"


def test_failure_memory_skips_without_project() -> None:
    out = failure_memory.record_failure(
        _StubTicket(project=""), verdict="fail",
    )
    assert out["ok"] is False
    assert out["error"] == "no_project"


def test_failure_memory_calls_upsert(monkeypatch) -> None:
    t = _StubTicket()
    fake_upsert = MagicMock(
        return_value={"id": "obs_xyz", "deduped": False},
    )
    fake_store = MagicMock(upsert_observation=fake_upsert)
    fake_gdb = MagicMock()
    fake_gdb.driver.return_value = MagicMock()
    with patch.dict("sys.modules", {
        "aiforge_memory.features.memory.store": fake_store,
        "neo4j": MagicMock(GraphDatabase=fake_gdb),
    }):
        out = failure_memory.record_failure(
            t, verdict="fail", reason="something broke",
            ci_status="red",
        )
    assert out["ok"] is True
    fake_upsert.assert_called_once()
    kwargs = fake_upsert.call_args.kwargs
    assert kwargs["kind"] == "failure"
    assert "kind:failure" in kwargs["tags"]
    assert "ci:red" in kwargs["tags"]


def test_failure_memory_callback_skips_on_clean_pass(monkeypatch) -> None:
    """Both feedback=pass AND validator approve → no write."""
    cb = failure_memory.make_failure_memory_after_callback()
    state = {
        "ticket_identifier": "ONE-1",
        "ticket_project": "TestRepo",
        "feedback_verdict": "pass",
        "validator_verdict": json.dumps({
            "verdict": "approve", "rationale": "looks good",
        }),
    }
    called = {"n": 0}
    monkeypatch.setattr(
        failure_memory, "record_failure",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
    )

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.state = state
    import asyncio
    asyncio.run(cb(callback_context=ctx))
    assert called["n"] == 0


def test_failure_memory_callback_writes_on_validator_reject(monkeypatch) -> None:
    """feedback=pass but validator request_changes → write."""
    cb = failure_memory.make_failure_memory_after_callback()
    # TERMINAL reject: replan budget already spent (replan_count==MAX_REPLANS)
    # so this Validator pass is the last one → record the failure.
    state = {
        "ticket_identifier": "ONE-2",
        "ticket_project": "TestRepo",
        "feedback_verdict": "pass",
        "replan_count": 1,
        "validator_verdict": json.dumps({
            "verdict": "request_changes",
            "rationale": "scope drift",
        }),
    }
    captured = {}

    def _rec(t, *, verdict, reason, review_verdict=None, **_kw):
        captured["verdict"] = verdict
        captured["reason"] = reason
        captured["review_verdict"] = review_verdict
        return {"ok": True}

    monkeypatch.setattr(failure_memory, "record_failure", _rec)

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.state = state
    import asyncio
    asyncio.run(cb(callback_context=ctx))
    assert captured.get("review_verdict") == "request_changes"
    assert "scope drift" in (captured.get("reason") or "")


def test_failure_memory_callback_skips_non_terminal_reject(monkeypatch) -> None:
    """A reject with replan budget remaining is NOT terminal — skip the
    write so the replanned attempt isn't pre-recorded as a failure."""
    cb = failure_memory.make_failure_memory_after_callback()
    state = {
        "ticket_identifier": "ONE-3",
        "ticket_project": "TestRepo",
        "feedback_verdict": "pass",
        "replan_count": 0,  # budget remains → validator_gate will replan
        "validator_verdict": json.dumps({"verdict": "request_changes"}),
    }
    called = {"n": 0}
    monkeypatch.setattr(
        failure_memory, "record_failure",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
    )

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.state = state
    import asyncio
    asyncio.run(cb(callback_context=ctx))
    assert called["n"] == 0
