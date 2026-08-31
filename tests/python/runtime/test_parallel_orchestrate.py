"""Subtask orchestration: sequential builds, wave scheduling, retries, recursion.

Three schedulers live here and they trade off differently. SEQUENTIAL uses git
as an undo stack — commit when the failure count holds or drops, `reset --hard`
when it rises — so progress is monotonic in one tree. The SHARED worktree runs
waves in one tree (one git index, so strictly sequential) and keeps its branch
only when it holds unmerged work. The per-worktree path runs concurrently and
re-dispatches failures in fresh worktrees.

Everything below drives them with stub runners: the questions are ordering,
retry counts, revert conditions and what survives a Stop.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.runtime.parallel_subtasks import _orchestrate as orch


class _R:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


@pytest.fixture()
def git(monkeypatch):
    """Record git calls instead of running them."""
    calls: list = []

    def _git(args, cwd=None, **kw):
        calls.append(list(args))
        return _R()
    monkeypatch.setattr(orch, "_git", _git)
    return calls


# ─── the digest a sequential worker is handed ──────────────────────────


def test_the_digest_skips_the_workers_own_file_and_the_tests(monkeypatch):
    monkeypatch.setattr(orch, "_gather_sources", lambda cwd: [
        ("app/store.py", "REAL STORE\n"),
        ("app/cli.py", "REAL CLI\n"),
        ("tests/test_store.py", "TESTS\n"),
        ("app/conftest.py", "FIXTURES\n"),
    ])
    out = orch._existing_source_digest("/cwd", "app/store.py")
    assert "REAL CLI" in out
    assert "REAL STORE" not in out and "TESTS" not in out and "FIXTURES" not in out


def test_still_stubbed_and_empty_files_are_skipped(monkeypatch):
    monkeypatch.setattr(orch, "_gather_sources", lambda cwd: [
        ("app/a.py", f"# {orch._SCAFFOLD_MARK}\n"),
        ("app/b.py", "   \n"),
        ("app/c.py", "REAL\n"),
    ])
    out = orch._existing_source_digest("/cwd", "own.py")
    assert "REAL" in out and orch._SCAFFOLD_MARK not in out


def test_the_digest_is_budget_capped(monkeypatch):
    monkeypatch.setattr(orch, "_gather_sources", lambda cwd: [
        ("a.py", "x" * 100), ("b.py", "y" * 5000)])
    out = orch._existing_source_digest("/cwd", "own.py", budget=200)
    assert "x" * 100 in out and "y" * 5000 not in out


def test_go_style_test_files_are_skipped(monkeypatch):
    monkeypatch.setattr(orch, "_gather_sources", lambda cwd: [
        ("pkg/thing_test.py", "TESTS\n"), ("pkg/thing.py", "REAL\n")])
    assert "TESTS" not in orch._existing_source_digest("/cwd", "own.py")


# ─── sequential ordering ───────────────────────────────────────────────


def test_leaf_modules_are_built_first():
    """Fewest declared symbols first, as a proxy for "foundational"."""
    subs = [{"slug": "facade", "path": "a.py", "api": ["a", "b", "c"]},
            {"slug": "util", "path": "b.py", "api": ["x"]}]
    assert [s["slug"] for s in orch._sequential_order(subs)] == ["util", "facade"]


def test_the_order_is_stable_within_a_tier():
    subs = [{"slug": "a", "path": "a.py"}, {"slug": "b", "path": "b.py"}]
    assert [s["slug"] for s in orch._sequential_order(subs)] == ["a", "b"]


# ─── status + safe run ─────────────────────────────────────────────────


def test_status_is_only_reported_when_asked():
    orch._status(None, "slug", "running")           # must not raise


def test_status_passes_files_when_it_has_them():
    seen: list = []
    orch._status(lambda *a: seen.append(a), "s", "done", ["a.py"])
    orch._status(lambda *a: seen.append(a), "s", "running")
    assert seen == [("s", "done", ["a.py"]), ("s", "running")]


def test_a_runner_that_raises_becomes_a_failed_result():
    def _boom(_s, _cwd):
        raise RuntimeError("agent died")
    assert orch._safe_run(_boom, {}, "/cwd") == {"ok": False, "error": "agent died"}


# ─── writing the tests first ───────────────────────────────────────────


def test_test_subtasks_are_written_and_committed(git, monkeypatch):
    monkeypatch.setattr(orch, "_safe_run", lambda run_one, s, cwd: {"files": ["t.py"]})
    seen: list = []
    orch._write_test_subtasks("/cwd", [{"slug": "t1"}, {"slug": "t2"}], None,
                              lambda *a: seen.append(a), None)
    assert [c[0] for c in git] == ["add", "commit", "add", "commit"]
    assert [s[1] for s in seen] == ["running", "done", "running", "done"]


def test_stop_halts_test_writing(git, monkeypatch):
    monkeypatch.setattr(orch, "_safe_run",
                        lambda *a: pytest.fail("ran a subtask after Stop"))
    orch._write_test_subtasks("/cwd", [{"slug": "t1"}], None, None, lambda: True)
    assert git == []


# ─── one impl, committed or reverted ───────────────────────────────────


@pytest.fixture()
def impl_env(monkeypatch, git):
    monkeypatch.setattr(orch, "_prune_offplan_files", lambda cwd, subs: [])
    monkeypatch.setattr(orch, "_retries", lambda: 2)
    monkeypatch.setattr(orch, "_safe_run", lambda run_one, s, cwd: {"files": ["a.py"]})
    return git


def _fails(monkeypatch, counts):
    queue = list(counts)
    monkeypatch.setattr(orch, "_project_test_output", lambda cwd: (False, "out"))
    monkeypatch.setattr(orch, "_fail_count",
                        lambda out: queue.pop(0) if len(queue) > 1 else queue[0])


def test_an_improving_impl_is_committed(impl_env, monkeypatch):
    _fails(monkeypatch, [1])
    events: list = []
    committed, fails, _res = orch._build_one_impl(
        "/cwd", {"slug": "store"}, [], None, 3, None, events.append)
    assert committed is True and fails == 1
    assert events[0]["name"] == "committed"
    assert events[0]["args"]["status"] == "1 failing"


def test_holding_the_score_is_good_enough_to_commit(impl_env, monkeypatch):
    _fails(monkeypatch, [3])
    committed, fails, _ = orch._build_one_impl(
        "/cwd", {"slug": "s"}, [], None, 3, None, lambda _e: None)
    assert committed is True and fails == 3


def test_a_regression_is_reverted_and_retried(impl_env, monkeypatch):
    _fails(monkeypatch, [5, 5])
    events: list = []
    sub: dict = {"slug": "store"}
    committed, fails, _ = orch._build_one_impl(
        "/cwd", sub, [], None, 3, None, events.append)
    assert committed is False and fails == 3          # the caller's count stands
    assert [c[0] for c in impl_env].count("reset") == 2
    assert sub["_retry_error"] == "out"               # the error is fed back in
    assert "reverted, retry 1/2" in events[0]["text"]


def test_a_retry_that_recovers_is_committed(impl_env, monkeypatch):
    _fails(monkeypatch, [5, 2])
    committed, fails, _ = orch._build_one_impl(
        "/cwd", {"slug": "s"}, [], None, 3, None, lambda _e: None)
    assert committed is True and fails == 2


def test_a_tree_that_cannot_run_tests_yet_says_so(impl_env, monkeypatch):
    _fails(monkeypatch, [999])
    events: list = []
    orch._build_one_impl("/cwd", {"slug": "s"}, [], None, 999, None, events.append)
    assert events[0]["args"]["status"] == "tests can't run yet"


def test_stop_abandons_the_impl(impl_env, monkeypatch):
    monkeypatch.setattr(orch, "_safe_run",
                        lambda *a: pytest.fail("ran an impl after Stop"))
    committed, fails, res = orch._build_one_impl(
        "/cwd", {"slug": "s"}, [], None, 3, lambda: True, lambda _e: None)
    assert (committed, fails, res) == (False, 3, {})


# ─── every impl ────────────────────────────────────────────────────────


def test_each_impl_gets_the_current_tree_and_its_tests(monkeypatch):
    monkeypatch.setattr(orch, "_existing_source_digest", lambda cwd, path: "DIGEST")
    monkeypatch.setattr(orch, "_matching_tests_for", lambda cwd, path: "TESTS")
    monkeypatch.setattr(orch, "_build_one_impl",
                        lambda *a, **k: (True, 0, {"files": ["a.py"]}))
    subs = [{"slug": "store", "path": "app/store.py"}]
    done, failed = orch._build_impls("/cwd", subs, subs, None, 3, None, None,
                                     lambda _e: None)
    assert (done, failed) == (1, 0)
    assert subs[0]["_existing_files"] == "DIGEST" and subs[0]["_tests"] == "TESTS"


def test_a_failed_impl_is_counted_and_reported(monkeypatch):
    monkeypatch.setattr(orch, "_existing_source_digest", lambda cwd, path: "")
    monkeypatch.setattr(orch, "_matching_tests_for", lambda cwd, path: "")
    monkeypatch.setattr(orch, "_build_one_impl", lambda *a, **k: (False, 3, {}))
    seen: list = []
    done, failed = orch._build_impls("/cwd", [{"slug": "s", "path": "a.py"}], [],
                                     None, 3, lambda *a: seen.append(a), None,
                                     lambda _e: None)
    assert (done, failed) == (0, 1)
    assert seen[-1] == ("s", "failed")


def test_the_sequential_run_reports_its_baseline(monkeypatch):
    monkeypatch.setattr(orch, "_is_test_subtask", lambda s: "test" in s["slug"])
    monkeypatch.setattr(orch, "_write_test_subtasks", lambda *a: None)
    monkeypatch.setattr(orch, "_prune_offplan_files", lambda cwd, subs: [])
    monkeypatch.setattr(orch, "_project_test_output", lambda cwd: (False, "out"))
    monkeypatch.setattr(orch, "_fail_count", lambda out: 4)
    monkeypatch.setattr(orch, "_build_impls", lambda *a, **k: (1, 0))
    events: list = []
    agg = orch._run_sequential("/cwd", "base",
                               [{"slug": "test-a"}, {"slug": "impl-a", "path": "a.py"}],
                               None, emit=events.append)
    assert agg == {"ok": True, "total": 2, "done": 2, "failed": 0}
    assert "baseline 4 failing" in events[0]["text"]


def test_a_failed_impl_makes_the_sequential_run_not_ok(monkeypatch):
    monkeypatch.setattr(orch, "_is_test_subtask", lambda s: False)
    monkeypatch.setattr(orch, "_write_test_subtasks", lambda *a: None)
    monkeypatch.setattr(orch, "_prune_offplan_files", lambda cwd, subs: [])
    monkeypatch.setattr(orch, "_project_test_output", lambda cwd: (False, "out"))
    monkeypatch.setattr(orch, "_fail_count", lambda out: 0)
    monkeypatch.setattr(orch, "_build_impls", lambda *a, **k: (0, 1))
    agg = orch._run_sequential("/cwd", "base", [{"slug": "a", "path": "a.py"}], None)
    assert agg["ok"] is False and agg["failed"] == 1


# ─── which files a subtask owns ────────────────────────────────────────


def test_explicit_files_win():
    assert orch._files_of({"files": ["a.py", "b.py"], "path": "c.py"}) == {"a.py", "b.py"}


def test_a_scope_allowlist_counts_as_ownership():
    assert orch._files_of({"scope_allowlist_globs": ["app/**"]}) == {"app/**"}


def test_the_path_is_the_fallback():
    assert orch._files_of({"path": "c.py"}) == {"c.py"}


def test_a_goal_line_recovers_the_target_file():
    """A planner subtask carries only slug+goal — without this it would look
    like it owns NOTHING and could be scheduled beside a subtask editing the
    same file."""
    assert orch._files_of({"goal": "update config.yaml: add the key"}) == {"config.yaml"}


def test_a_prose_colon_is_not_a_file():
    assert orch._files_of({"goal": "Refactor: split the module"}) == set()


# ─── wave scheduling ───────────────────────────────────────────────────


def test_dependencies_order_the_waves():
    waves = orch.schedule_waves([
        {"slug": "b", "files": ["b.py"], "deps": ["a"]},
        {"slug": "a", "files": ["a.py"]},
    ])
    assert [[s["slug"] for s in w] for w in waves] == [["a"], ["b"]]


def test_disjoint_subtasks_share_a_wave():
    waves = orch.schedule_waves([{"slug": "a", "files": ["a.py"]},
                                 {"slug": "b", "files": ["b.py"]}])
    assert [[s["slug"] for s in w] for w in waves] == [["a", "b"]]


def test_two_subtasks_touching_one_file_are_serialized():
    """Never merge two edits to the same file — defer instead."""
    waves = orch.schedule_waves([{"slug": "a", "files": ["shared.py"]},
                                 {"slug": "b", "files": ["shared.py"]}])
    assert [[s["slug"] for s in w] for w in waves] == [["a"], ["b"]]


def test_a_dependency_cycle_still_makes_progress():
    waves = orch.schedule_waves([{"slug": "a", "files": ["a.py"], "deps": ["b"]},
                                 {"slug": "b", "files": ["b.py"], "deps": ["a"]}])
    assert sum(len(w) for w in waves) == 2


def test_a_dep_on_an_unknown_slug_is_ignored():
    waves = orch.schedule_waves([{"slug": "a", "files": ["a.py"], "deps": ["gone"]}])
    assert [[s["slug"] for s in w] for w in waves] == [["a"]]


def test_subtasks_without_a_slug_are_dropped():
    assert orch.schedule_waves([{"files": ["a.py"]}, "not a dict"]) == []


def test_a_fileless_subtask_never_blocks_a_wave():
    waves = orch.schedule_waves([{"slug": "think"}, {"slug": "a", "files": ["a.py"]}])
    assert [[s["slug"] for s in w] for w in waves] == [["think", "a"]]


# ─── recursion + retry budgets ─────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("3", 3), ("0", 1), ("junk", 2)])
def test_the_recursion_depth_cap(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_DECOMP_MAX_DEPTH", raw)
    assert orch._recurse_max() == expected


@pytest.mark.parametrize("raw,expected", [("4", 4), ("-1", 0), ("junk", 2)])
def test_the_retry_budget(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_DECOMP_RETRIES", raw)
    assert orch._decomp_retries() == expected


@pytest.mark.parametrize("raw,expected", [("2", 2), ("9", 5), ("-1", 0), ("junk", 1)])
def test_the_rerun_round_cap(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_PARALLEL_RERUN_ROUNDS", raw)
    assert orch._rerun_rounds() == expected


# ─── one attempt, validated ────────────────────────────────────────────


def test_a_final_answer_is_not_success_until_it_validates():
    """"the agent emitted a final answer" is NOT "it works"."""
    r = orch._attempt_subtask({}, "/wt", lambda s, wt: {"ok": True},
                              lambda s, wt: {"ok": False, "error": "tests red"})
    assert r == {"ok": False, "error": "tests red", "validated": False}


def test_a_validated_attempt_is_marked():
    r = orch._attempt_subtask({}, "/wt", lambda s, wt: {"ok": True, "files": ["a"]},
                              lambda s, wt: {"ok": True})
    assert r == {"ok": True, "files": ["a"], "validated": True}


def test_without_a_validator_the_runner_result_stands():
    assert orch._attempt_subtask({}, "/wt", lambda s, wt: {"ok": True}, None) == {"ok": True}


def test_a_failed_run_is_not_validated():
    assert orch._attempt_subtask({}, "/wt", lambda s, wt: {"ok": False},
                                 lambda s, wt: pytest.fail("validated a failed run")) == {
        "ok": False}


def test_a_runner_exception_is_a_failure():
    def _boom(_s, _wt):
        raise RuntimeError("agent died")
    assert orch._attempt_subtask({}, "/wt", _boom, None) == {"ok": False, "error": "agent died"}


def test_a_validator_exception_is_a_failure():
    def _boom(_s, _wt):
        raise RuntimeError("no toolchain")
    r = orch._attempt_subtask({}, "/wt", lambda s, wt: {"ok": True}, _boom)
    assert r["ok"] is False and "validate: no toolchain" in r["error"]


# ─── informed retries ──────────────────────────────────────────────────


@pytest.fixture()
def quiet_emit(monkeypatch):
    monkeypatch.setattr(orch, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_update", lambda *a, **k: None)


def test_each_retry_feeds_the_prior_error_back(monkeypatch, quiet_emit):
    monkeypatch.setenv("AIFORGE_DECOMP_RETRIES", "2")
    seen: list = []

    def _attempt(sub, wt, run_one, validate_one):
        seen.append(sub.get("_retry_error"))
        return {"ok": False, "error": "boom"}
    monkeypatch.setattr(orch, "_attempt_subtask", _attempt)
    sub: dict = {"slug": "s"}
    orch._attempt_with_retries(sub, "/wt", None, None, 1, "s", 0, None)
    assert seen == [None, "boom", "boom"]
    assert "_retry_error" not in sub          # cleaned up afterwards


def test_a_first_time_success_does_not_retry(monkeypatch, quiet_emit):
    calls = {"n": 0}

    def _attempt(*_a):
        calls["n"] += 1
        return {"ok": True}
    monkeypatch.setattr(orch, "_attempt_subtask", _attempt)
    orch._attempt_with_retries({"slug": "s"}, "/wt", None, None, 1, "s", 0, None)
    assert calls["n"] == 1


def test_stop_ends_the_retry_loop(monkeypatch, quiet_emit):
    calls = {"n": 0}

    def _attempt(*_a):
        calls["n"] += 1
        return {"ok": False, "error": "boom"}
    monkeypatch.setattr(orch, "_attempt_subtask", _attempt)
    orch._attempt_with_retries({"slug": "s"}, "/wt", None, None, 1, "s", 0,
                               lambda: True)
    assert calls["n"] == 1


# ─── recursion ─────────────────────────────────────────────────────────


def test_a_stuck_subtask_is_split_into_sub_agents(monkeypatch, quiet_emit):
    monkeypatch.setenv("AIFORGE_DECOMP_MAX_DEPTH", "2")
    monkeypatch.setattr(orch, "_decompose", lambda goal: [{"goal": "a"}, {"goal": "b"}])

    def _waves(wt, children, *a):
        a[-2].update({c["slug"]: {"ok": True} for c in children})
    monkeypatch.setattr(orch, "_run_wave_set", _waves)
    out = orch._recurse_subtask({"slug": "s", "goal": "big"}, "/wt", None, None,
                                None, 1, None, 0, "s")
    assert out == {"ok": True, "slug": "s", "recursed": True, "children": 2}


def test_recursion_stops_at_the_depth_cap(monkeypatch, quiet_emit):
    monkeypatch.setenv("AIFORGE_DECOMP_MAX_DEPTH", "1")
    monkeypatch.setattr(orch, "_decompose",
                        lambda goal: pytest.fail("decomposed past the cap"))
    assert orch._recurse_subtask({"slug": "s"}, "/wt", None, None, None, 1,
                                 None, 0, "s") is None


def test_a_subtask_that_will_not_split_is_not_recursed(monkeypatch, quiet_emit):
    monkeypatch.setenv("AIFORGE_DECOMP_MAX_DEPTH", "3")
    monkeypatch.setattr(orch, "_decompose", lambda goal: [{"goal": "only one"}])
    assert orch._recurse_subtask({"slug": "s"}, "/wt", None, None, None, 1,
                                 None, 0, "s") is None


def test_stop_prevents_recursion(monkeypatch, quiet_emit):
    monkeypatch.setenv("AIFORGE_DECOMP_MAX_DEPTH", "3")
    monkeypatch.setattr(orch, "_decompose",
                        lambda goal: pytest.fail("decomposed after Stop"))
    assert orch._recurse_subtask({"slug": "s"}, "/wt", None, None, None, 1,
                                 lambda: True, 0, "s") is None


def test_a_green_subtask_never_recurses(monkeypatch, quiet_emit):
    monkeypatch.setattr(orch, "_attempt_with_retries", lambda *a: {"ok": True})
    monkeypatch.setattr(orch, "_recurse_subtask",
                        lambda *a: pytest.fail("recursed a passing subtask"))
    assert orch._run_one_recursive({"slug": "s"}, "/wt", None, None, None, 1,
                                   None, 0) == {"ok": True, "slug": "s"}


def test_a_subtask_that_cannot_be_split_reports_its_failure(monkeypatch, quiet_emit):
    monkeypatch.setattr(orch, "_attempt_with_retries",
                        lambda *a: {"ok": False, "error": "boom"})
    monkeypatch.setattr(orch, "_recurse_subtask", lambda *a: None)
    assert orch._run_one_recursive({"slug": "s"}, "/wt", None, None, None, 1,
                                   None, 0) == {"ok": False, "error": "boom", "slug": "s"}


def test_waves_run_in_order_and_stop_on_cancel(monkeypatch, quiet_emit):
    monkeypatch.setattr(orch, "schedule_waves",
                        lambda subs: [[{"slug": "a"}], [{"slug": "b"}]])
    seen: list = []

    def _one(s, *a):
        seen.append(s["slug"])
        return {"ok": True}
    monkeypatch.setattr(orch, "_run_one_recursive", _one)
    results: dict = {}
    orch._run_wave_set("/wt", [], None, None, None, 1, None, results, 0)
    assert seen == ["a", "b"] and set(results) == {"a", "b"}

    seen.clear()
    orch._run_wave_set("/wt", [], None, None, None, 1, lambda: True, {}, 0)
    assert seen == []


# ─── the shared worktree ───────────────────────────────────────────────


@pytest.mark.parametrize("raw,on", [("1", True), ("true", True), ("yes", True),
                                    ("on", True), ("0", False), ("", False)])
def test_the_shared_worktree_is_opt_in(monkeypatch, raw, on):
    monkeypatch.setenv("AIFORGE_SHARED_WORKTREE", raw)
    assert orch._shared_worktree_enabled() is on


def test_the_shared_worktree_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AIFORGE_SHARED_WORKTREE", raising=False)
    assert orch._shared_worktree_enabled() is False


def test_a_branch_with_commits_is_ahead(monkeypatch):
    monkeypatch.setattr(orch, "_git", lambda args, cwd: _R("3\n"))
    assert orch._branch_is_ahead("/repo", "main", "shared") is True


def test_a_branch_with_nothing_on_it_is_not(monkeypatch):
    monkeypatch.setattr(orch, "_git", lambda args, cwd: _R("0\n"))
    assert orch._branch_is_ahead("/repo", "main", "shared") is False


def test_an_unknowable_branch_state_never_loses_work(monkeypatch):
    def _boom(args, cwd):
        raise RuntimeError("git broken")
    monkeypatch.setattr(orch, "_git", _boom)
    assert orch._branch_is_ahead("/repo", "main", "shared") is True


def test_integration_falls_back_to_a_plain_build(monkeypatch):
    monkeypatch.setattr(orch, "_build_or_test", lambda wt: {"ok": True})
    assert orch._shared_integration("/wt", None) == {"ok": True}


def test_a_supplied_integration_test_is_used():
    assert orch._shared_integration("/wt", lambda wt: {"ok": False}) == {"ok": False}


def test_an_integration_crash_is_a_failure():
    def _boom(_wt):
        raise RuntimeError("no toolchain")
    r = orch._shared_integration("/wt", _boom)
    assert r["ok"] is False and "no toolchain" in r["error"]


def test_the_worktree_is_removed_and_the_branch_deleted(monkeypatch, tmp_path, git):
    wt = tmp_path / "wt"
    wt.mkdir()
    orch._cleanup_shared("/repo", str(wt), "shared-1", keep=False, cancelled=False)
    assert ["branch", "-D", "shared-1"] in git
    assert ["worktree", "remove", "--force", str(wt)] in git


def test_a_branch_holding_unmerged_work_is_kept(monkeypatch, tmp_path, git):
    orch._cleanup_shared("/repo", str(tmp_path / "gone"), "shared-1",
                         keep=True, cancelled=True)
    assert not any(c[:2] == ["branch", "-D"] for c in git)


@pytest.mark.parametrize("kw,expected", [
    ({"done": 2, "cancelled": False, "conflicts": [], "integ": {"ok": True}},
     "shared worktree: 2/2 subtasks done; integration green"),
    ({"done": 1, "cancelled": False, "conflicts": [], "integ": {"ok": False}},
     "shared worktree: 1/2 subtasks done; integration FAILED"),
    ({"done": 1, "cancelled": False, "conflicts": ["shared"], "integ": {}},
     "shared worktree: 1/2 subtasks done; MERGE CONFLICT"),
    ({"done": 1, "cancelled": True, "conflicts": [], "integ": {}},
     "STOPPED — shared worktree: 1/2 subtasks done; partial work kept on "
     "branch, NOT merged"),
])
def test_the_shared_review_line(kw, expected):
    assert orch._shared_review([{}, {}], **kw) == expected
