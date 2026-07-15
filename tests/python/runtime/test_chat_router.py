"""chat_router.decide — the routing brain, now a pure function (was untestable
nested closures inside the api streaming handler)."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_router as cr


# ── predicates ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("p", [
    "how do I build a CLI?", "what is the latest?", "should I use postgres",
    "explain the pipeline", "can you add tests?"])
def test_advice_question_true(p):
    assert cr.is_advice_question(p)


@pytest.mark.parametrize("p", [
    "build a REST API with tests", "create a todo app", "add a delete method"])
def test_advice_question_false(p):
    assert not cr.is_advice_question(p)


def test_regex_build_fallback():
    assert cr.regex_build_fallback("build a python cli tool with tests")
    assert cr.regex_build_fallback("create a rest api service with endpoints")
    # advice question → not a build
    assert not cr.regex_build_fallback("how do I build an api?")
    # tracker action → not a build even with a code noun
    assert not cr.regex_build_fallback("create 2 jira tickets about the api")
    # too short / no build signal
    assert not cr.regex_build_fallback("fix it")
    assert not cr.regex_build_fallback("what's the weather")


# ── decide: classifier-driven ─────────────────────────────────────────────
def _d(prompt="build a cli app with tests", **kw):
    base = dict(agent_mode="act", team=False, psub_on=True, greenfield=True,
                fresh=True, cat=None, team_approvals=False)
    base.update(kw)
    return cr.decide(prompt, **base)


def test_code_build_simple_escalates():
    r = _d(cat="code_build")
    assert r.is_build_task and r.build_escalate and r.route_pipeline
    assert "Multi-file build detected" in (r.notice or "")


def test_doc_analysis_routes_to_research():
    r = _d(prompt="analyze the repo and write a report", cat="doc_analysis")
    assert r.doc_task and not r.build_escalate and not r.route_pipeline


def test_chat_class_no_escalation():
    r = _d(prompt="what does this function do?", cat="chat")
    assert not r.is_build_task and not r.build_escalate and not r.route_pipeline
    assert r.notice is None


# ── C: plan mode never routes to research ─────────────────────────────────
def test_plan_mode_ignores_doc_class():
    r = _d(prompt="review the architecture", cat="doc_analysis",
           agent_mode="plan")
    assert not r.doc_task              # plan owns its own analysis
    assert not r.build_escalate        # plan never escalates


# ── A: explicit team + build never downgraded on a doc misclass ───────────
def test_team_build_not_downgraded_by_doc_misclass():
    r = _d(prompt="build an auth module with tests", cat="doc_analysis",
           team=True)
    assert not r.doc_task              # rescued — it's clearly a build
    assert r.route_pipeline


def test_team_real_doc_stays_doc():
    r = _d(prompt="write a report on our options", cat="doc_analysis", team=True)
    assert r.doc_task                  # no code noun → genuine doc


# ── F: fresh explicit team always pipelines; follow-up doesn't ────────────
def test_fresh_team_pipelines_even_non_greenfield():
    r = _d(prompt="add a new billing subsystem", cat="code_edit", team=True,
           greenfield=False, fresh=True)
    assert r.route_pipeline            # fresh team → pipeline regardless of class


def test_team_followup_edit_is_sequential():
    r = _d(prompt="tweak the naming", cat=None, team=True, greenfield=False,
           fresh=False)
    assert not r.route_pipeline        # follow-up → sequential/in-place
    assert "sequential in-place" in (r.notice or "")


# ── J: team approvals ON → not the parallel path ──────────────────────────
def test_team_approvals_force_sequential():
    r = _d(cat="code_build", team=True, team_approvals=True)
    assert not r.route_pipeline        # approvals ON → gated sequential
    assert "approvals ON" in (r.notice or "")


def test_team_approvals_off_uses_parallel():
    r = _d(cat="code_build", team=True, team_approvals=False)
    assert r.route_pipeline


# ── fallback + safety ─────────────────────────────────────────────────────
def test_none_cat_uses_regex_fallback():
    r = _d(prompt="build a flask api with tests", cat=None)
    assert r.is_build_task and r.build_escalate


def test_question_never_escalates_even_if_classed_build():
    r = _d(prompt="how would I build a REST API with tests?", cat="code_build")
    assert not r.build_escalate        # advice veto wins over the class


def test_auto_escalate_off():
    r = _d(cat="code_build", auto_escalate=False)
    assert not r.build_escalate
