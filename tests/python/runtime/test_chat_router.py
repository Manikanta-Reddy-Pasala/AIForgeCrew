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
    assert r.is_build_task
    assert r.build_escalate
    assert r.route_pipeline
    assert "Multi-file build detected" in (r.notice or "")


def test_doc_analysis_routes_to_research():
    r = _d(prompt="analyze the repo and write a report", cat="doc_analysis")
    assert r.doc_task
    assert not r.build_escalate
    assert not r.route_pipeline


def test_chat_class_no_escalation():
    r = _d(prompt="what does this function do?", cat="chat")
    assert not r.is_build_task
    assert not r.build_escalate
    assert not r.route_pipeline
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
    assert r.is_build_task
    assert r.build_escalate


def test_question_never_escalates_even_if_classed_build():
    r = _d(prompt="how would I build a REST API with tests?", cat="code_build")
    assert not r.build_escalate        # advice veto wins over the class


def test_auto_escalate_off():
    r = _d(cat="code_build", auto_escalate=False)
    assert not r.build_escalate


# ── trivial file/shell chores stay on the single agent ───────────────────
_TRIVIAL = [
    "Create three empty files a.txt b.txt c.txt, then run ls -1 and reply "
    "with ONLY the number of entries.",
    "Create hello.py that prints hi, run it with python3, and reply with ONLY "
    "its output.",
]
_REAL_BUILDS = [
    "Build a Python CLI task manager with storage, cli and tests",
    "build a flask api with tests",
    "Create main.py, models.py, views.py and utils.py for a todo manager",
]


@pytest.mark.parametrize("p", _TRIVIAL)
@pytest.mark.parametrize("cat", ["code_build", None])
def test_trivial_chore_not_escalated(p, cat):
    r = _d(prompt=p, cat=cat)
    assert cr.is_small_task(p)
    assert not r.is_build_task
    assert not r.build_escalate
    assert not r.route_pipeline
    assert r.notice is None


@pytest.mark.parametrize("p", _REAL_BUILDS)
def test_real_build_still_escalates(p):
    assert not cr.is_small_task(p)
    r = _d(prompt=p, cat="code_build")
    assert r.is_build_task
    assert r.build_escalate
    assert r.route_pipeline


def test_explicit_team_keeps_trivial_as_build():
    # the small-task guard is for simple-mode auto-escalation only
    r = _d(prompt=_TRIVIAL[1], cat="code_build", team=True)
    assert r.is_build_task
    assert r.route_pipeline


def test_version_number_is_not_a_named_file():
    # "3.11" is not a file, and "run" alone no longer makes a task small
    assert not cr.is_small_task("create notes for python 3.11 and run it")


@pytest.mark.parametrize("p", [
    "Create a snake game in pygame and run it",
    "Implement a markdown to HTML converter and run it on README.md",
    "Generate a Go web scraper for HN and run it",
    "Create five python files for an inventory manager and run them",
    "Create a.txt b.txt c.txt d.txt and run ls",           # 4 files > 3
    "Create 4 files a.txt b.txt c.txt and run ls",
    "Create a chat app with socket.io and next.js and run it",
    "Create Dockerfile, Makefile, go.mod and main.go",     # 4 files
    "Create hello.py with tests and run them",
    "Create main.py and a test file and run it",
    "Create hello.py that prints hi and run it. " + "x " * 100,  # too long
])
def test_real_builds_are_not_small(p):
    assert not cr.is_small_task(p)
    r = _d(prompt=p, cat="code_build")
    assert r.build_escalate


@pytest.mark.parametrize("p", [
    "Create app.py that prints hi and run it",
    "Create server.js that prints hi and run it with node",
    "Create cli.py that prints its args and run it",
    "Create bot.py that prints hello",
    "Create test.py that prints the latest date and run it",
    "Create server.js using node.js that prints hi",       # node.js not a file
    "Create a.txt b.txt c.txt and run ls",                 # exactly 3 files
    "Create a Dockerfile and a Makefile and run make",
    "Create an empty build.gradle and app.properties",
])
def test_trivial_chores_are_small(p):
    assert cr.is_small_task(p)
    r = _d(prompt=p, cat="code_build")
    assert not r.build_escalate
    assert not r.route_pipeline


def test_plan_mode_small_prompt_unchanged():
    r = _d(prompt=_TRIVIAL[1], cat="code_build", agent_mode="plan")
    assert r.is_build_task             # plan mode keeps the classifier's view
    assert not r.build_escalate        # and plan never escalates anyway
    assert not r.route_pipeline



@pytest.mark.parametrize("prompt", [
    "Implement OAuth login in auth.py and routes.py",
    "Implement rate limiting in middleware.py and settings.py",
    "Create a Kafka consumer in consumer.py that writes to Postgres",
    "Refactor utils.py into helpers.py and format.py",
])
def test_feature_work_naming_a_few_files_is_not_a_chore(prompt):
    """Only chores (run / print / empty / touch / ls) skip the pipeline's plan
    and review; feature work that names a file or two keeps them."""
    from aiforge_core.runtime.chat_router import is_small_task
    assert is_small_task(prompt) is False
