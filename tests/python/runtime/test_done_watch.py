"""The harness sees the work is done instead of waiting for the model: big
benchmark runs never replied without a tool call and used all 50 minutes.
It is strict: ending a turn that was not done would lose the user's work."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _done_watch as W
from aiforge_core.runtime.chat_agent._turn import _outcomes

FULL = ("class Cart:\n    def __init__(self):\n        self.items = []\n\n"
        "    def total(self, *, tax=0):\n        return 1\n")


def _drain(g):
    evs = []
    try:
        while True:
            evs.append(next(g))
    except StopIteration as e:
        return evs, e.value


@pytest.fixture
def st(tmp_path, monkeypatch):
    """A turn that started without Cart.total / Cart.items and has added them."""
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "m.py").write_text("class Cart:\n    pass\n")
    monkeypatch.setattr(_outcomes, "content_fingerprint", lambda cwd: "fp1")
    monkeypatch.setattr(W, "_jobs_running", lambda st: False)
    s = SimpleNamespace(
        goal="Add `Cart.total(self, *, tax=0)` and `Cart.items` in `shop/m.py`.",
        cwd=str(tmp_path), convo=[{"role": "system", "content": "s"}], edits_made=0,
        steers=[], session_id=None,
        board={"part-1": {"title": "totals", "status": "running"}})
    _drain(W.watch(s, 1))                       # turn start: 2 names missing
    assert s.dw_initial == 2
    (tmp_path / "shop" / "m.py").write_text(FULL)
    s.edits_made, s.green_at_edits, s.last_green_fp = 3, 3, "fp1"
    return s


def test_done_work_gets_a_note_then_a_forced_answer_never_an_end(st, monkeypatch):
    from aiforge_core.runtime.chat_agent import _native
    monkeypatch.setattr(W, "_replies_only_looked", lambda st: True)
    st.session_id = 41
    evs, sig = _drain(W.watch(st, 10))
    assert sig is None and "Done check" in st.convo[-1]["content"]
    assert st.board["part-1"]["status"] == "running"     # the board is the model's
    for n in range(11, 10 + W.grace()):
        assert _drain(W.watch(st, n))[1] is None
    assert 41 not in _native._FORCE_FINAL
    evs, sig = _drain(W.watch(st, 10 + W.grace()))
    assert sig is None and 41 in _native._FORCE_FINAL     # next call: no tool
    _native._FORCE_FINAL.discard(41)
    for _ in range(W.MAX_FORCED * W.grace() * 2):
        _drain(W.watch(st, 99))
    assert st.dw_forced == W.MAX_FORCED                  # bounded
    _native._FORCE_FINAL.discard(41)


def test_a_command_that_acts_resets_the_quiet_count(st, monkeypatch):
    from aiforge_core.runtime.chat_agent import _native
    st.session_id = 42
    looked = iter([True, True, False] + [True] * 20)
    monkeypatch.setattr(W, "_replies_only_looked", lambda st: next(looked))
    _drain(W.watch(st, 10))
    for n in range(11, 11 + W.grace()):
        _drain(W.watch(st, n))
    assert 42 not in _native._FORCE_FINAL                # pushed back by the push
    _native._FORCE_FINAL.discard(42)


def test_never_forces_when_it_cannot_tell_what_the_replies_did(st):
    from aiforge_core.runtime.chat_agent import _native
    _drain(W.watch(st, 10))                    # session_id None: no step log
    for n in range(11, 30):
        _drain(W.watch(st, n))
    assert not getattr(st, "dw_forced", 0)


def test_a_request_that_only_changes_existing_names_never_triggers(tmp_path, monkeypatch):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "m.py").write_text(FULL)       # every name there from step 0
    monkeypatch.setattr(_outcomes, "content_fingerprint", lambda cwd: "fp1")
    st = SimpleNamespace(goal="Make `Cart.total(self, *, tax=0)` and `Cart.items` in "
                              "`shop/m.py` accept YAML.", cwd=str(tmp_path), convo=[],
                         edits_made=0, steers=[], session_id=None, board={})
    _drain(W.watch(st, 1))
    st.edits_made, st.green_at_edits, st.last_green_fp = 1, 1, "fp1"
    assert not W.done_now(st)


def test_not_done_without_a_green_suite_with_gaps_or_an_open_target(st, tmp_path):
    assert W.done_now(st)
    st.last_green_fp = None
    assert not W.done_now(st)
    st.last_green_fp = "fp1"
    st.board["t"] = {"title": "match out.json", "status": "running", "enforced": True}
    assert not W.done_now(st)
    del st.board["t"]
    st.tests_green = False                     # the turn's last test result is red
    assert not W.done_now(st)
    st.tests_green = None
    (tmp_path / "shop" / "m.py").write_text("class Cart:\n    pass\n")
    st.edits_made = st.green_at_edits = 4
    assert not W.done_now(st)


def test_a_user_message_blocks_the_watch_until_the_next_edit(st, monkeypatch):
    monkeypatch.setattr(W, "_replies_only_looked", lambda st: True)
    st.session_id = 43
    _drain(W.watch(st, 10))
    st.steers.append("also commit and push")
    for n in range(11, 30):
        _drain(W.watch(st, n))
    assert not W.done_now(st) and not getattr(st, "dw_forced", 0)


def test_the_harness_reruns_the_turns_own_test_command_after_idle_replies(st, monkeypatch):
    st.last_green_fp = None
    st.last_full_test = ("run_command", {"cmd": "/venv/bin/python -m pytest -q tests"})
    calls = []
    monkeypatch.setattr(W, "_run", lambda cmd, cwd, timeout=0: calls.append(cmd) or (0, "5 passed in 1s"))
    for n in range(2, W.IDLE + 4):
        _drain(W.watch(st, n))
    assert calls == ["/venv/bin/python -m pytest -q tests"] and st.last_green_fp == "fp1"


def test_a_run_with_no_tests_or_no_command_is_not_green(st, monkeypatch):
    st.last_green_fp = None
    monkeypatch.setattr(W, "_run", lambda cmd, cwd, timeout=0: (0, "BUILD SUCCESS"))
    for n in range(2, W.IDLE + 4):
        _drain(W.watch(st, n))
    assert not st.last_green_fp                 # no last_full_test: nothing run
    st.last_full_test = ("run_command", {"cmd": "mvn verify -DskipTests"})
    st.dw_probed_at = None
    for n in range(20, 20 + W.IDLE + 2):
        _drain(W.watch(st, n))
    assert not st.last_green_fp                 # ran, but no "N passed"


def _git_repo(tmp_path, files):
    import os
    import subprocess
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True,
                       env=env)


def test_failures_the_original_code_has_too_are_pre_existing(tmp_path):
    import sys
    _git_repo(tmp_path, {"test_a.py": "def test_old():\n    assert False\n\n"
                                      "def test_ddl():\n    assert True\n"})
    cmd = f"cd {tmp_path} && {sys.executable} -m pytest -q -p no:cacheprovider"
    code, out = W._run(cmd, str(tmp_path))
    assert W.pre_existing(str(tmp_path), cmd, out)        # test_old fails on HEAD too
    (tmp_path / "test_a.py").write_text("def test_old():\n    assert False\n\n"
                                        "def test_ddl():\n    assert False\n")
    code, out = W._run(cmd, str(tmp_path))
    assert not W.pre_existing(str(tmp_path), cmd, out)    # the run broke test_ddl


def test_a_failure_counts_as_known_only_when_the_request_says_so():
    st = SimpleNamespace(goal="(`tests/x.py::T::test_lazy_load` already fails in this "
                              "environment; ignore it). Fix it so `test_parse` passes.")
    out = lambda i: {"ok": False, "stdout": f"FAILED tests/x.py::T::{i} - x\n1 failed, 9 passed\n"}
    assert _outcomes._only_known_failures(st, out("test_lazy_load"))
    assert not _outcomes._only_known_failures(st, out("test_parse"))   # asked to fix it
    assert not _outcomes._only_known_failures(st, out("test_lazy"))    # not a whole word
    st2 = SimpleNamespace(goal="Fix the flaky test `test_retry`.")
    assert not _outcomes._only_known_failures(st2, out("test_retry"))  # asked to fix


@pytest.mark.parametrize("step,acts", [
    ({"name": "read_lines", "args": {}}, False),
    ({"name": "run_command", "args": {"cmd": "cd /r && python -m pytest -q tests"}}, False),
    ({"name": "run_command", "args": {"cmd": "git status && git diff"}}, False),
    ({"name": "run_command", "args": {"cmd": "git commit -am x && git push"}}, True),
    ({"name": "run_command", "args": {"cmd": "kubectl apply -f k.yaml"}}, True),
    ({"name": "run_command", "args": {"cmd": "pip install requests"}}, True),
    ({"name": "jira_create", "args": {}}, True),
])
def test_what_counts_as_only_looking(step, acts):
    assert W._acts(step) is acts


# third review

def test_the_probe_reruns_only_a_bare_test_command():
    assert W._probe_command("cd /r && PYTHONPATH=src py -m pytest -q tests")
    for cmd in ("py -m pytest -q && git commit -am wip", "pytest -q; make deploy",
                "pytest -q > out.txt", "pytest -q | tail -5"):
        assert W._probe_command(cmd) is None, cmd


def test_an_exitfirst_run_is_not_the_whole_suite():
    assert not _outcomes._full_and_honest("run_command", {"cmd": "python -m pytest -x -q"})
    assert not _outcomes._full_and_honest("run_command", {"cmd": "pytest --maxfail=1"})
    assert _outcomes._full_and_honest("run_command", {"cmd": "python -m pytest -q tests"})


def test_a_negated_or_fix_sentence_is_not_a_known_failure():
    for goal in ("`test_parse` is not flaky — it's a real regression, investigate.",
                 "Remove the xfail on `test_parse`."):
        st = SimpleNamespace(goal=goal)
        out = {"ok": False, "stdout": "FAILED t.py::test_parse - x\n1 failed, 3 passed\n"}
        assert not _outcomes._only_known_failures(st, out), goal


def test_the_base_run_imports_the_original_code_not_the_edited_one(tmp_path):
    import sys
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("def f():\n    return 1\n")
    (tmp_path / "test_f.py").write_text("from pkg import f\n\ndef test_f():\n    assert f() == 1\n")
    _git_repo(tmp_path, {})
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("def f():\n    return 2\n")
    cmd = f"cd {tmp_path} && PYTHONPATH={tmp_path}/src {sys.executable} -m pytest -q -p no:cacheprovider"
    code, out = W._run(cmd, str(tmp_path))
    assert code != 0                                   # the edit broke test_f
    assert not W.pre_existing(str(tmp_path), cmd, out)  # it passes on the original


def test_the_finish_check_falls_back_to_the_turns_own_command_when_the_runner_cannot_run(
        tmp_path, monkeypatch):
    """The harness's guessed runner failed to collect click's suite ("999
    failing") while the model's own command ran it green: two false rounds.
    Its verdict stands whenever it did run tests."""
    import sys
    _git_repo(tmp_path, {"test_a.py": "def test_ok():\n    assert True\n"})
    monkeypatch.setattr(_outcomes, "_run_project_verify",
                        lambda cwd: (False, "ImportError while loading conftest"))
    st = SimpleNamespace(cwd=str(tmp_path), goal="x", last_full_test=(
        "run_command", {"cmd": f"cd {tmp_path} && {sys.executable} -m pytest -q -p no:cacheprovider"}))
    ok, out = _outcomes._project_verify(st, str(tmp_path))
    assert ok is True and "1 passed" in out
    (tmp_path / "test_a.py").write_text("def test_ok():\n    assert False\n\n"
                                        "def test_b():\n    assert True\n")
    ok, out = _outcomes._project_verify(st, str(tmp_path))
    assert ok is False and "FAILED" in out                # broken by the turn
    st.last_full_test = None             # no own command; no base recorded: the runner stands
    monkeypatch.setattr(_outcomes, "_BROKEN_AT_BASE", {})
    assert _outcomes._project_verify(st, str(tmp_path))[0] is False
    monkeypatch.setattr(_outcomes, "_run_project_verify",   # the runner ran tests: it stands
                        lambda cwd: (False, "FAILED t.py::x\n1 failed, 9 passed"))
    st.last_full_test = ("run_command", {"cmd": f"cd {tmp_path} && true"})
    assert _outcomes._project_verify(st, str(tmp_path)) == (False, "FAILED t.py::x\n1 failed, 9 passed")


def test_a_red_exit_with_no_listed_failure_is_not_judged(tmp_path, monkeypatch):
    """pytest --cov-fail-under: 120 passed, then exit 1 for the coverage floor."""
    monkeypatch.setattr(W, "_run", lambda cmd, cwd, timeout=0: (
        1, "120 passed in 3s\nFAIL Required test coverage of 90% not reached."))
    st = SimpleNamespace(cwd=str(tmp_path), goal="x")
    assert W.own_test_verdict(st, "pytest --cov=p --cov-fail-under=90") is None


@pytest.mark.parametrize("cmd,whole", [
    ("cd /r && PYTHONPATH=src /v/bin/python -m pytest tests -q", True),
    ("python -m pytest -q -p no:cacheprovider", True),
    ("pytest tests/ -q --tb short", True),
    ("pytest tests/unit -q", False),
    ("pytest -q --ignore=tests/integration", False),
    ("pytest tests/test_a.py", False),
    ("make test", False),
    ("cd /r/backend && python -m pytest", False),
    ("cd sub&&pytest", False),
    ("pytest --noconftest", False),
    ("pytest -o testpaths=tests/unit", False),
    ("pytest --override-ini=testpaths=tests/unit", False),
    ("pytest --sw", False),
    ("pytest --confcutdir=tests/unit", False),
    ("pytest -c other.ini", False),
])
def test_only_a_whole_suite_run_stands_in_for_the_runner(cmd, whole):
    assert _outcomes._whole_suite(cmd, "/r") is whole


def test_a_static_check_failure_is_a_lint_failure_not_failing_tests():
    from aiforge_core.runtime.chat_agent._context._verify import lint_only
    assert lint_only("12 passed\n\n=== static checks — fix these ===\n=== go vet ===\nx")


def test_a_cd_into_a_repo_root_from_its_parent_is_still_the_whole_suite(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo, {"a.py": "x = 1\n"})
    (repo / "backend").mkdir()
    assert _outcomes._whole_suite(f"cd {repo} && python -m pytest -q", str(tmp_path))
    assert _outcomes._whole_suite("cd repo && pytest", str(tmp_path))     # relative to base
    assert not _outcomes._whole_suite(f"cd {repo}/backend && pytest", str(tmp_path))


def test_every_board_item_done_asks_for_the_summary_and_forces_it_soon(tmp_path, monkeypatch):
    """A worked task list whose items are all done ends the turn: a note, then
    the answer forced after BOARD_GRACE replies that only re-check."""
    from aiforge_core.runtime.chat_agent import _native
    monkeypatch.setattr(W, "_jobs_running", lambda st: False)
    monkeypatch.setattr(W, "_replies_only_looked", lambda st: True)
    st = SimpleNamespace(goal="Do a and b.", cwd=str(tmp_path), convo=[{"role": "user", "content": "x"}],
                         edits_made=2, steers=[], session_id=55, board_touched=True,
                         board={"a": {"title": "a", "status": "done"},
                                "b": {"title": "b", "status": "done"}})
    monkeypatch.setattr(W, "done_now", lambda st: False)
    _drain(W.watch(st, 10))
    assert "Every item on the task board is done" in st.convo[-1]["content"]
    for n in range(11, 11 + W.BOARD_GRACE):
        _drain(W.watch(st, n))
    assert 55 in _native._FORCE_FINAL
    _native._FORCE_FINAL.discard(55)


def test_a_board_with_a_failed_or_open_item_or_red_tests_is_not_done(tmp_path):
    base = dict(goal="x", cwd=str(tmp_path), edits_made=1, steers=[], board_touched=True)
    two = lambda s: {"a": {"title": "a", "status": "done"}, "b": {"title": "b", "status": s}}
    assert W.board_done(SimpleNamespace(**base, board=two("done")))
    assert not W.board_done(SimpleNamespace(**base, board=two("failed")))
    assert not W.board_done(SimpleNamespace(**base, board=two("running")))
    assert not W.board_done(SimpleNamespace(**base, board=two("done"), tests_green=False))
    assert not W.board_done(SimpleNamespace(**base, board={"a": {"title": "a", "status": "done"}}))


def test_carried_targets_unverified_closes_and_a_red_full_run_do_not_end_the_turn(tmp_path):
    two = {"a": {"title": "a", "status": "done"}, "b": {"title": "b", "status": "done"}}
    base = dict(goal="x", cwd=str(tmp_path), edits_made=1, steers=[])
    assert not W.board_done(SimpleNamespace(**base, board=two, board_used=True))   # not worked
    unv = {"a": {"title": "a", "status": "done", "note": "UNVERIFIED: no test"},
           "b": {"title": "b", "status": "done"}}
    assert not W.board_done(SimpleNamespace(**base, board=unv, board_touched=True))
    assert not W.board_done(SimpleNamespace(**base, board=two, board_touched=True,
                                            last_full_test=("run_command", {}), tests_green=None))


def test_a_runner_that_cannot_run_the_original_suite_either_has_no_verdict(tmp_path, monkeypatch):
    """sqlglot: the runner cannot collect the suite (a deprecation warning);
    the model ran one test file; a 6/6 answer got 'checks still FAIL'."""
    _git_repo(tmp_path, {"test_a.py": "def test_ok():\n    assert True\n"})
    (tmp_path / ".aiforge-venv").mkdir()                       # the runner's venv exists
    monkeypatch.setattr(_outcomes, "_BROKEN_AT_BASE", {})
    monkeypatch.setattr(_outcomes, "_run_project_verify",
                        lambda cwd: (False, "ImportError while loading conftest"))
    st = SimpleNamespace(cwd=str(tmp_path), goal="x", dw_base_rev="HEAD", last_full_test=(
        "run_command", {"cmd": "pytest tests/test_a.py"}))       # narrower than the suite
    ok, out = _outcomes._project_verify(st, str(tmp_path))
    assert ok is None                                          # unknown, not FAIL
    monkeypatch.setattr(_outcomes, "_BROKEN_AT_BASE", {})
    calls = []
    def runner(cwd):
        calls.append(cwd)
        return (True, "3 passed") if len(calls) == 2 else (False, "ImportError while loading conftest")
    monkeypatch.setattr(_outcomes, "_run_project_verify", runner)
    assert _outcomes._project_verify(st, str(tmp_path))[0] is False   # fine at base: the edit broke it



def test_no_base_check_without_a_recorded_base_or_a_runner_venv(tmp_path):
    _git_repo(tmp_path, {"a.py": "x = 1\n"})
    assert not _outcomes._runner_broken_at_base(SimpleNamespace(), str(tmp_path))
    assert not _outcomes._runner_broken_at_base(SimpleNamespace(dw_base_rev="HEAD"), str(tmp_path))


def test_a_done_board_with_a_named_part_still_missing_is_not_done(tmp_path, monkeypatch):
    """shopkit: 'every task-board item is done' then the spec check sent the
    answer back for a missing name — twice."""
    two = {"a": {"title": "a", "status": "done"}, "b": {"title": "b", "status": "done"}}
    st = SimpleNamespace(goal="x", cwd=str(tmp_path), edits_made=1, steers=[],
                         board_touched=True, board=two)
    monkeypatch.setattr(W, "_raw_gaps", lambda st: ["Cart.total is missing"])
    assert not W.board_done(st)
    monkeypatch.setattr(W, "_raw_gaps", lambda st: [])
    st.edits_made = 2                                   # a new code state: checked again
    assert W.board_done(st)



def test_a_waived_gap_does_not_hold_a_done_board_and_the_check_runs_once_per_edit(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _contract_gate
    two = {"a": {"title": "a", "status": "done"}, "b": {"title": "b", "status": "done"}}
    st = SimpleNamespace(goal="x", cwd=str(tmp_path), edits_made=1, steers=[],
                         board_touched=True, board=two)
    calls = []
    monkeypatch.setattr(W, "_raw_gaps", lambda st: calls.append(1) or ["export_csv is missing"])
    monkeypatch.setattr(_contract_gate, "_waived", lambda st, gaps: [])   # the model waived it
    assert W.board_done(st) and W.board_done(st)
    assert len(calls) == 1                                  # same code state: no re-index


def test_a_narrow_green_run_after_a_red_full_suite_does_not_close_the_board(monkeypatch):
    """click t5: board done fired on a green single-file run 4 s after the
    full suite failed."""
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _done_watch as DW
    monkeypatch.setenv("AIFORGE_CHAT_DONE_WATCH", "1")
    st = SimpleNamespace(board={"a": {"title": "A", "status": "done"},
                                "b": {"title": "B", "status": "done"}},
                         board_touched=True, tests_green=True, last_full_green=False,
                         last_full_test=("run_tests", {}), edits_made=3, dw_blocked_at=None,
                         dw_board_gaps=(3, False))
    assert not DW.board_done(st)
    st.last_full_green = True
    assert DW.board_done(st)


def test_a_red_check_on_final_marks_the_full_suite_red(monkeypatch):
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _outcomes as O
    monkeypatch.setattr(O, "_project_verify", lambda st, cwd: (False, "1 failed"))
    monkeypatch.setattr(O, "_verify_known_ok", lambda st, ok, out: (ok, out))
    monkeypatch.setattr(O, "_verify_on_final_enabled", lambda: True)
    monkeypatch.setattr(O, "content_fingerprint", lambda cwd: "fp")
    monkeypatch.setattr(O, "_verify_max_rounds", lambda: 2)
    st = SimpleNamespace(last_full_green=True, tests_green=True, last_green_fp=None,
                         edits_made=2, verify_rounds=0, convo=[], verify_fails=None)
    g = O._verify_fix(st, {"text": "done"}, "/tmp", False, None)
    try:
        while True:
            next(g)
    except StopIteration:
        pass
    except Exception:  # noqa: BLE001 — later send-back plumbing; the flag is set first
        pass
    assert st.last_full_green is False


def test_done_early_gets_one_review_pass_first(monkeypatch):
    """shopkit a2: board closed at 22 of 70 minutes with 37/60."""
    import time
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _done_watch as DW
    monkeypatch.setenv("AIFORGE_CHAT_DONE_WATCH", "1")
    monkeypatch.setattr(DW, "done_now", lambda st: False)
    monkeypatch.setattr(DW, "board_done", lambda st: True)
    monkeypatch.setattr(DW, "_initial_gaps", lambda st: 0)
    monkeypatch.setattr(DW, "_jobs_running", lambda st: False)
    big = {f"p{i}": {"title": "t", "status": "done", "from_request": True} for i in range(4)}

    def mk(left, **kw):
        return SimpleNamespace(edits_made=3, turn_deadline=time.monotonic() + left,
                               turn_budget_s=4000, extensions_used=0, board=big, goal="x",
                               convo=[{"role": "user", "content": "x"}], steers=[], **kw)
    st = mk(3000)
    out = list(DW.watch(st, 5))
    assert "time to spare" in out[0]["text"] and DW.REVIEW_NOTE in st.convo[-1]["content"]
    assert list(DW.watch(st, 6)) == []          # the review is at work
    out = list(DW.watch(st, 5 + DW.REVIEW_STEPS))   # no edit came: the done note after its room
    assert out and "every task-board item is done" in out[0]["text"]
    st = mk(3000)
    list(DW.watch(st, 5))
    st.edits_made = 4                           # the review landed an edit: done note at once
    assert "every task-board item is done" in list(DW.watch(st, 6))[0]["text"]
    late = mk(100)
    assert "every task-board item is done" in list(DW.watch(late, 5))[0]["text"]
    small = mk(3000)
    small.board = {f"m{i}": {"title": "t", "status": "done"} for i in range(5)}   # model-added
    assert "every task-board item is done" in list(DW.watch(small, 5))[0]["text"]
    extended = mk(3000, )
    extended.extensions_used = 1
    assert "every task-board item is done" in list(DW.watch(extended, 5))[0]["text"]


def test_a_plain_final_answer_early_gets_the_review_pass_once(monkeypatch):
    """shopkit a3: answered at 24 of 50 minutes (18/60), no board-done event."""
    import time
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _done_watch as DW
    big = {f"p{i}": {"title": "t", "status": "pending", "from_request": True} for i in range(4)}
    st = SimpleNamespace(edits_made=3, turn_deadline=time.monotonic() + 2000, turn_budget_s=3000,
                         extensions_used=0, board=big, goal="x", steers=[],
                         convo=[{"role": "user", "content": "x"}])

    def run(g):
        out = []
        try:
            while True:
                out.append(next(g))
        except StopIteration as e:
            return out, e.value
    monkeypatch.setattr(DW, "_applies", lambda st: True)
    assert run(DW.review_gate(st, {"text": "Should I also add refunds?"}))[1] is None
    out, sig = run(DW.review_gate(st, {"text": "FINAL: done"}))
    assert sig == "continue" and st.convo[-1]["content"] == DW.REVIEW_NOTE
    assert run(DW.review_gate(st, {"text": "FINAL: done"}))[1] is None      # once
    assert st.dw_gate_edits == 3
    late = SimpleNamespace(**{**vars(st), "dw_reviewed": False,
                              "turn_deadline": time.monotonic() + 600})
    assert run(DW.review_gate(late, {"text": "FINAL"}))[1] is None
    assert run(DW.review_gate(SimpleNamespace(**{**vars(st), "dw_reviewed": False}),
                              {"text": "F"}, builder="job"))[1] is None
