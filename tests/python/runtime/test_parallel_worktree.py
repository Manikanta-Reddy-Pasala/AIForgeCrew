"""Per-subtask worktrees, validation, and conflict resolution.

Each subtask gets its OWN git worktree and branch, which is what makes the
parallel path safe: separate indexes, no shared tree. Two consequences are
pinned below. First, per-subtask validation can only be a SYNTAX check — one
file alone cannot build a project whose imports live in other worktrees — and
a file still carrying the scaffold marker must be REJECTED, or an LLM that
wrote nothing "succeeds". Second, a merge conflict is resolved hunk-by-hunk
with widening context rather than dropping the subtask's work, and anything
that still fails syntax rolls back rather than landing.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from aiforge_core.runtime.parallel_subtasks import _worktree as wt


class _R:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def git(monkeypatch):
    calls: list = []
    replies: dict = {}

    def _git(args, cwd=None):
        calls.append(list(args))
        for key, rep in replies.items():
            if key in args:
                return rep
        return _R()
    monkeypatch.setattr(wt, "_git", _git)
    _git.calls = calls
    _git.replies = replies
    return _git


# ─── switches ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,on", [("1", True), ("true", True), ("YES", True),
                                    ("on", True), ("0", False), ("no", False)])
def test_the_parallel_path_toggle(monkeypatch, raw, on):
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", raw)
    assert wt.enabled() is on


def test_parallel_subtasks_are_on_by_default(monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS", raising=False)
    assert wt.enabled() is True


@pytest.mark.parametrize("raw,expected", [("1", 1), ("8", 8), ("99", 8),
                                          ("0", 1), ("junk", 4)])
def test_the_worker_count_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", raw)
    assert wt._max_workers() == expected


def test_four_workers_by_default(monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS_MAX", raising=False)
    assert wt._max_workers() == 4


@pytest.mark.parametrize("raw,expected", [("0", 0), ("6", 6), ("9", 6), ("junk", 2)])
def test_the_subtask_retry_budget(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_SUBTASK_RETRIES", raw)
    assert wt._retries() == expected


# ─── naming ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,slug", [
    ("Build the LRU cache", "build-the-lru-cache"),
    ("app/store.py", "app-store-py"),
    ("!!!", "step"),
    ("", "step"),
])
def test_slugify(text, slug):
    assert wt._slugify(text) == slug


def test_a_slug_is_length_capped():
    assert len(wt._slugify("x" * 100)) == 40


def test_a_run_token_makes_the_branch_unique_per_run():
    """Two concurrent runs in one repo must not collide on a fixed name."""
    assert wt._branch_for("store", "main", "ab12") == "main-ab12-sub-store"
    assert wt._branch_for("store", "main") == "main-sub-store"


def test_unsafe_characters_are_scrubbed_from_a_branch():
    assert wt._branch_for("app/store.py", "main") == "main-sub-app-store-py"


# ─── creating a worktree ───────────────────────────────────────────────


def test_a_stale_worktree_and_branch_are_cleared_first(git, monkeypatch, tmp_path):
    monkeypatch.setattr(wt.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(wt.os.path, "isdir", lambda p: True)
    path, branch = wt._make_worktree(str(tmp_path), "main", "store", "ab12")
    assert branch == "main-ab12-sub-store"
    assert path.endswith(os.path.join(".aiforge-worktrees", "ab12-store"))
    assert git.calls[0][:2] == ["worktree", "remove"]
    assert git.calls[1][:2] == ["branch", "-D"]
    assert git.calls[2][:3] == ["worktree", "add", "-B"]


def test_a_failed_worktree_add_is_an_error(git, monkeypatch, tmp_path):
    git.replies["add"] = _R(stderr="fatal: already checked out", returncode=128)
    monkeypatch.setattr(wt.os, "makedirs", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="worktree add failed"):
        wt._make_worktree(str(tmp_path), "main", "store")


def test_the_worktree_is_reset_between_attempts(git):
    wt._reset_worktree("/wt", "main")
    assert git.calls == [["reset", "--hard", "main"], ["clean", "-fdx"]]


def test_uncommitted_work_is_committed(git):
    git.replies["status"] = _R(stdout=" M a.py\n")
    assert wt._commit_all("/wt", "store") is True
    assert any(c[0] == "commit" for c in git.calls)


def test_a_clean_tree_is_not_committed_again(git):
    git.replies["status"] = _R(stdout="")
    wt._commit_all("/wt", "store")
    assert not any(c[0] == "commit" for c in git.calls)


# ─── one attempt ───────────────────────────────────────────────────────


def test_a_crashing_agent_is_a_retryable_failure(git):
    def _boom(_s, _wt):
        raise RuntimeError("agent died")
    r = wt._attempt({}, "/wt", "s", _boom, None)
    assert r["ok"] is False
    assert r["ran"] is False
    assert "crash: agent died" in r["error"]


def test_a_crashing_validator_is_a_failure_not_an_exception(git):
    def _boom(_s, _wt):
        raise RuntimeError("no toolchain")
    r = wt._attempt({}, "/wt", "s", lambda s, w: {"ok": True}, _boom)
    assert r["ok"] is False
    assert "crash: no toolchain" in r["validation"]["error"]


def test_a_run_and_validate_that_both_pass(git):
    r = wt._attempt({}, "/wt", "s", lambda s, w: {"ok": True, "files": ["a.py"]},
                    lambda s, w: {"ok": True})
    assert r["ok"] is True
    assert r["ran"] is True
    assert r["validated"] is True
    assert r["detail"]["files"] == ["a.py"]


def test_a_failed_run_is_not_validated(git):
    r = wt._attempt({}, "/wt", "s", lambda s, w: {"ok": False},
                    lambda s, w: pytest.fail("validated a failed run"))
    assert r["ok"] is False


def test_work_is_committed_even_when_validation_fails(git):
    wt._attempt({}, "/wt", "s", lambda s, w: {"ok": True}, lambda s, w: {"ok": False})
    assert any(c[0] == "add" for c in git.calls)


# ─── informed retries ──────────────────────────────────────────────────


def test_the_retry_carries_the_previous_error():
    out = wt._retry_subtask({"slug": "s"}, {"error": "ImportError: x"}, 1)
    assert out["_retry_error"] == "ImportError: x"
    assert out["_retry_n"] == 1
    assert out["_too_big"] is False


def test_a_validation_error_is_used_when_there_is_no_run_error():
    out = wt._retry_subtask({}, {"validation": {"error": "syntax"}}, 1)
    assert out["_retry_error"] == "syntax"


def test_a_subtask_that_ran_out_of_budget_is_told_to_ship_a_core():
    """"stopped" means it was too big for one pass, not that it was wrong."""
    assert wt._retry_subtask({}, {"error": "(stopped: turn budget)"}, 1)["_too_big"] is True
    assert wt._retry_subtask({}, {"stopped": True}, 1)["_too_big"] is True


def test_a_failure_with_no_reason_still_says_something():
    assert "previous build/tests failed" in wt._retry_subtask({}, {}, 1)["_retry_error"]


def test_retries_reset_the_worktree_between_attempts(monkeypatch):
    monkeypatch.setenv("AIFORGE_SUBTASK_RETRIES", "2")
    monkeypatch.setattr(wt, "_emit", lambda *a: None)
    resets: list = []
    monkeypatch.setattr(wt, "_reset_worktree", lambda w, b: resets.append(b))
    monkeypatch.setattr(wt, "_attempt", lambda *a: {"ok": False, "error": "boom"})
    last, i = wt._run_with_retries({"slug": "s"}, "/wt", "s", "main", None, None, None)
    assert i == 2
    assert resets == ["main", "main"]
    assert last["ok"] is False


def test_a_passing_first_attempt_does_not_retry(monkeypatch):
    monkeypatch.setattr(wt, "_attempt", lambda *a: {"ok": True})
    monkeypatch.setattr(wt, "_reset_worktree",
                        lambda *a: pytest.fail("reset after a green attempt"))
    _last, i = wt._run_with_retries({"slug": "s"}, "/wt", "s", "main", None, None, None)
    assert i == 0


# ─── running a subtask end to end ──────────────────────────────────────


@pytest.fixture
def sub_env(monkeypatch):
    monkeypatch.setattr(wt, "_emit", lambda *a: None)
    monkeypatch.setattr(wt, "_make_worktree", lambda *a: ("/wt", "main-sub-s"))
    seen: list = []
    monkeypatch.setattr(wt, "_update",
                        lambda tid, slug, status, on_status=None, files=None:
                        seen.append(status))
    return seen


def test_a_subtask_queued_when_stop_is_pressed_never_starts(sub_env, monkeypatch):
    monkeypatch.setattr(wt, "_make_worktree",
                        lambda *a: pytest.fail("made a worktree after Stop"))
    r = wt._run_subtask("/repo", "main", None, {"slug": "s"}, None, None,
                        should_cancel=lambda: True)
    assert r == {"slug": "s", "ok": False, "cancelled": True, "branch": None}
    assert sub_env == ["cancelled"]


def test_a_worktree_that_cannot_be_made_fails_the_subtask(sub_env, monkeypatch):
    def _boom(*_a):
        raise RuntimeError("no disk")
    monkeypatch.setattr(wt, "_make_worktree", _boom)
    r = wt._run_subtask("/repo", "main", None, {"slug": "s"}, None, None)
    assert r["ok"] is False
    assert r["error"] == "no disk"
    assert r["branch"] is None


def test_a_finished_subtask_reports_its_branch_and_files(sub_env, monkeypatch):
    monkeypatch.setattr(wt, "_run_with_retries",
                        lambda *a: ({"ok": True, "ran": True, "validated": True,
                                     "detail": {"files": ["a.py"]}}, 0))
    r = wt._run_subtask("/repo", "main", None, {"slug": "s"}, None, None)
    assert r["ok"] is True
    assert r["branch"] == "main-sub-s"
    assert r["attempts"] == 1
    assert sub_env == ["running", "done"]


# ─── build/test gating ─────────────────────────────────────────────────


def test_the_compiler_output_buried_in_sub_results_is_recovered():
    assert "cannot find symbol" in wt._project_fail_detail(
        {"error": None, "results": [{"ok": False, "output": "cannot find symbol"}]})


def test_a_passing_sub_result_contributes_nothing():
    assert wt._project_fail_detail({"results": [{"ok": True, "output": "fine"}]}) is None


def test_a_non_dict_result_has_no_detail():
    assert wt._project_fail_detail("boom") == ""


def test_the_detail_is_tail_capped():
    assert len(wt._project_fail_detail({"error": "x" * 9000})) == 4000


@pytest.fixture
def runner(monkeypatch):
    import aiforge_core.runtime.tools.project_runner as pr
    state = {"stacks": ["python"], "has_tests": True, "res": {"ok": True}}
    monkeypatch.setattr(pr, "detect", lambda cwd: {"stacks": state["stacks"]})
    monkeypatch.setattr(pr, "_has_tests", lambda cwd, stacks: state["has_tests"])

    def _project(action, cwd):
        state["action"] = action
        return state["res"]
    monkeypatch.setattr(pr, "project", _project)
    return state


def test_no_project_means_nothing_to_gate(runner):
    runner["stacks"] = []
    assert wt._build_or_test("/wt") == {"ok": True, "via": "no-project",
                                        "note": "nothing to build/test"}


def test_failing_tests_never_pass_via_a_build_fallback(runner):
    runner["res"] = {"ok": False, "results": [{"ok": False, "output": "2 failed"}]}
    out = wt._build_or_test("/wt")
    assert out["ok"] is False
    assert out["via"] == "test"
    assert "2 failed" in out["detail"]


def test_a_project_without_tests_is_gated_on_the_build(runner):
    runner["has_tests"] = False
    out = wt._build_or_test("/wt")
    assert out == {"ok": True, "via": "build", "note": "no tests", "detail": None}


def test_a_runner_crash_is_a_failed_gate(monkeypatch):
    import aiforge_core.runtime.tools.project_runner as pr

    def _boom(_cwd):
        raise RuntimeError("detect died")
    monkeypatch.setattr(pr, "detect", _boom)
    assert wt._build_or_test("/wt") == {"ok": False, "error": "detect died"}


def test_the_integration_test_gates_the_whole_repo(monkeypatch):
    monkeypatch.setattr(wt, "_build_or_test", lambda root: {"ok": True, "via": "test"})
    assert wt.default_integration_test("/repo")["via"] == "test"


# ─── per-subtask validation ────────────────────────────────────────────


def test_a_written_valid_file_passes(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_STRICT_VALIDATE", raising=False)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert wt.default_validate_one({"path": "a.py"}, str(tmp_path)) == {
        "ok": True, "via": "syntax", "detail": None}


def test_a_file_that_was_never_written_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_STRICT_VALIDATE", raising=False)
    out = wt.default_validate_one({"path": "a.py"}, str(tmp_path))
    assert out["ok"] is False
    assert out["via"] == "written"


def test_an_empty_file_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_STRICT_VALIDATE", raising=False)
    (tmp_path / "a.py").write_text("")
    assert wt.default_validate_one({"path": "a.py"}, str(tmp_path))["ok"] is False


def test_an_untouched_scaffold_stub_is_rejected(tmp_path, monkeypatch):
    """The stub is syntax-valid by construction, so without this check a worker
    that wrote nothing would "succeed" and never retry."""
    monkeypatch.delenv("AIFORGE_PARALLEL_STRICT_VALIDATE", raising=False)
    (tmp_path / "a.py").write_text(f'"""Stub {wt._SCAFFOLD_MARK}"""\n')
    out = wt.default_validate_one({"path": "a.py"}, str(tmp_path))
    assert out["ok"] is False
    assert out["via"] == "stub"


def test_broken_syntax_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_STRICT_VALIDATE", raising=False)
    (tmp_path / "a.py").write_text("def (:\n")
    out = wt.default_validate_one({"path": "a.py"}, str(tmp_path))
    assert out["ok"] is False
    assert out["via"] == "syntax"


def test_a_pathless_subtask_has_nothing_to_validate(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_STRICT_VALIDATE", raising=False)
    assert wt.default_validate_one({}, str(tmp_path)) == {"ok": True, "via": "no-path"}


def test_strict_mode_runs_the_real_build(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_PARALLEL_STRICT_VALIDATE", "1")
    monkeypatch.setattr(wt, "_build_or_test", lambda w: {"ok": False, "via": "test"})
    assert wt.default_validate_one({"path": "a.py"}, str(tmp_path))["via"] == "test"


def test_a_guard_glitch_never_fails_a_subtask(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_STRICT_VALIDATE", raising=False)
    (tmp_path / "a.py").write_text("x = 1\n")
    import aiforge_core.runtime.syntax_guard as sg

    def _boom(*_a):
        raise RuntimeError("guard died")
    monkeypatch.setattr(sg, "validate_syntax", _boom)
    assert wt.default_validate_one({"path": "a.py"}, str(tmp_path)) == {
        "ok": True, "via": "written"}


# ─── status plumbing ───────────────────────────────────────────────────


def test_a_two_argument_status_callback_still_works():
    seen: list = []

    def _old_style(slug, status):
        seen.append((slug, status))
    wt._update(None, "s", "done", _old_style, ["a.py"])
    assert seen == [("s", "done")]


def test_a_status_callback_that_raises_is_swallowed():
    def _boom(*_a):
        raise RuntimeError("ui gone")
    wt._update(None, "s", "done", _boom)


def test_no_ticket_means_no_event(monkeypatch):
    wt._emit(None, "s", "kind", "body", {})     # must not raise


# ─── the dirty-workspace warning ───────────────────────────────────────


def test_a_dirty_tree_warns(git):
    git.replies["status"] = _R(stdout=" M app/store.py\n")
    assert "uncommitted changes" in wt._dirty_warning("/repo")


def test_a_clean_tree_does_not(git):
    assert wt._dirty_warning("/repo") is None


def test_the_agents_own_gitignore_edit_is_not_an_operator_change(git):
    """_ensure_git_workspace appends the artifact lines before this check, so
    without the exclude every default run would warn."""
    wt._dirty_warning("/repo")
    assert ":(exclude).gitignore" in git.calls[0]


def test_a_git_failure_never_warns(monkeypatch):
    def _boom(args, cwd):
        raise RuntimeError("no git")
    monkeypatch.setattr(wt, "_git", _boom)
    assert wt._dirty_warning("/repo") is None


# ─── conflict resolution ───────────────────────────────────────────────


_CONFLICT = ("def f():\n"
             "<<<<<<< HEAD\n    return 1\n=======\n    return 2\n>>>>>>> incoming\n")


def test_breadcrumbs_come_from_around_the_hunk():
    content = "a\nb\nc\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> in\nd\ne\n"
    # The hunk scanner replaced the DOTALL regex (a denial-of-service shape over
    # a whole file); it reports the same character span.
    hunk = wt._conflict_hunks(content)[0]
    above, below = wt._hunk_breadcrumbs(content, hunk["span"], 2)
    assert above == "b\nc\n"
    assert below.startswith("e")
    assert hunk["head"] == "x"
    assert hunk["incoming"] == "y"


@pytest.mark.parametrize("text,conflicted", [
    ("<<<<<<< HEAD", True), ("=======", True), (">>>>>>> x", True),
    ("clean code", False),
])
def test_leftover_markers_are_detected(text, conflicted):
    assert wt._still_conflicted(text) is conflicted


def test_the_syntax_check_fails_open(monkeypatch):
    import aiforge_core.runtime.syntax_guard as sg

    def _boom(*_a):
        raise RuntimeError("guard died")
    monkeypatch.setattr(sg, "validate_syntax", _boom)
    assert wt._syntax_ok("a.py", "def (:\n") is True


@pytest.fixture
def resolver(monkeypatch):
    import aiforge_core.llm.client as client
    seen: dict = {"prompts": []}

    def _complete(role, convo, **kw):
        seen["prompts"].append(convo[1]["content"])
        return seen.get("reply", "    return 3")
    monkeypatch.setattr(client, "complete", _complete)
    return seen


def test_a_hunk_is_resolved_from_minimal_context(resolver):
    out = wt._resolve_conflict_hunk("goal", "a.py", "    return 1", "    return 2",
                                    "def f():\n", "", 1)
    assert out == "    return 3"
    p = resolver["prompts"][0]
    assert "GOAL: goal" in p
    assert "[AMBIENT CODE ABOVE]" in p
    assert "CRITICAL" not in p


def test_a_retry_is_told_the_last_attempt_broke_syntax(resolver):
    wt._resolve_conflict_hunk("goal", "a.py", "a", "b", attempt=2)
    assert "CRITICAL" in resolver["prompts"][0]


def test_fences_and_markers_are_stripped_from_the_reply(resolver):
    resolver["reply"] = "```python\n<<<<<<< HEAD\n    return 3\n```"
    assert wt._resolve_conflict_hunk("g", "a.py", "a", "b") == "    return 3"


def test_the_resolved_blocks_indentation_survives(resolver):
    """The block is spliced back verbatim, so eating the first line's indent
    breaks the syntax check that immediately follows — and the hunk then falls
    back to HEAD, silently dropping the incoming side's work."""
    resolver["reply"] = "\n        return 3\n"
    assert wt._resolve_conflict_hunk("g", "a.py", "a", "b") == "        return 3"


def test_a_dead_model_resolves_nothing(monkeypatch):
    import aiforge_core.llm.client as client

    def _boom(*_a, **_kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(client, "complete", _boom)
    assert wt._resolve_conflict_hunk("g", "a.py", "a", "b") == ""


def test_an_unresolvable_hunk_keeps_head(monkeypatch):
    monkeypatch.setattr(wt, "_resolve_conflict_hunk", lambda *a, **k: "")
    out = wt._resolve_all_hunks(_CONFLICT, "goal", "a.py", 5, 1)
    assert "return 1" in out
    assert "return 2" not in out


def test_a_resolved_file_is_written(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(_CONFLICT)
    monkeypatch.setattr(wt, "_resolve_conflict_hunk", lambda *a, **k: "    return 3")
    assert wt._resolve_file_conflicts(str(tmp_path), "a.py", "goal") is True
    assert (tmp_path / "a.py").read_text() == "def f():\n    return 3\n"


def test_a_resolution_that_breaks_syntax_widens_and_retries(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(_CONFLICT)
    budgets: list = []

    def _resolve(goal, path, head, incoming, above="", below="", attempt=1):
        budgets.append(attempt)
        return "    return ((("
    monkeypatch.setattr(wt, "_resolve_conflict_hunk", _resolve)
    assert wt._resolve_file_conflicts(str(tmp_path), "a.py", "goal") is False
    assert budgets == [1, 2, 3]                      # widened, then gave up
    assert (tmp_path / "a.py").read_text() == _CONFLICT   # rolled back


def test_a_reply_that_keeps_the_markers_widens_too(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(_CONFLICT)
    monkeypatch.setattr(wt, "_resolve_conflict_hunk",
                        lambda *a, **k: "<<<<<<< HEAD\nstill conflicted")
    assert wt._resolve_file_conflicts(str(tmp_path), "a.py", "goal") is False


def test_an_unreadable_conflicted_file(tmp_path):
    assert wt._resolve_file_conflicts(str(tmp_path), "gone.py", "goal") is False


def test_every_conflicted_file_must_resolve(git, monkeypatch):
    git.replies["--diff-filter=U"] = _R(stdout="a.py\nb.py\n")
    monkeypatch.setattr(wt, "_spec_goal", lambda repo: "goal")
    monkeypatch.setattr(wt, "_resolve_file_conflicts",
                        lambda repo, f, goal: f == "a.py")
    assert wt._resolve_conflicts("/repo", "goal") is False


def test_all_resolved_files_are_staged(git, monkeypatch):
    git.replies["--diff-filter=U"] = _R(stdout="a.py\n")
    monkeypatch.setattr(wt, "_resolve_file_conflicts", lambda *a: True)
    assert wt._resolve_conflicts("/repo", "goal") is True
    assert ["add", "--", "a.py"] in git.calls


def test_nothing_conflicted_is_not_a_resolution(git):
    assert wt._resolve_conflicts("/repo", "goal") is False


# ─── merging ───────────────────────────────────────────────────────────


def test_a_clean_merge(git):
    assert wt._merge_branch("/repo", "main", "sub-a") == (True, "merged")


def test_a_conflict_is_resolved_rather_than_dropping_the_work(git, monkeypatch):
    git.replies["merge"] = _R(stdout="CONFLICT", returncode=1)
    monkeypatch.setattr(wt, "_resolve_conflicts", lambda repo, goal: True)
    monkeypatch.setattr(wt, "_spec_goal", lambda repo: "goal")
    ok, info = wt._merge_branch("/repo", "main", "sub-a")
    assert ok is True
    assert "auto-resolved" in info


def test_an_unresolvable_conflict_aborts_and_leaves_base_clean(git, monkeypatch):
    git.replies["merge"] = _R(stdout="CONFLICT in a.py", returncode=1)
    monkeypatch.setattr(wt, "_resolve_conflicts", lambda repo, goal: False)
    monkeypatch.setattr(wt, "_spec_goal", lambda repo: "goal")
    ok, info = wt._merge_branch("/repo", "main", "sub-a")
    assert ok is False
    assert "CONFLICT in a.py" in info
    assert ["merge", "--abort"] in git.calls


def test_auto_resolution_can_be_turned_off(git, monkeypatch):
    monkeypatch.setenv("AIFORGE_RESOLVE_CONFLICTS", "0")
    git.replies["merge"] = _R(stdout="CONFLICT", returncode=1)
    monkeypatch.setattr(wt, "_resolve_conflicts",
                        lambda *a: pytest.fail("resolved with the gate off"))
    assert wt._merge_branch("/repo", "main", "sub-a")[0] is False


def test_a_crash_during_resolution_still_aborts_cleanly(git, monkeypatch):
    git.replies["merge"] = _R(stdout="CONFLICT", returncode=1)

    def _boom(*_a):
        raise RuntimeError("resolver died")
    monkeypatch.setattr(wt, "_resolve_conflicts", _boom)
    monkeypatch.setattr(wt, "_spec_goal", lambda repo: "goal")
    assert wt._merge_branch("/repo", "main", "sub-a")[0] is False
    assert ["merge", "--abort"] in git.calls
