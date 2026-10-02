"""Model calls a deterministic fact makes pointless are skipped: the second plan
verification after the replan, the validator after a green pass, the refiner
after the first iteration — and a Doer loop with no edits counts as stalled."""
from types import SimpleNamespace

from aiforge_core.agents import _base, refiner, validator, verifier
from aiforge_core.runtime.graph_pipeline import _gates as G
from aiforge_core.runtime.graph_pipeline._parsers import _parse_verdict


def test_first_verification_still_runs():
    assert verifier._reverify_decide({}) is None
    assert verifier._reverify_decide({"verify_replan_count": 0}) is None


def test_second_verification_is_skipped_and_says_why():
    v = verifier._reverify_decide({"verify_replan_count": 1})
    assert v["skipped"] is True and _parse_verdict(v) == "pass"


def test_skip_helper_writes_the_verdict_and_otherwise_runs_the_stage_callback():
    calls = []
    agent = SimpleNamespace(output_key="k",
                            before_agent_callback=lambda **kw: calls.append(1))
    _base.skip_agent_when(agent, lambda st: {"verdict": "pass"}
                          if st.get("skip") else None)
    ctx = SimpleNamespace(state={})
    agent.before_agent_callback(callback_context=ctx)
    assert calls == [1] and "k" not in ctx.state
    ctx = SimpleNamespace(state={"skip": True})
    assert agent.before_agent_callback(callback_context=ctx) is not None
    assert ctx.state["k"] == {"verdict": "pass"} and calls == [1]


def _green(**kw):
    return {"feedback_verdict": "pass", "tests_ok": True, **kw}


def test_validator_is_skipped_only_on_a_clean_green_pass(monkeypatch):
    monkeypatch.delenv("AIFORGE_VALIDATE_ON_GREEN", raising=False)
    v = validator._green_decide(_green())
    assert v["skipped"] and _parse_verdict(v) == "approve"
    for bad in ({"tests_ok": None}, {"tests_ok": False}, {"lint_ok": False},
                {"typecheck_ok": False}, {"doer_incomplete": True},
                {"quality_issue": "test_gaming"}, {"feedback_verdict": "fail"}):
        assert validator._green_decide(_green(**bad)) is None, bad


def test_validator_can_be_forced_to_run(monkeypatch):
    monkeypatch.setenv("AIFORGE_VALIDATE_ON_GREEN", "1")
    assert validator._green_decide(_green()) is None


def test_refiner_runs_once_then_is_skipped(monkeypatch):
    monkeypatch.delenv("AIFORGE_REFINE_EVERY_ITER", raising=False)
    assert refiner._already_polished({"doer_iters": 0}) is None
    assert refiner._already_polished({"doer_iters": 1})["refiner_skipped"] is True
    monkeypatch.setenv("AIFORGE_REFINE_EVERY_ITER", "1")
    assert refiner._already_polished({"doer_iters": 3}) is None


def test_two_iterations_without_an_edit_mark_the_loop_stalled():
    st = {"_iter_edits": 0}
    G._no_edit_stop(st)
    assert "loop_budget_kill" not in st
    G._no_edit_stop(st)
    assert st["loop_budget_kill"] is True and st["loop_budget_reason"] == "no_edits"


def test_an_edit_resets_the_idle_count_and_unknown_is_ignored():
    st = {"_iter_edits": 0}
    G._no_edit_stop(st)
    st["_iter_edits"] = 3
    G._no_edit_stop(st)
    assert st["_idle_iters"] == 0
    st2 = {}
    G._no_edit_stop(st2)
    assert st2 == {}
