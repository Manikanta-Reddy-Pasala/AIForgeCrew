"""Which agent path one chat turn takes, and the passes that run around it.

Every helper here sits between the user's message and an agent, and each was
written after a specific way a turn went wrong: a build spec contaminated by an
unrelated session, a pipeline run that produced no SPEC.md, a plan the UI never
saw because the terminal ``done`` arrived first, a "saved that" capture that
swallowed a real task riding along in the same sentence, and a dead Jira note
that stalled the whole turn while it was re-fetched.

So the rules under test are mostly about *failing open*: a probe that raises
routes on, a curation that times out is skipped silently, a capture that misses
still lets the agent run. The one thing that must not fail open is dropping
work — a classifier calling a message a pure capture never wins over the
deterministic actionable-intent backstop.
"""
from __future__ import annotations

import types as pytypes

import pytest

from aiforge_core.api.routes import chat as C


def _drain(gen):
    return list(gen)


# ─── stale note curation ───────────────────────────────────────────────


@pytest.fixture()
def curator(monkeypatch):
    from aiforge_core.runtime import note_curator as nc
    state: dict = {"stale": "/ctx/ONE-7.md", "result": None, "calls": []}
    monkeypatch.setattr(nc, "stale_note_path", lambda cwd: state["stale"])

    def _curate(path):
        state["calls"].append(path)
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]
    monkeypatch.setattr(nc, "curate_note", _curate)
    return state


def test_a_note_that_actually_drifted_is_reported(curator):
    curator["result"] = {"ok": True, "changes": ["status: open → done"]}
    evs = _drain(C._note_staleness_notice("/repo"))
    assert evs[0]["role"] == "curator"
    assert "ONE-7.md" in evs[0]["text"] and "status: open → done" in evs[0]["text"]


def test_a_silent_freshness_bump_adds_no_chat_noise(curator):
    curator["result"] = {"ok": True, "changes": []}
    assert _drain(C._note_staleness_notice("/repo")) == []


def test_a_fresh_note_is_never_re_fetched(curator):
    curator["stale"] = None
    assert _drain(C._note_staleness_notice("/repo")) == []
    assert curator["calls"] == []


def test_a_dead_source_cannot_stall_the_turn(curator):
    """The curation re-fetches over the network, so it is hard time-boxed."""
    curator["result"] = RuntimeError("jira unreachable")
    assert _drain(C._note_staleness_notice("/repo")) == []


def test_the_whole_pass_fails_open(monkeypatch):
    from aiforge_core.runtime import note_curator as nc
    monkeypatch.setattr(nc, "stale_note_path",
                        lambda cwd: (_ for _ in ()).throw(OSError("gone")))
    assert _drain(C._note_staleness_notice("/repo")) == []


# ─── the 3-agent pipeline route ────────────────────────────────────────


@pytest.fixture()
def pp(monkeypatch):
    """The pipeline module, stubbed: enhancer → architect → planner."""
    state: dict = {"files": ["a.py", "b.py"], "subs": [{"slug": "s1"},
                                                       {"slug": "s2"}],
                   "spec_md": "# SPEC", "team_events": [{"type": "step"}],
                   "bon_events": [{"type": "step", "text": "bon"}],
                   "seen": {}}

    def _enhance(prompt, history=None, cwd=None, repo=None):
        state["seen"]["repo"] = repo
        return f"spec({prompt})"

    def _stream_team(spec, cwd=None, subtasks=None, enhanced=None,
                     session_id=None):
        state["seen"]["team_spec"] = spec
        state["seen"]["team_subs"] = subtasks
        yield from state["team_events"]

    ns = pytypes.SimpleNamespace(
        _enhance=_enhance,
        _architect=lambda spec, cwd=None: state["files"],
        _plan_files=lambda files: state["subs"],
        _decompose=lambda spec: state["subs"],
        _render_spec_md=lambda spec, subs: state["spec_md"],
        stream_parallel_team=_stream_team,
        enabled=lambda: True,
        _is_greenfield=lambda cwd: False)
    state["ns"] = ns
    return state


def _pipeline(pp, cwd, **kw):
    return _drain(C._pipeline_route(pp["ns"], "build a lang", cwd, 1, [],
                                    lambda s: s, kw.pop("path", {}), 0.0, {}))


def test_a_spec_that_splits_runs_the_parallel_team(pp, tmp_path):
    path: dict = {}
    evs = _drain(C._pipeline_route(pp["ns"], "build", str(tmp_path), 1, [],
                                   lambda s: s, path, 0.0, {}))
    assert path["parallel"] is True
    assert evs[-1] == {"type": "done"}, "a UI waiting on done must not hang"
    assert pp["seen"]["team_subs"] == pp["subs"]


def test_the_enhancers_recall_is_scoped_to_this_sessions_repo(pp, tmp_path):
    """Unscoped, an unrelated prior session bleeds into the build spec — a
    'mathx' build once decomposed into game/storage."""
    from aiforge_core.runtime import chat_agent as ca
    _pipeline(pp, str(tmp_path))
    assert pp["seen"]["repo"] == ca._chat_repo_key(str(tmp_path))


def test_a_single_file_design_is_decomposed_instead_of_planned(pp, tmp_path):
    pp["files"] = ["only.py"]
    pp["subs"] = [{"slug": "one"}]
    _pipeline(pp, str(tmp_path))
    assert "team_subs" not in pp["seen"], "one subtask is not a parallel run"


def test_a_single_task_run_still_writes_a_spec(pp, tmp_path):
    """Every pipeline-routed run tracks against a SPEC.md — the <2-subtask
    fallbacks used to run spec-less."""
    pp["subs"] = [{"slug": "one"}]
    evs = _pipeline(pp, str(tmp_path))
    assert (tmp_path / "SPEC.md").read_text() == "# SPEC"
    assert "Wrote SPEC.md" in evs[0]["text"]


def test_a_failed_spec_write_is_reported_not_raised(pp, tmp_path):
    pp["subs"] = [{"slug": "one"}]
    evs = _pipeline(pp, str(tmp_path / "does-not-exist"))
    assert "SPEC.md write failed" in evs[0]["text"]


def test_best_of_n_is_opt_in(pp, tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_BEST_OF_N", raising=False)
    pp["subs"] = [{"slug": "one"}]
    evs = _pipeline(pp, str(tmp_path))
    assert not any(e.get("text") == "bon" for e in evs)
    assert evs[-1]["type"] != "done", "the sequential fallback carries on"


def test_best_of_n_marks_the_run_unsteerable(pp, tmp_path, monkeypatch):
    """Nothing in best_of_n drains the steer queue, so /steer must report
    unsupported instead of silently dropping the message at end of turn."""
    from aiforge_core.runtime import best_of_n as bon
    from aiforge_core.runtime import chat_interject
    monkeypatch.setenv("AIFORGE_BEST_OF_N", "3")
    seen: dict = {}
    monkeypatch.setattr(chat_interject, "set_steerable",
                        lambda sid, val: seen.update(sid=sid, val=val))
    monkeypatch.setattr(bon, "stream_best_of_n",
                        lambda spec, cwd, session_id=None:
                        iter(pp["bon_events"]))
    pp["subs"] = [{"slug": "one"}]
    path: dict = {}
    evs = _drain(C._pipeline_route(pp["ns"], "build", str(tmp_path), 9, [],
                                   lambda s: s, path, 0.0, {}))
    assert seen == {"sid": 9, "val": False}
    assert path["parallel"] is True and evs[-1] == {"type": "done"}


# ─── the doc / analysis route ──────────────────────────────────────────


@pytest.fixture()
def analysis(monkeypatch):
    from aiforge_core.runtime import analysis_pipeline as ap
    state: dict = {"fan": (False, [], []), "plan": (False, [], []), "seen": {}}

    def _fan_out(prompt, cwd):
        if isinstance(state["fan"], Exception):
            raise state["fan"]
        return state["fan"]

    def _plan(prompt, cwd):
        if isinstance(state["plan"], Exception):
            raise state["plan"]
        return state["plan"]

    def _team(prompt, cwd=None, session_id=None, repos=None, topics=None):
        state["seen"].update(repos=repos, topics=topics)
        yield {"type": "step", "text": "fan"}

    def _planned(prompt, cwd=None, session_id=None, groups=None, topics=None):
        state["seen"].update(groups=groups)
        yield {"type": "step", "text": "planned"}
    monkeypatch.setattr(ap, "should_fan_out", _fan_out)
    monkeypatch.setattr(ap, "plan_single_repo", _plan)
    monkeypatch.setattr(ap, "stream_analysis_team", _team)
    monkeypatch.setattr(ap, "stream_analysis_planned", _planned)
    return state


def _doc(pctx=None):
    return _drain(C._doc_task_route("analyse the repos", "/repo", 1,
                                    lambda s: s, pctx if pctx is not None
                                    else {}))


def test_a_multi_repo_analysis_fans_out_one_agent_per_repo(analysis):
    analysis["fan"] = (True, ["/a", "/b"], ["auth"])
    pctx: dict = {}
    evs = _doc(pctx)
    assert evs == [{"type": "step", "text": "fan"}] and pctx["done"] is True
    assert analysis["seen"]["repos"] == ["/a", "/b"]


def test_one_repo_naming_many_files_is_planned_into_bounded_groups(analysis):
    """A flat many-file sweep is exactly what a local model cannot track."""
    analysis["plan"] = (True, [{"files": ["a", "b"]}, {"files": ["c"]}], [])
    pctx: dict = {}
    evs = _doc(pctx)
    assert "3 files" in evs[0]["text"] and "2 bounded" in evs[0]["text"]
    assert evs[-1] == {"type": "step", "text": "planned"}
    assert pctx["done"] is True


def test_anything_else_goes_to_the_single_research_agent(analysis):
    pctx: dict = {}
    evs = _doc(pctx)
    assert "single research agent" in evs[0]["text"] and "done" not in pctx


def test_a_probe_that_blows_up_never_breaks_routing(analysis):
    analysis["fan"] = RuntimeError("x")
    analysis["plan"] = RuntimeError("y")
    assert "single research agent" in _doc()[0]["text"]


# ─── skipping the enhancer ─────────────────────────────────────────────


def _skip(**kw):
    args = {"auto_downgraded": False, "route_pipeline": False,
            "is_build_task": False, "history": [{"role": "user"}],
            "prompt": "and now?"}
    args.update(kw)
    return C._should_skip_enhance(**args)


@pytest.fixture()
def router(monkeypatch):
    from aiforge_core.runtime import turn_router as tr
    state: dict = {"followup": True, "cls": "simple"}
    monkeypatch.setattr(tr, "is_followup", lambda h: state["followup"])

    def _classify(prompt, history=None):
        if isinstance(state["cls"], Exception):
            raise state["cls"]
        return state["cls"]
    monkeypatch.setattr(tr, "classify", _classify)
    return state


def test_a_simple_follow_up_skips_the_second_round_trip(router):
    assert _skip() is True


def test_a_complex_follow_up_still_gets_enhanced(router):
    router["cls"] = "complex"
    assert _skip() is False


def test_the_first_turn_always_gets_the_enhancer(router):
    """Fresh context, referents still to resolve."""
    router["followup"] = False
    assert _skip() is False


def test_a_build_task_is_never_under_enhanced(router):
    assert _skip(is_build_task=True) is False


def test_a_classify_failure_keeps_the_enhancer(router):
    router["cls"] = RuntimeError("model down")
    assert _skip() is False


def test_a_pipeline_route_answers_from_its_own_flags(router):
    assert _skip(route_pipeline=True, auto_downgraded=True) is False
    assert _skip(route_pipeline=False, auto_downgraded=True) is True


def test_a_broken_router_module_never_blocks_the_turn(monkeypatch):
    from aiforge_core.runtime import turn_router as tr
    monkeypatch.setattr(tr, "is_followup",
                        lambda h: (_ for _ in ()).throw(RuntimeError("x")))
    assert _skip() is False


# ─── plan mode ─────────────────────────────────────────────────────────


@pytest.fixture()
def plan_agent(monkeypatch):
    import aiforge_core.runtime.chat_agent as ca
    state: dict = {"events": [{"type": "step"}, {"type": "done"}], "kw": {}}

    def _run(history, **kw):
        state["kw"] = kw
        yield from state["events"]
    monkeypatch.setattr(ca, "run_chat_agent", _run)
    return state


def _plan_mode(pp, quick=False):
    return _drain(C._plan_mode_route(pp["ns"], "the spec", [], "/repo",
                                     "architect", 1, quick))


def test_the_plan_reaches_the_ui_before_the_turn_ends(pp, plan_agent):
    """plan_ready after the terminal done would show a finished turn with no
    plan, so the done is held and released last."""
    evs = _plan_mode(pp)
    assert [e["type"] for e in evs[-2:]] == ["plan_ready", "done"]
    assert evs[-2]["spec"] == "the spec"


def test_the_planned_subtasks_are_never_rendered_as_pending(pp, plan_agent):
    """Plan mode shows a static plan it never executes."""
    evs = _plan_mode(pp)
    items = evs[0]["items"]
    assert [i["status"] for i in items] == ["planned", "planned"]
    assert items[0]["slug"] == "s1"


def test_a_subtask_without_a_slug_still_gets_one(pp, plan_agent):
    pp["subs"] = [{"goal": "do it"}]
    assert _plan_mode(pp)[0]["items"][0]["slug"] == "sub-1"


def test_a_plan_that_will_not_decompose_emits_no_panel(pp, plan_agent):
    pp["subs"] = []
    assert [e["type"] for e in _plan_mode(pp)] == ["step", "plan_ready", "done"]


def test_an_agent_that_never_finishes_still_yields_the_plan(pp, plan_agent):
    plan_agent["events"] = [{"type": "step"}]
    assert _plan_mode(pp)[-1]["type"] == "plan_ready"


def test_quick_mode_caps_the_plan_agents_steps(pp, plan_agent, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_QUICK_STEPS", "3")
    _plan_mode(pp, quick=True)
    assert plan_agent["kw"]["max_steps"] == 3 and plan_agent["kw"]["mode"] == "plan"


def test_a_normal_turn_runs_the_open_loop(pp, plan_agent):
    _plan_mode(pp)
    assert plan_agent["kw"]["max_steps"] is None


# ─── gathering the routing inputs ──────────────────────────────────────


@pytest.fixture()
def decide(monkeypatch):
    from aiforge_core.runtime import chat_router as cr
    seen: dict = {}
    monkeypatch.setattr(cr, "decide",
                        lambda prompt, **kw: seen.update(prompt=prompt, **kw)
                        or "decision")
    return seen


def _decide(pp, **kw):
    args = {"agent_mode": "chat", "team": False, "parallel_team": False,
            "cwd": "/repo", "history": []}
    args.update(kw)
    return C._decide_chat_route(pp["ns"], "build it", args["agent_mode"],
                                args["team"], args["parallel_team"],
                                args["cwd"], args["history"])


def test_the_gathered_inputs_reach_the_pure_router(pp, decide, monkeypatch):
    from aiforge_core.runtime import task_router as tr
    from aiforge_core.runtime import turn_router as tr2
    monkeypatch.setattr(tr2, "is_followup", lambda h: False)
    monkeypatch.setattr(tr, "classify_task",
                        lambda p, history=None, cwd=None: "code_build")
    assert _decide(pp) == "decision"
    assert decide["psub_on"] is True and decide["greenfield"] is False
    assert decide["fresh"] is True and decide["cat"] == "code_build"


def test_a_follow_up_is_not_re_classified(pp, decide, monkeypatch):
    from aiforge_core.runtime import task_router as tr
    from aiforge_core.runtime import turn_router as tr2
    monkeypatch.setattr(tr2, "is_followup", lambda h: True)
    monkeypatch.setattr(tr, "classify_task",
                        lambda *a, **k: pytest.fail("classified a follow-up"))
    _decide(pp)
    assert decide["fresh"] is False and decide["cat"] is None


def test_a_dead_classifier_routes_on_without_a_class(pp, decide, monkeypatch):
    from aiforge_core.runtime import task_router as tr
    from aiforge_core.runtime import turn_router as tr2
    monkeypatch.setattr(tr2, "is_followup", lambda h: False)
    monkeypatch.setattr(tr, "classify_task",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    _decide(pp)
    assert decide["cat"] is None


def test_each_probe_fails_safe(pp, decide, monkeypatch):
    """A broken parallel/greenfield probe must not decide the route by
    accident."""
    pp["ns"].enabled = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    pp["ns"]._is_greenfield = lambda cwd: (_ for _ in ()).throw(OSError("x"))
    monkeypatch.setattr("aiforge_core.runtime.turn_router.is_followup",
                        lambda h: (_ for _ in ()).throw(RuntimeError("x")))
    _decide(pp, parallel_team=True)
    assert decide["psub_on"] is True   # falls back to the explicit pick
    assert decide["greenfield"] is True and decide["fresh"] is True


def test_pipeline_approvals_force_the_gated_sequential_path(pp, decide,
                                                            monkeypatch):
    from aiforge_core.config import approval_settings as aps
    monkeypatch.setattr(aps, "required", lambda kind: True)
    _decide(pp, team=True)
    assert decide["team_approvals"] is True


def test_an_unreadable_approval_setting_fails_safe_to_gated(pp, decide,
                                                            monkeypatch):
    from aiforge_core.config import approval_settings as aps
    monkeypatch.setattr(aps, "required",
                        lambda kind: (_ for _ in ()).throw(OSError("x")))
    _decide(pp, team=True)
    assert decide["team_approvals"] is True


@pytest.mark.parametrize("val,expected", [("0", False), ("false", False),
                                          ("1", True), (None, True)])
def test_auto_escalation_can_be_switched_off(pp, decide, monkeypatch, val,
                                             expected):
    if val is None:
        monkeypatch.delenv("AIFORGE_AUTO_ESCALATE", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_AUTO_ESCALATE", val)
    _decide(pp)
    assert decide["auto_escalate"] is expected


# ─── rule / memory capture ─────────────────────────────────────────────


@pytest.fixture()
def capture(monkeypatch):
    from aiforge_core.runtime import rule_capture as rc
    state: dict = {
        "should": True,
        "cls": {"category": "preference", "scope": "global",
                "canonical": "always run tests", "task_present": False},
        "stored": {"id": 12}, "intent": None, "actionable": False}

    def _classify(prompt, repo=None, session_id=None):
        if isinstance(state["cls"], Exception):
            raise state["cls"]
        return state["cls"]
    monkeypatch.setattr(rc, "repo_key", lambda cwd: "my-repo")
    monkeypatch.setattr(rc, "should_classify", lambda p: state["should"])
    monkeypatch.setattr(rc, "classify", _classify)
    monkeypatch.setattr(rc, "store",
                        lambda c, **kw: state["stored"])
    monkeypatch.setattr(rc, "recognize_gate_intent", lambda c: state["intent"])
    monkeypatch.setattr(rc, "looks_actionable", lambda p: state["actionable"])
    return state


def _capture(prompt="always run tests", pctx=None):
    return _drain(C._rule_capture_pass(prompt, "/repo", 1,
                                       pctx if pctx is not None else {}))


def test_a_directive_stated_in_passing_is_captured(capture):
    evs = _capture()
    assert evs[0] == {"type": "captured", "id": 12, "category": "preference",
                      "scope": "global", "text": "always run tests",
                      "repo": "my-repo"}


def test_a_pure_capture_acks_and_skips_the_agent(capture):
    evs = _capture()
    assert "saved as preference (global)" in evs[1]["text"]
    assert evs[-1] == {"type": "done"}


def test_a_task_riding_along_with_the_capture_still_runs(capture):
    """Never drop real work on the classifier's say-so."""
    capture["cls"] = {**capture["cls"], "task_present": True}
    evs = _capture()
    assert [e["type"] for e in evs] == ["captured"]


def test_the_deterministic_backstop_beats_a_wrong_classification(capture):
    capture["actionable"] = True
    assert [e["type"] for e in _capture("...and now fix the bug")] == ["captured"]


def test_a_recognised_gate_intent_rides_on_the_event(capture):
    capture["intent"] = "approvals_off"
    assert _capture()[0]["gate_intent"] == "approvals_off"


def test_an_ordinary_turn_never_spends_a_classify(capture):
    """No preference cue in the message → no per-turn LLM cost."""
    capture["should"] = False
    assert _capture("fix the bug") == []


def test_the_none_category_stores_nothing(capture):
    capture["cls"] = {"category": "none"}
    assert _capture() == []


def test_a_classify_crash_leaves_the_turn_alone(capture):
    capture["cls"] = RuntimeError("model down")
    assert _capture() == []


def test_a_broken_capture_module_fails_open(monkeypatch):
    from aiforge_core.runtime import rule_capture as rc
    monkeypatch.setattr(rc, "repo_key",
                        lambda cwd: (_ for _ in ()).throw(RuntimeError("x")))
    assert _capture() == []


def test_a_repo_less_session_still_captures(capture, monkeypatch):
    from aiforge_core.runtime import rule_capture as rc
    monkeypatch.setattr(rc, "repo_key", lambda cwd: None)
    assert _capture()[0]["repo"] == "repo"


def test_the_capture_pass_is_hard_wall_clock_bounded(monkeypatch):
    """A degraded LLM must never stall the turn."""
    import time
    monkeypatch.setenv("AIFORGE_CAPTURE_BUDGET_S", "0.05")
    rc = pytypes.SimpleNamespace(
        classify=lambda p, repo=None, session_id=None: time.sleep(5),
        store=lambda *a, **k: {}, recognize_gate_intent=lambda c: None)
    assert C._run_capture_pass(rc, "p", "repo", "/cwd", 1) is None


def test_a_successful_pass_returns_the_class_store_and_intent():
    rc = pytypes.SimpleNamespace(
        classify=lambda p, repo=None, session_id=None: {"category": "fact"},
        store=lambda c, **kw: {"id": 3},
        recognize_gate_intent=lambda c: "gate")
    assert C._run_capture_pass(rc, "p", "repo", "/cwd", 1) == (
        {"category": "fact"}, {"id": 3}, "gate")
