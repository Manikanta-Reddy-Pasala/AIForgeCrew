"""A small request in the team pipeline: triage's own file estimate routes it
straight to the Planner, a plan of one small step skips the plan critic and
the polish, and the Doer's history is cut at a fixed point so consecutive
model requests share their beginning (the server's prompt cache)."""
from types import SimpleNamespace

import pytest
from google.genai import types as gtypes

from aiforge_core.agents import refiner, verifier
from aiforge_core.runtime import executor_focus as ef
from aiforge_core.runtime import graph_pipeline as gp
from aiforge_core.runtime.adk_runner import _context as ctxmod
from aiforge_core.runtime.graph_pipeline import _gates as G
from aiforge_core.runtime.graph_pipeline import _parsers as P

_ENVS = ("AIFORGE_SMALL_ROUTE", "AIFORGE_SMALL_MAX_FILES",
         "AIFORGE_SMALL_PLAN_SKIP", "AIFORGE_FORCE_FULL_PIPELINE",
         "AIFORGE_TRIAGE_STRICT", "AIFORGE_EXECUTOR_FOCUS",
         "AIFORGE_EXECUTOR_TAIL", "AIFORGE_EXECUTOR_PROLOGUE",
         "AIFORGE_CONTEXT_TRIM_STEP", "AIFORGE_REFINE_EVERY_ITER",
         "AIFORGE_SMALL_REDO")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENVS:
        monkeypatch.delenv(k, raising=False)


def _route(state):
    ctx = SimpleNamespace(state=state, route=None)
    G._triage_gate(ctx)
    return ctx.route


def _verdict(complexity, files):
    return {"triage_verdict": {"complexity": complexity,
                               "estimated_files": files}}


# ── the small route ────────────────────────────────────────────────────

def test_triage_sized_at_two_files_goes_straight_to_the_planner():
    assert _route(_verdict("moderate", 2)) == gp.ROUTE_SMALL
    assert _route(_verdict("moderate", 1)) == gp.ROUTE_SMALL


def test_the_estimate_is_read_from_the_fenced_json_a_local_model_writes():
    raw = ('```json\n{"complexity": "moderate", "estimated_files": 2, '
           '"rationale": "two files"}\n```')
    assert P._read_estimated_files({"triage_verdict": raw}) == 2
    assert _route({"triage_verdict": raw}) == gp.ROUTE_SMALL


def test_more_files_a_hard_verdict_or_no_estimate_take_the_full_path():
    assert _route(_verdict("moderate", 3)) == gp.ROUTE_FULL
    assert _route(_verdict("hard", 2)) == gp.ROUTE_FULL
    assert _route({"triage_verdict": {"complexity": "moderate"}}) == gp.ROUTE_FULL
    assert _route({"triage_verdict": "moderate"}) == gp.ROUTE_FULL
    assert _route({"complexity": "moderate"}) == gp.ROUTE_FULL
    assert _route(_verdict("moderate", "many")) == gp.ROUTE_FULL


def test_trivial_still_goes_to_the_doer():
    assert _route(_verdict("trivial", 2)) == gp.ROUTE_TRIVIAL


def test_the_small_route_can_be_switched_off_and_resized(monkeypatch):
    monkeypatch.setenv("AIFORGE_SMALL_ROUTE", "0")
    assert _route(_verdict("moderate", 2)) == gp.ROUTE_FULL
    monkeypatch.delenv("AIFORGE_SMALL_ROUTE")
    monkeypatch.setenv("AIFORGE_SMALL_MAX_FILES", "4")
    assert _route(_verdict("moderate", 4)) == gp.ROUTE_SMALL
    monkeypatch.setenv("AIFORGE_SMALL_MAX_FILES", "0")
    assert _route(_verdict("moderate", 1)) == gp.ROUTE_FULL


def test_forcing_the_full_pipeline_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_FORCE_FULL_PIPELINE", "1")
    assert _route(_verdict("moderate", 2)) == gp.ROUTE_FULL


def test_the_graph_has_an_edge_for_the_small_route():
    from aiforge_core.runtime._pipeline_graph import _entry_edges

    def edge(**kw):
        return kw
    nodes = {k: k for k in ("triage", "triage_gate", "doer", "planner",
                            "enhancer")}
    edges = _entry_edges(edge, "START", nodes)
    assert {"from_node": "triage_gate", "to_node": "planner",
            "route": gp.ROUTE_SMALL} in edges
    assert {"from_node": "triage_gate", "to_node": "enhancer",
            "route": gp.ROUTE_FULL} in edges


def test_the_small_route_runs_plan_do_check_without_the_context_stages():
    import asyncio

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START, Edge, Workflow, node

    order: list = []

    def stub(name, sets=None):
        async def _fn(ctx):
            order.append(name)
            for k, v in (sets or {}).items():
                ctx.state[k] = v
        return node(_fn, name=name)

    plan = ('{"plan_md": "add f", "scope_allowlist_globs": ["a.py"], '
            '"child_subtickets": []}')
    nodes = {
        "triage": stub("triage", _verdict("moderate", 2)),
        "triage_gate": gp.make_triage_gate(),
        "enhancer": stub("enhancer"),
        "planner": stub("planner", {"plan_md": plan}),
        "plan_promote": gp.make_plan_promote(),
        "doer": stub("doer"),
    }
    from aiforge_core.runtime._pipeline_graph import _entry_edges
    edges = _entry_edges(Edge, START, nodes) + [
        Edge(from_node=nodes["planner"], to_node=nodes["plan_promote"]),
        Edge(from_node=nodes["plan_promote"], to_node=nodes["doer"])]

    async def _go():
        svc = InMemorySessionService()
        r = Runner(agent=Workflow(name="stub", edges=edges), app_name="t",
                   session_service=svc, auto_create_session=True)
        s = await svc.create_session(app_name="t", user_id="u")
        c = gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="go")])
        async for _ in r.run_async(user_id="u", session_id=s.id, new_message=c):
            pass
        s2 = await svc.get_session(app_name="t", user_id="u", session_id=s.id)
        return dict(s2.state or {})

    state = asyncio.run(_go())
    assert order == ["triage", "planner", "doer"]
    assert state["graph_route"]["route"] == gp.ROUTE_SMALL
    assert state["plan_small"] is True


# ── a rejected fast-path run goes back to the Doer ─────────────────────

def _gate(state):
    ctx = SimpleNamespace(state=state, route=None)
    G._validator_gate(ctx)
    return ctx.route


def _rejected(**kw):
    return {"graph_route": {"complexity": "trivial", "route": gp.ROUTE_TRIVIAL},
            "validator_verdict": {"verdict": "request_changes",
                                  "rationale": "no test for b == 0"},
            "feedback_verdict": "pass", "doer_iters": 2, **kw}


def test_a_rejected_trivial_run_is_redone_by_the_doer_not_replanned():
    st = _rejected()
    assert _gate(st) == gp.ROUTE_REDO
    assert "no test for b == 0." in st["replan_note"]
    assert st["replan_count"] == 1 and st["doer_iters"] == 0
    assert "feedback_verdict" not in st                    # a clean loop
    st["validator_verdict"] = {"verdict": "request_changes"}
    assert _gate(st) == gp.ROUTE_DONE                      # once


def test_a_rejected_small_plan_is_redone_by_the_doer_too():
    st = _rejected(plan_md="the plan", plan_small=True)
    st["graph_route"]["route"] = gp.ROUTE_SMALL
    assert _gate(st) == gp.ROUTE_REDO
    assert "plan_small" not in st and "no test for b == 0." in st["replan_note"]


def test_a_larger_plan_is_still_replanned(monkeypatch):
    assert _gate(_rejected(plan_md="the plan")) == gp.ROUTE_REPLAN
    full = _rejected(plan_md="the plan", plan_small=False)
    full["graph_route"]["route"] = gp.ROUTE_FULL
    assert _gate(full) == gp.ROUTE_REPLAN
    monkeypatch.setenv("AIFORGE_SMALL_REDO", "0")
    assert _gate(_rejected()) == gp.ROUTE_REPLAN
    assert _gate(_rejected(plan_md="p", plan_small=True)) == gp.ROUTE_REPLAN


def test_the_validators_words_reach_the_doer_whatever_their_shape():
    for raw in ('```json\n{"verdict": "request_changes", "rationale": "add x"}\n```',
                {"verdict": "request_changes", "rationale": "add x"}):
        assert G._validator_rationale({"validator_verdict": raw}) == "add x."
    prose = "**Verdict: request_changes** tests miss the zero case"
    assert "zero case" in G._validator_rationale({"validator_verdict": prose})


def test_the_graph_runs_the_redo_without_the_planner():
    import asyncio

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START, Edge, Workflow, node

    from aiforge_core.runtime._pipeline_graph import _entry_edges, _loop_edges

    order: list = []
    verdicts = iter(['{"verdict": "request_changes", "rationale": "add x"}',
                     '{"verdict": "approve"}'])

    def stub(name, sets=None):
        async def _fn(ctx):
            order.append(name)
            for k, v in (sets or {}).items():
                ctx.state[k] = v() if callable(v) else v
        return node(_fn, name=name)

    nodes = {
        "triage": stub("triage", _verdict("trivial", 2)),
        "triage_gate": gp.make_triage_gate(), "enhancer": stub("enhancer"),
        "planner": stub("planner"), "doer": stub("doer"),
        "refiner": stub("refiner"),
        "feedback": stub("feedback", {"feedback_verdict": "pass"}),
        "loop_gate": gp.make_loop_gate(),
        "validator": stub("validator", {"validator_verdict": lambda: next(verdicts)}),
        "validator_gate": gp.make_validator_gate(), "learner": stub("learner"),
    }
    edges = _entry_edges(Edge, START, nodes) + _loop_edges(Edge, nodes)

    async def _go():
        svc = InMemorySessionService()
        r = Runner(agent=Workflow(name="stub", edges=edges), app_name="t",
                   session_service=svc, auto_create_session=True)
        s = await svc.create_session(app_name="t", user_id="u")
        c = gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="go")])
        async for _ in r.run_async(user_id="u", session_id=s.id, new_message=c):
            pass

    asyncio.run(_go())
    loop = ["doer", "refiner", "feedback", "validator"]
    assert order == ["triage", *loop, *loop, "learner"]


# ── a plan of one small step ───────────────────────────────────────────

def test_a_plan_with_no_subtickets_and_two_named_files_is_small():
    assert P._plan_is_small({"plan_md": "x", "child_subtickets": [],
                             "scope_allowlist_globs": ["calc.py", "test_calc.py"]})


@pytest.mark.parametrize("plan", [
    {"scope_allowlist_globs": ["a.py"], "subtickets": [{"slug": "s"}]},
    {"scope_allowlist_globs": ["a.py"], "child_subtickets": [{"slug": "t"}]},
    {"scope_allowlist_globs": ["a.py", "b.py", "c.py"]},
    {"scope_allowlist_globs": ["src/**"]},
    {"scope_allowlist_globs": ["a.py", "tests/test_*.py"]},
    {"scope_allowlist_globs": []},
    {"plan_md": "no scope"},
    "not a plan",
])
def test_anything_larger_or_unscoped_is_not_small(plan):
    assert not P._plan_is_small(plan)


def _promote(state):
    G._plan_promote(SimpleNamespace(state=state))
    return state


_SMALL_PLAN = ('{"plan_md": "add f", "scope_allowlist_globs": ["a.py"], '
               '"child_subtickets": []}')


def test_plan_promote_records_whether_the_plan_is_small():
    assert _promote({"plan_md": _SMALL_PLAN})["plan_small"] is True
    big = '{"plan_md": "x", "scope_allowlist_globs": ["src/**"]}'
    assert _promote({"plan_md": big})["plan_small"] is False


def test_a_replanned_plan_is_never_small():
    st = _promote({"plan_md": _SMALL_PLAN, "replan_note": "verifier rejected"})
    assert st["plan_small"] is False


def test_a_new_plan_does_not_inherit_the_flag():
    for moment in ("replan", "verify"):
        assert "plan_small" in G._scoped(moment)


def test_a_small_plan_skips_the_plan_critic_and_the_polish(monkeypatch):
    st = {"plan_small": True}
    v = verifier._skip_decide(st)
    assert v["skipped"] is True and gp._parse_verdict(v) == "pass"
    assert G_verifier_passes(v)
    assert refiner._skip_decide(st)["refiner_skipped"] is True
    monkeypatch.setenv("AIFORGE_SMALL_PLAN_SKIP", "0")
    assert verifier._skip_decide(st) is None
    assert refiner._skip_decide(st) is None


def G_verifier_passes(verdict) -> bool:
    ctx = SimpleNamespace(state={"verifier_verdict": verdict}, route=None)
    G._verifier_gate(ctx)
    return ctx.route == gp.ROUTE_VERIFY_PASS


def test_a_larger_plan_is_still_verified_and_polished():
    assert verifier._skip_decide({"plan_small": False}) is None
    assert verifier._skip_decide({}) is None
    assert refiner._skip_decide({"doer_iters": 0}) is None
    # the earlier skips still hold
    assert verifier._skip_decide({"verify_replan_count": 1})["skipped"] is True
    assert refiner._skip_decide({"doer_iters": 1})["refiner_skipped"] is True


# ── the Doer's history is cut at a fixed point ─────────────────────────

def _c(role, text):
    return gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=text)])


def _focused(role, contents):
    req = SimpleNamespace(contents=list(contents))
    ef.make_executor_focus_callback(role)(llm_request=req)
    return [c.parts[0].text for c in req.contents]


def _history(prologue, own):
    """seed + other agents' events (ADK shows them as user) + own model/tool pairs."""
    out = [_c("user", "seed")]
    out += [_c("user", f"For context: p{i}") for i in range(prologue)]
    for i in range(own):
        out.append(_c("model" if i % 2 == 0 else "user", f"own{i}"))
    return out


def test_stepped_split_moves_only_every_step():
    assert ef.stepped_split(20, 20, 10) == 0
    assert [ef.stepped_split(n, 20, 10) for n in (29, 30, 39, 40)] == [0, 10, 10, 20]
    assert ef.stepped_split(45, 20, 1) == 25      # step 1 = the plain tail
    assert ef.stepped_split(5, 0, 10) == 0


def test_the_doer_starts_from_the_seed_alone():
    assert _focused("doer", _history(30, 0)) == ["seed"]


def test_the_doer_keeps_its_own_work_and_drops_the_prologue():
    out = _focused("doer", _history(30, 6))
    assert out == ["seed"] + [f"own{i}" for i in range(6)]


def test_consecutive_doer_requests_share_their_beginning():
    """Each call appends two contents. The request before and after must have
    the same prefix, except when a whole step is dropped."""
    changes = 0
    prev = _focused("doer", _history(30, 2))
    for own in range(4, 80, 2):
        cur = _focused("doer", _history(30, own))
        if cur[:len(prev)] != prev:
            changes += 1
        assert len(cur) <= 1 + 20 + 10 + 1
        prev = cur
    # 39 calls; a sliding window changed the beginning on every one of them
    assert 0 < changes <= 6


def test_step_one_slides_on_every_call(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONTEXT_TRIM_STEP", "1")
    a = _focused("doer", _history(0, 40))
    b = _focused("doer", _history(0, 42))
    assert len(a) == len(b) == 21 and a[1] != b[1]


def test_the_earlier_prologue_tail_can_be_kept(monkeypatch):
    monkeypatch.setenv("AIFORGE_EXECUTOR_PROLOGUE", "keep")
    monkeypatch.setenv("AIFORGE_CONTEXT_TRIM_STEP", "1")
    out = _focused("doer", _history(30, 0))
    assert out[0] == "seed" and len(out) == 21 and out[-1] == "For context: p29"


def test_the_refiner_keeps_the_doers_work_in_front_of_it():
    """The Refiner has no content of its own yet: what precedes it IS what it
    judges, so only the count cap applies."""
    hist = _history(4, 10)
    assert _focused("refiner", hist) == [c.parts[0].text for c in hist]
    out = _focused("refiner", _history(40, 10))
    assert out[0] == "seed" and out[-1] == "own9" and 21 <= len(out) <= 30


def test_focus_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_EXECUTOR_FOCUS", "0")
    hist = _history(30, 6)
    assert len(_focused("doer", hist)) == len(hist)


def test_a_tool_result_is_never_separated_from_its_call():
    call = gtypes.Content(role="model", parts=[gtypes.Part(
        function_call=gtypes.FunctionCall(id="c1", name="t", args={}))])
    resp = gtypes.Content(role="user", parts=[gtypes.Part(
        function_response=gtypes.FunctionResponse(id="c1", name="t",
                                                  response={"ok": True}))])
    contents = [_c("user", "seed"), _c("user", "For context: plan"), call, resp]
    req = SimpleNamespace(contents=list(contents))
    ef.make_executor_focus_callback("doer")(llm_request=req)
    assert req.contents == [contents[0], call, resp]


# ── the global count cap moves in steps too ────────────────────────────

def _is_human(c):
    return c.role == "user"


def _adjust(_contents, split):
    return split


def test_the_count_cap_cuts_in_steps(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "20")
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_TOKENS", "1000000")
    lim = ctxmod._CtxLimits()
    assert lim.trim_step == 10
    trim = ctxmod._tail_trimmer(lim, _adjust, _is_human)

    def kept(n):
        return [c.parts[0].text for c in trim(
            [_c("user", "seed")] + [_c("model", str(i)) for i in range(n)])]

    assert len(kept(25)) == 26                      # under one step: untouched
    a, b = kept(41), kept(45)
    assert a[:2] == b[:2] == ["seed", "19"]         # same cut, same beginning
    assert kept(50)[:2] == ["seed", "29"]


def test_the_count_cap_slides_with_step_one(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "20")
    monkeypatch.setenv("AIFORGE_CONTEXT_TRIM_STEP", "1")
    trim = ctxmod._tail_trimmer(ctxmod._CtxLimits(), _adjust, _is_human)
    out = trim([_c("user", "seed")] + [_c("model", str(i)) for i in range(100)])
    assert len(out) == 21 and out[-1].parts[0].text == "99"
