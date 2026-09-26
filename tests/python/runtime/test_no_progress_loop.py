"""The no-progress rule: varied steps that change nothing, read nothing new
and move no test are a loop (the live case: 30+ different brute-force
scripts guessing a sha256), while exploration, builds being waited on and
edit→test cycles are not."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import no_progress
from aiforge_core.runtime.chat_agent._turn import _idle_steps, _progress
from aiforge_core.runtime.doer_no_progress import DoerProgressGuard

HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _brute(i: int) -> str:
    return ("python3 << 'EOF'\nimport hashlib\n"
            f"target = '{HASH}'\n# Let's try variant {i}: formats around 2.5\n"
            f"for s in ['2.5{'0' * i}', '${i}.50', 'USD {i}']:\n"
            "    if hashlib.sha256(s.encode()).hexdigest() == target:\n"
            "        print('match', s)\n"
            "print('no match')\nEOF")


def _st(**kw):
    st = SimpleNamespace(stuck_recoveries=0, action_counts={}, reads_new=0,
                         edits_made=0, board={}, **_progress.progress_fields())
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _feed(st, name, args, result):
    got = _idle_steps.note_step(st, name, args, result)
    return got[0] if got else ""


# ── templates ────────────────────────────────────────────────────────────

def test_brute_force_scripts_share_a_template():
    t = {no_progress.command_template("run_command", {"cmd": _brute(i)})
         for i in range(30)}
    assert len(t) == 1
    assert "import hashlib" in t.pop()


def test_other_tools_are_their_exact_arguments():
    a = no_progress.command_template("file_read", {"path": "a.py"})
    b = no_progress.command_template("file_read", {"path": "b.py"})
    assert a != b


def test_output_class_masks_numbers():
    assert (no_progress.output_class("run_command", "tried 1000: no match")
            == no_progress.output_class("run_command", "tried 2000: no match"))
    assert no_progress.output_class("run_command", "") == ""
    assert no_progress.output_class("file_read", "x") == ""


def test_shell_read_paths():
    assert no_progress.shell_read_paths({"cmd": "sed -n 1,80p src/a.py"}) == ["src/a.py"]
    assert no_progress.shell_read_paths({"cmd": "cat a.py b.py"}) == ["a.py", "b.py"]
    assert no_progress.shell_read_paths({"cmd": _brute(1)}) == []
    assert no_progress.shell_read_paths({"cmd": "python3 x.py"}) == []


# ── the rule on its own ──────────────────────────────────────────────────

def test_six_of_eight_alike_nudge_then_six_more_stop():
    track: dict = {}
    got = [no_progress.observe(track, "t", "", False) for _ in range(12)]
    assert got.index("nudge") == 5
    assert got[-1] == "stop" and got.count("stop") == 1


def test_any_progress_empties_the_window():
    track: dict = {}
    for i in range(40):
        assert no_progress.observe(track, "t", "", i % 5 == 0) == ""


def test_the_ceiling_counts_only_steps_without_progress(monkeypatch):
    monkeypatch.setenv("AIFORGE_NO_PROGRESS_STEPS", "10")
    track: dict = {}
    got = [no_progress.observe(track, f"t{i}", f"o{i}", False) for i in range(10)]
    assert got == [""] * 9 + ["nudge"]
    assert "10 steps without progress" in track["last"]


def test_the_warning_budget_is_shared_with_same_failure():
    from aiforge_core.runtime import same_failure
    from aiforge_core.runtime.failure_signature import Failure
    track: dict = {}
    fail = Failure("test:t", 1, "t")
    assert [same_failure.observe(track, fail, s) for s in "abc"][-1] == "nudge"
    got = [no_progress.observe(track, "t", "", False) for _ in range(6)]
    assert got[-1] == "stop"                 # the one warning is spent


# ── chat: the live case, end to end ──────────────────────────────────────

def test_the_brute_force_hash_loop_is_stopped(tmp_path):
    (tmp_path / "money.py").write_text("def fmt(x):\n    return f'{x:.2f}'\n")
    (tmp_path / "test_money.py").write_text("import hashlib\n")
    steps = ['ACTION: file_read\nARGS_JSON: {"path": "money.py"}',
             'ACTION: file_read\nARGS_JSON: {"path": "test_money.py"}']
    steps += ['ACTION: run_command\nARGS_JSON: ' + __import__("json").dumps(
        {"cmd": _brute(i)}) for i in range(40)]
    seq = list(steps)
    seen = []

    def _fn(_role, convo):
        seen.append(convo[-1]["content"])
        return seq.pop(0) if seq else "FINAL: gave up"
    evs = list(ca.run_chat_agent([{"role": "user", "content": "make the test pass"}],
                                 cwd=str(tmp_path), complete_fn=_fn))
    runs = [e for e in evs if e["type"] == "tool" and e["name"] == "run_command"]
    assert runs and "no match" in runs[0]["result"]["stdout"]
    assert len(runs) == 12                   # 6 → nudge, 6 more → pause
    assert sum("no progress" in str(c) for c in seen) == 1
    last = [e for e in evs if e["type"] == "message"][-1]
    assert last.get("awaiting_input") is True
    assert evs[-1]["type"] == "done"


# ── chat: what must never trip ───────────────────────────────────────────

def test_reading_many_different_files_is_progress(tmp_path):
    st = _st()
    for i in range(60):
        st.reads_new += 1                   # what _record_read does per new read
        assert _feed(st, "file_read", {"path": f"f{i}.py"}, {"ok": True}) == ""
        assert _feed(st, "run_command", {"cmd": f"cat src/g{i}.py"},
                     {"ok": True, "stdout": "x = 1"}) == ""
        assert _feed(st, "grep", {"pattern": f"name{i}"}, {"ok": True}) == ""


def test_edit_then_test_cycles_are_progress(tmp_path):
    st = _st()
    f = tmp_path / "a.py"
    for i in range(40):
        f.write_text(f"v{i}")
        _progress.note_write(st, "file_write", {"path": "a.py"}, {"ok": True}, tmp_path)
        assert _feed(st, "file_write", {"path": "a.py"}, {"ok": True}) == ""
        assert _feed(st, "run_command", {"cmd": "pytest -q"},
                     {"ok": False, "stdout": "FAILED tests/a.py::t - x"}) == ""


def test_a_suite_whose_failures_drop_is_progress():
    st = _st()
    for k in range(30, 0, -1):
        out = "".join(f"FAILED tests/a.py::t{j} - x\n" for j in range(k))
        assert _feed(st, "run_command", {"cmd": "pytest -q"},
                     {"ok": False, "stdout": out}) == ""


def test_a_suite_that_stays_red_without_edits_trips():
    st = _st()
    got = [_feed(st, "run_command", {"cmd": "pytest -q"},
                 {"ok": False, "stdout": "FAILED tests/a.py::t - x"}) for _ in range(6)]
    assert got[-1] == "nudge"


def test_waiting_on_a_build_that_keeps_working_is_progress():
    st = _st()
    for _ in range(60):
        assert _feed(st, "command_wait", {"id": "j1"},
                     {"ok": True, "running": True, "output_growing": True}) == ""


def test_waiting_on_a_job_that_does_nothing_trips():
    st = _st()
    got = [_feed(st, "command_wait", {"id": "j1"},
                 {"ok": True, "running": True, "output_growing": False,
                  "cpu_active": False}) for _ in range(6)]
    assert got[-1] == "nudge"


def test_marking_a_task_done_is_progress():
    st = _st(board={"a": {"title": "a", "status": "pending"}})
    for _ in range(5):
        _feed(st, "run_command", {"cmd": _brute(1)}, {"ok": True, "stdout": "no match"})
    st.board["a"]["status"] = "done"
    assert _feed(st, "plan_progress", {"slug": "a"}, {"ok": True}) == ""
    assert _feed(st, "run_command", {"cmd": _brute(2)},
                 {"ok": True, "stdout": "no match"}) == ""


# ── the native pipeline Doer ─────────────────────────────────────────────

def test_native_doer_brute_force_nudges_then_stops_the_loop():
    guard = DoerProgressGuard()
    state: dict = {}
    notes = []
    for i in range(14):
        out = guard.step("run1", "run_command", {"cmd": _brute(i)},
                         {"ok": True, "stdout": "no match"}, state)
        notes.append((out or {}).get("loop_guard", ""))
    assert "no progress" in notes[5]
    assert "Stop calling tools" in notes[11]
    assert state == {"loop_budget_kill": True, "loop_budget_reason": "no_progress"}


def test_native_doer_exploring_is_left_alone():
    guard = DoerProgressGuard()
    for i in range(50):
        assert guard.step("r", "read_file", {"path": f"f{i}.py"}, {"ok": True}, {}) is None
        assert guard.step("r", "run_command", {"cmd": f"cat d/f{i}.py"},
                          {"ok": True, "stdout": "x"}, {}) is None


def test_native_doer_runs_are_tracked_apart():
    guard = DoerProgressGuard()
    for i in range(5):
        for run in ("a", "b"):
            assert guard.step(run, "run_command", {"cmd": _brute(i)},
                              {"ok": True, "stdout": "no match"}, {}) is None


def test_the_quality_callback_returns_the_note(monkeypatch):
    from aiforge_core.runtime.quality_gate import make_quality_signal_callback
    cb = make_quality_signal_callback()
    ctx = SimpleNamespace(state={}, invocation_id="inv1")
    tool = SimpleNamespace(name="run_command")
    outs = [cb(tool=tool, args={"cmd": _brute(i)}, tool_context=ctx,
               tool_response={"ok": True, "stdout": "no match"}) for i in range(6)]
    assert outs[:5] == [None] * 5
    assert "loop_guard" in outs[5]


def test_the_doer_loop_exits_partial_without_replan(monkeypatch):
    from aiforge_core.runtime.graph_pipeline import _config as C
    from aiforge_core.runtime.graph_pipeline import _gates as G
    monkeypatch.setattr(G, "_effective_max_iters", lambda state: 100)
    ctx = SimpleNamespace(state={"loop_budget_kill": True,
                                 "loop_budget_reason": "no_progress"}, route=None)
    G._loop_gate(ctx)
    assert ctx.route == C.ROUTE_EXIT
    assert ctx.state["feedback_verdict"] == "partial loop_budget_kill: no_progress"
    G._validator_gate(ctx)
    assert ctx.route == C.ROUTE_DONE


@pytest.mark.parametrize("bad", [None, "x", 3])
def test_odd_results_never_break_a_step(bad):
    st = _st()
    assert _idle_steps.note_step(st, "run_command", {"cmd": "ls"}, bad) is None
