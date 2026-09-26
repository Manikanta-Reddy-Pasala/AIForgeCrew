"""The same failure, fix after fix, is the loop — in the chat agent, the
graph Doer loop, parallel subtask retries and the reconcile repair loop.

A healthy run has no step or time limit; each of these loops used to run
forever (or to its round cap) when every fix was different and the failure
was not. And the verify-on-final skip must only trust a full, honest, green
test run on the tree's current CONTENT.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import same_failure
from aiforge_core.runtime.chat_agent._turn import _limits, _outcomes, _progress
from aiforge_core.runtime.failure_signature import Failure, failure_of

FAIL_X = Failure("test:tests/a.py::test_x", 1, "test_x")


# ── the rule ─────────────────────────────────────────────────────────────

def test_three_states_nudge_then_three_more_stop():
    track: dict = {}
    got = [same_failure.observe(track, FAIL_X, f"s{i}") for i in range(6)]
    assert got == ["", "", "nudge", "", "", "stop"]


def test_a_repeat_in_one_state_is_one_state():
    track: dict = {}
    got = [same_failure.observe(track, FAIL_X, "s0") for _ in range(5)]
    assert got == [""] * 5
    # …but a run that only varies the command still trips eventually
    assert same_failure.observe(track, FAIL_X, "s0") == "nudge"


def test_fewer_failures_is_progress():
    track: dict = {}
    many = Failure("line:boom", 5, "boom")
    fewer = Failure("line:boom", 3, "boom")
    assert same_failure.observe(track, many, "a") == ""
    assert same_failure.observe(track, many, "b") == ""
    assert same_failure.observe(track, fewer, "c") == ""     # count restarts
    assert same_failure.observe(track, fewer, "d") == ""
    assert same_failure.observe(track, fewer, "e") == "nudge"


def test_no_signature_is_never_a_loop():
    track: dict = {}
    assert all(same_failure.observe(track, Failure("", 0, ""), f"s{i}") == ""
               for i in range(10))


def test_the_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("AIFORGE_SAME_FAILURE_LIMIT", "2")
    track: dict = {}
    assert [same_failure.observe(track, FAIL_X, s) for s in "ab"] == ["", "nudge"]
    monkeypatch.setenv("AIFORGE_SAME_FAILURE_LIMIT", "junk")
    assert same_failure.same_failure_limit() == 3


# ── the chat agent ───────────────────────────────────────────────────────

def _scripted(outputs):
    seq = list(outputs)

    def _fn(_role, _convo):
        return seq.pop(0) if seq else "FINAL: out of script"
    return _fn


def _fix_and_test(i, failing="tests/a.py::test_x"):
    write = (f'ACTION: file_write\nARGS_JSON: {{"path": "a.py", '
             f'"content": "x = {i}\\n"}}')
    # a different command each round: -x, -q, … must not dodge the rule
    flag = ["-x", "-q", "-v", "-vv", "-rA", "--tb=short", "-s", "-l"][i % 8]
    run = ('ACTION: run_command\nARGS_JSON: {"cmd": "echo pytest ' + flag +
           f'; echo \'FAILED {failing} - assert {i} == 2\'; exit 1"}}')
    return [write, run]


def test_a_different_fix_each_round_with_the_same_failure_stops(tmp_path):
    steps = []
    for i in range(1, 12):
        steps += _fix_and_test(i)
    evs = list(ca.run_chat_agent([{"role": "user", "content": "fix test_x"}],
                                 cwd=str(tmp_path), complete_fn=_scripted(steps)))
    runs = [e for e in evs if e["type"] == "tool" and e["name"] == "run_command"]
    assert len(runs) == 6                      # 3 → nudge, 3 more → stop
    assert any("same failure again" in str(e.get("text", "")) for e in evs)
    last_msg = [e for e in evs if e["type"] == "message"][-1]
    assert last_msg.get("awaiting_input") is True
    assert "same failure" in last_msg["text"]
    assert evs[-1]["type"] == "done"


def test_the_nudge_rides_on_the_observation(tmp_path):
    steps = []
    for i in range(1, 4):
        steps += _fix_and_test(i)
    seen = []

    def _fn(_role, convo):
        seen.append(convo[-1]["content"])
        return steps.pop(0) if steps else "FINAL: stopped"
    list(ca.run_chat_agent([{"role": "user", "content": "fix"}],
                           cwd=str(tmp_path), complete_fn=_fn))
    nudged = [c for c in seen if "SAME failure survived" in str(c)]
    assert len(nudged) == 1
    assert str(nudged[0]).startswith("OBSERVATION:")


def test_a_failure_that_moves_is_not_a_loop(tmp_path):
    steps = []
    for i in range(1, 8):
        steps += _fix_and_test(i, failing=f"tests/a.py::test_{i}")
    steps.append("FINAL: done")
    evs = list(ca.run_chat_agent([{"role": "user", "content": "fix"}],
                                 cwd=str(tmp_path), complete_fn=_scripted(steps)))
    assert not any("same failure" in str(e.get("text", "")) for e in evs)
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "done"


def test_the_tracker_survives_in_loop_state_not_the_conversation():
    fields = _progress.progress_fields()
    assert fields["same_fail"] == {}
    assert _progress.progress_fields()["same_fail"] is not fields["same_fail"]


def test_a_timeout_or_a_block_is_not_the_codes_failure():
    st = SimpleNamespace(same_fail={}, state_fp="a", tree_pending=False)
    res = {"ok": False, "timed_out": True, "stdout": "FAILED tests/a.py::t"}
    assert _outcomes.note_failure(st, "run_command", {}, res) is None
    assert st.same_fail == {}
    assert _outcomes.note_failure(st, "file_read", {}, {"ok": False,
                                  "error": "error: no such file"}) is None


# ── verify on FINAL: a content fingerprint and an honest green ──────────

@pytest.fixture
def repo(tmp_path):
    import shutil
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    (tmp_path / "foo.py").write_text("v1\n")
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_the_fingerprint_is_content_not_the_list_of_dirty_files(repo):
    (repo / "foo.py").write_text("v2 — green\n")
    green = _outcomes.content_fingerprint(str(repo))
    (repo / "foo.py").write_text("v3 — broken again\n")      # same dirty list
    assert _outcomes.content_fingerprint(str(repo)) != green
    (repo / "foo.py").write_text("v2 — green\n")
    assert _outcomes.content_fingerprint(str(repo)) == green
    (repo / "new.py").write_text("a\n")                        # untracked too
    fp = _outcomes.content_fingerprint(str(repo))
    (repo / "new.py").write_text("b\n")
    assert _outcomes.content_fingerprint(str(repo)) != fp


def test_no_repository_is_no_signal(tmp_path):
    assert _outcomes.content_fingerprint(str(tmp_path)) is None


GREEN = {"ok": True, "stdout": "======== 12 passed in 0.40s ========"}


@pytest.mark.parametrize("cmd", [
    "pytest -q | tail -20",
    "pytest || true",
    "pytest; echo done",
    "pytest -k unrelated",
    "pytest tests/test_one.py",
    "pytest tests/test_a.py::test_x",
    "pytest --lf",
])
def test_a_green_that_proves_nothing_is_not_remembered(repo, cmd):
    st = SimpleNamespace(last_green_fp="stale")
    _outcomes._note_green_tests(st, "run_command", {"cmd": cmd}, GREEN, str(repo))
    assert st.last_green_fp == "stale"


@pytest.mark.parametrize("cmd", ["pytest -q", "python -m pytest -q tests/",
                                 "cd sub && pytest 2>&1", "pytest -m 'not slow' -q"])
def test_a_full_honest_green_is_remembered(repo, cmd):
    st = SimpleNamespace(last_green_fp=None)
    _outcomes._note_green_tests(st, "run_command", {"cmd": cmd}, GREEN, str(repo))
    if "not slow" in cmd:                        # a marker narrows the run
        assert st.last_green_fp is None
    else:
        assert st.last_green_fp == _outcomes.content_fingerprint(str(repo))


def test_exit_zero_without_the_runners_word_is_not_green(repo):
    st = SimpleNamespace(last_green_fp=None)
    _outcomes._note_green_tests(st, "run_command", {"cmd": "pytest"},
                                {"ok": True, "stdout": "no tests ran"}, str(repo))
    assert st.last_green_fp is None


def test_a_red_run_forgets_the_green(repo):
    st = SimpleNamespace(last_green_fp="x")
    _outcomes._note_green_tests(st, "run_tests", {"mode": "all"},
                                {"ok": False, "stdout": "1 failed"}, str(repo))
    assert st.last_green_fp is None


def test_run_tests_counts_only_as_a_full_unfiltered_run(repo):
    st = SimpleNamespace(last_green_fp=None)
    _outcomes._note_green_tests(st, "run_tests", {"mode": "fast"}, GREEN, str(repo))
    assert st.last_green_fp is None
    _outcomes._note_green_tests(st, "run_tests", {"mode": "all", "pattern": "x"},
                                GREEN, str(repo))
    assert st.last_green_fp is None
    _outcomes._note_green_tests(st, "run_tests", {"mode": "all"}, GREEN, str(repo))
    assert st.last_green_fp


def test_editing_after_a_green_run_makes_final_verify_again(repo, monkeypatch):
    st = SimpleNamespace(last_green_fp=None, edits_made=1, verify_rounds=0,
                         verify_prev_fails=None, verify_stalls=0, convo=[],
                         same_fail={}, state_fp="", tree_pending=False)
    (repo / "foo.py").write_text("fixed\n")
    _outcomes._note_green_tests(st, "run_command", {"cmd": "pytest"}, GREEN, str(repo))
    ran = []
    monkeypatch.setattr(_outcomes, "_run_project_verify",
                        lambda cwd: ran.append(cwd) or (True, "ok"))
    list(_outcomes._verify_on_final(st, {"text": "done"}, str(repo), False, ""))
    assert ran == []                                     # unchanged: skipped
    (repo / "foo.py").write_text("broken again\n")       # same file, new bytes
    list(_outcomes._verify_on_final(st, {"text": "done"}, str(repo), False, ""))
    assert ran == [str(repo)]


# ── the older guards: no refill that a loop can earn ─────────────────────

def _loop_state(**kw):
    st = SimpleNamespace(stuck_recoveries=0, action_counts={},
                         **_progress.progress_fields())
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_the_lifetime_count_is_forgiven_once(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LOOP_BACKSTOP", "2")
    st = _loop_state()
    hits = [_progress.strike(st, "run_command|pytest", per_state=False)
            for _ in range(6)]
    assert hits[-1] == "often"
    _progress.forgive(st, "run_command|pytest")
    assert _progress.strike(st, "run_command|pytest", per_state=False) == ""
    for _ in range(6):
        _progress.strike(st, "run_command|pytest", per_state=False)
    _progress.forgive(st, "run_command|pytest")
    st.backstop.clear()
    assert _progress.strike(st, "run_command|pytest", per_state=False) == "often"


def test_giving_up_on_a_task_does_not_refill_the_recoveries(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MAX_RECOVERIES", "2")
    st = _loop_state(board={"a": {"title": "a", "status": "pending"},
                            "b": {"title": "b", "status": "pending"}})
    st.recoveries_total = 2
    assert not _progress.may_recover(st)
    st.board["a"]["status"] = "failed"
    assert not _progress.may_recover(st)
    st.board["b"]["status"] = "skipped"
    assert not _progress.may_recover(st)
    st.board["a"]["status"] = "done"
    assert _progress.may_recover(st)


# ── replies that never act ───────────────────────────────────────────────

def test_replies_without_acting_nudge_once_then_pause():
    st = SimpleNamespace(convo=[])
    sigs, events = [], []
    for _ in range(2 * _limits._IDLE_REPLIES):
        g = _limits._idle_reply_guard(st)
        try:
            while True:
                events.append(next(g))
        except StopIteration as stop:
            sigs.append(stop.value)
    assert sigs[:-1] == ["continue"] * (2 * _limits._IDLE_REPLIES - 1)
    assert sigs[-1] == "return"
    assert sum("ran no tool" in m["content"] for m in st.convo) == 1
    assert events[-2]["awaiting_input"] is True


def test_plan_mode_asking_again_and_again_pauses(tmp_path):
    from aiforge_core.runtime.chat_agent import _pause
    _pause.reset()
    _pause.save(9101, [{"role": "user", "content": "plan"}], asked=True)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "plan the refactor"}], cwd=str(tmp_path),
        complete_fn=lambda _r, _c: "ASK: which module?", mode="plan",
        session_id=9101))
    _pause.reset()
    assert evs[-1]["type"] == "done"
    assert any(e.get("awaiting_input") for e in evs if e["type"] == "message")


# ── the graph Doer loop ──────────────────────────────────────────────────

def test_the_doer_loop_stops_on_the_same_failure_without_a_replan(monkeypatch):
    from aiforge_core.runtime.graph_pipeline import _config as C
    from aiforge_core.runtime.graph_pipeline import _gates as G
    monkeypatch.setattr(G, "_effective_max_iters", lambda state: 100)
    ctx = SimpleNamespace(state={}, route=None)
    routes, notes = [], []
    for _ in range(10):
        ctx.state["_iter_fail"] = list(FAIL_X)
        G._loop_gate(ctx)
        routes.append(ctx.route)
        notes.append(ctx.state.get("replan_note"))
        if ctx.route == C.ROUTE_EXIT:
            break
    assert routes == [C.ROUTE_LOOP] * 5 + [C.ROUTE_EXIT]
    assert "SAME failure" in notes[2]
    assert ctx.state["feedback_verdict"] == "partial loop_budget_kill: same_failure"
    G._validator_gate(ctx)
    assert ctx.route == C.ROUTE_DONE                      # no replan


def test_the_doer_loop_tracker_survives_a_replan(monkeypatch):
    from aiforge_core.runtime.graph_pipeline import _gates as G
    monkeypatch.setattr(G, "_effective_max_iters", lambda state: 100)
    ctx = SimpleNamespace(state={"feedback_verdict": "fail"}, route=None)
    ctx.state["_iter_fail"] = list(FAIL_X)
    G._loop_gate(ctx)
    monkeypatch.setattr(G, "_validator_failed", lambda state: True)
    G._validator_gate(ctx)
    assert ctx.state["_same_failure"]["sigs"]


def test_test_failure_reads_the_tool_result():
    from aiforge_core.runtime.quality_gate import test_failure
    assert test_failure({"ok": True, "stdout": "3 passed"}) == []
    got = test_failure({"ok": False, "stdout": "FAILED tests/a.py::t - x"})
    assert got[0] == "test:tests/a.py::t"


# ── parallel subtasks ────────────────────────────────────────────────────

def test_a_subtask_that_fails_the_same_way_twice_is_not_retried(monkeypatch):
    from aiforge_core.runtime.parallel_subtasks import _worktree as W
    monkeypatch.setattr(W, "_retries", lambda: 5)
    monkeypatch.setattr(W, "_max_workers", lambda: 2)
    monkeypatch.setattr(W, "_reset_worktree", lambda *a: None)
    monkeypatch.setattr(W, "_commit_all", lambda *a: True)
    monkeypatch.setattr(W, "_emit", lambda *a, **k: None)
    calls = []

    def _run(sub, wt):
        calls.append(sub.get("_retry_error"))
        return {"ok": False, "error": "FAILED tests/a.py::t - assert 1 == 2"}
    last, _i = W._run_with_retries({"slug": "s"}, "/wt", "s", "main", 1, _run, None)
    assert len(calls) == 2
    assert last["same_failure"] is True


def test_a_subtask_whose_failure_changes_keeps_its_retries(monkeypatch):
    from aiforge_core.runtime.parallel_subtasks import _worktree as W
    monkeypatch.setattr(W, "_retries", lambda: 3)
    monkeypatch.setattr(W, "_max_workers", lambda: 2)
    monkeypatch.setattr(W, "_reset_worktree", lambda *a: None)
    monkeypatch.setattr(W, "_commit_all", lambda *a: True)
    monkeypatch.setattr(W, "_emit", lambda *a, **k: None)
    n = {"i": 0}

    def _run(sub, wt):
        n["i"] += 1
        return {"ok": False, "error": f"FAILED tests/a.py::t{n['i']} - x"}
    W._run_with_retries({"slug": "s"}, "/wt", "s", "main", 1, _run, None)
    assert n["i"] == 4


def test_restarts_skip_a_subtask_that_failed_the_same_way(monkeypatch):
    from aiforge_core.runtime.parallel_subtasks import _orchestrate as O
    monkeypatch.setattr(O, "_rerun_rounds", lambda: 4)
    rounds = []

    def _dispatch(subs, should_cancel=None, **kw):
        rounds.append([s["slug"] for s in subs])
        return [{"slug": s["slug"], "ok": False, "fail_sig": "test:t"} for s in subs]
    monkeypatch.setattr(O, "_dispatch_batch", _dispatch)
    O._run_with_restarts([{"slug": "a"}], None)
    assert rounds == [["a"], ["a"]]


def test_wave_retries_stop_on_the_same_error(monkeypatch):
    from aiforge_core.runtime.parallel_subtasks import _orchestrate as orch
    monkeypatch.setenv("AIFORGE_DECOMP_RETRIES", "5")
    monkeypatch.setattr(orch, "_emit", lambda *a, **k: None)
    calls = {"n": 0}

    def _attempt(*_a):
        calls["n"] += 1
        return {"ok": False, "error": "src/app.ts(3,1): error TS2304: Cannot find name 'x'."}
    monkeypatch.setattr(orch, "_attempt_subtask", _attempt)
    r = orch._attempt_with_retries({"slug": "s"}, "/wt", None, None, 1, "s", 0, None)
    assert calls["n"] == 2
    assert r["same_failure"] is True


# ── the reconcile repair loop ────────────────────────────────────────────

def test_reconcile_signature_reads_a_java_build():
    from aiforge_core.runtime.parallel_subtasks._reconcile import _testrun
    _failure_signature = _testrun._failure_signature
    a = "[ERROR] /w/1/src/Foo.java:[12,5] cannot find symbol\n[INFO] BUILD FAILURE\n"
    b = "[ERROR] /w/2/src/Foo.java:[14,9] cannot find symbol\n[INFO] BUILD FAILURE\n"
    assert _failure_signature(a) and _failure_signature(a) == _failure_signature(b)
    assert _failure_signature("FAILED tests/t.py::a - x") == failure_of(
        "FAILED tests/t.py::a - y").signature
