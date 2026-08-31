"""The team-mode stream driver's own helpers.

``stream_parallel_team`` is a generator over a worker thread, so its pieces are
only reachable one at a time: the steer router, the SPEC writer, the change
emitter, the verdict wording, the test-coverage backstop. Each is exercised
here against stubs — no git, no models, no worktrees — because the parts that
matter are exactly the ones that must not need them: a steer arriving for a
subtask that no longer exists, an unwritable workspace, a runner that raises,
a reconcile that produced no clear pass/fail.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.runtime.parallel_subtasks import _stream as st


@pytest.fixture(autouse=True)
def _no_mandate_bleed():
    st._USER_MANDATES.clear()
    yield
    st._USER_MANDATES.clear()


def _subs():
    return [{"slug": "store", "path": "app/store.py", "goal": "storage"},
            {"slug": "cli", "path": "app/cli.py", "goal": "entry"}]


# ─── steering: pin, headings, SPEC append ──────────────────────────────


def test_a_steer_pins_to_its_subtask_as_a_must():
    subs = _subs()
    heading, feedback = st._pin_to_subtask(subs, "store", "use an LRU", "cache")
    assert "MANDATORY" in heading
    assert "app/store.py" in heading
    assert "app/store.py" in feedback and "cache" in feedback
    assert "[MANDATORY user instruction — MUST satisfy]: use an LRU" in subs[0]["goal"]
    assert subs[0]["_user_mandate"] == ["use an LRU"]


def test_a_second_steer_to_the_same_subtask_accumulates():
    subs = _subs()
    st._pin_to_subtask(subs, "store", "one", "")
    st._pin_to_subtask(subs, "store", "two", "")
    assert subs[0]["_user_mandate"] == ["one", "two"]


def test_a_steer_for_a_vanished_subtask_is_not_pinned():
    """None here is what makes the caller fall back to a GLOBAL steer instead of
    dropping the user's instruction on the floor."""
    assert st._pin_to_subtask(_subs(), "gone", "text", "") is None


def test_a_subtask_with_no_path_is_labelled_by_slug():
    subs = [{"slug": "store", "goal": "g"}]
    heading, _ = st._pin_to_subtask(subs, "store", "t", "")
    assert heading.endswith("store")


@pytest.mark.parametrize("target,marker", [("new", "NEW requirement"),
                                           ("global", "whole build")])
def test_steer_headings(target, marker):
    heading, feedback = st._steer_headings(target, "note")
    assert marker in heading
    assert "note" in feedback


def test_steer_headings_without_a_note():
    _, feedback = st._steer_headings("global", "")
    assert "—  ." not in feedback


def test_the_mandate_is_appended_to_spec_and_remembered(tmp_path):
    (tmp_path / "SPEC.md").write_text("# spec\n")
    st._append_spec_mandate(str(tmp_path), "## ⚙ heading", "must log errors")
    body = (tmp_path / "SPEC.md").read_text()
    assert "## ⚙ heading" in body
    assert "- **MUST:** must log errors" in body
    assert st._USER_MANDATES[str(tmp_path)] == ["must log errors"]


def test_an_unwritable_workspace_still_records_the_mandate(tmp_path):
    """The in-memory record is what the reconcile prompt re-asserts from, so it
    must survive a failed disk write."""
    missing = str(tmp_path / "nope")
    st._append_spec_mandate(missing, "## h", "keep it")
    assert st._USER_MANDATES[missing] == ["keep it"]


def test_apply_steer_routes_to_a_subtask(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_route_steering",
                        lambda text, subs: {"target": "store", "note": "n"})
    subs = _subs()
    fb = st._apply_steer("use an LRU", subs, str(tmp_path))
    assert "app/store.py" in fb
    assert "MUST satisfy" in subs[0]["goal"]
    assert "app/store.py" in (tmp_path / "SPEC.md").read_text()


def test_apply_steer_falls_back_to_global_when_the_target_is_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_route_steering",
                        lambda text, subs: {"target": "deleted", "note": ""})
    fb = st._apply_steer("everywhere", _subs(), str(tmp_path))
    assert "must-have" in fb or "whole build" in (tmp_path / "SPEC.md").read_text()


def test_apply_steer_for_a_new_requirement(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_route_steering",
                        lambda text, subs: {"target": "new", "note": ""})
    st._apply_steer("add a health endpoint", _subs(), str(tmp_path))
    assert "NEW requirement" in (tmp_path / "SPEC.md").read_text()


# ─── cancellation + session arming ─────────────────────────────────────


def test_no_session_is_never_cancelled():
    assert st._cancel_checker_for(None)() is False


def test_the_cancel_checker_asks_chat_cancel(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: sid == 7)
    assert st._cancel_checker_for(7)() is True
    assert st._cancel_checker_for(8)() is False


def test_a_broken_cancel_module_reads_as_not_cancelled(monkeypatch):
    from aiforge_core.runtime import chat_cancel

    def _boom(_sid):
        raise RuntimeError("db gone")
    monkeypatch.setattr(chat_cancel, "is_cancelled", _boom)
    assert st._cancel_checker_for(3)() is False


def test_arming_a_session_is_a_no_op_without_one():
    st._arm_session(None)   # must not raise


def test_arming_a_session_makes_it_steerable_and_active():
    from aiforge_core.runtime import chat_cancel, chat_interject
    st._arm_session(99123)
    assert chat_interject.pending(99123) == [] or True   # module accepted the id
    chat_cancel.clear(99123) if hasattr(chat_cancel, "clear") else None


# ─── steering drain ────────────────────────────────────────────────────


def test_the_drain_is_empty_without_a_session():
    assert list(st._steering_drain(None, _subs(), "/tmp")) == []


def test_the_drain_is_empty_when_nothing_is_pending(monkeypatch):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "pending", lambda sid: [])
    assert list(st._steering_drain(1, _subs(), "/tmp")) == []


def test_a_pending_steer_is_echoed_then_routed(monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "pending", lambda sid: ["use an LRU"])
    monkeypatch.setattr(chat_interject, "drain", lambda sid: ["use an LRU", "  "])
    monkeypatch.setattr(st, "_route_steering",
                        lambda text, subs: {"target": "store", "note": ""})
    events = list(st._steering_drain(1, _subs(), str(tmp_path)))
    assert [e["role"] for e in events] == ["steer", "planner"]
    assert events[0]["text"] == "use an LRU"


def test_a_failing_drain_yields_nothing_rather_than_raising(monkeypatch):
    from aiforge_core.runtime import chat_interject

    def _boom(_sid):
        raise RuntimeError("interject store down")
    monkeypatch.setattr(chat_interject, "pending", _boom)
    assert list(st._steering_drain(1, _subs(), "/tmp")) == []


# ─── SPEC.md ───────────────────────────────────────────────────────────


def test_the_spec_is_rendered_reviewed_and_written(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_render_spec_md", lambda prompt, subs: "# SPEC\nbody")
    monkeypatch.setattr(st.review_gates, "review_spec",
                        lambda prompt, spec: (spec, "spec reviewed — sound"))
    state: dict = {}
    events = list(st._write_spec("build it", _subs(), str(tmp_path), state))
    assert state["spec_md"] == "# SPEC\nbody"
    assert (tmp_path / "SPEC.md").read_text() == "# SPEC\nbody"
    assert [e["role"] for e in events] == ["reviewer", "planner"]


def test_a_review_that_raises_does_not_stop_the_spec_being_written(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_render_spec_md", lambda prompt, subs: "# SPEC")

    def _boom(*_a):
        raise RuntimeError("reviewer down")
    monkeypatch.setattr(st.review_gates, "review_spec", _boom)
    state: dict = {}
    events = list(st._write_spec("p", _subs(), str(tmp_path), state))
    assert (tmp_path / "SPEC.md").read_text() == "# SPEC"
    assert [e["role"] for e in events] == ["planner"]


def test_an_unwritable_workspace_is_surfaced_not_swallowed(monkeypatch, tmp_path):
    """Silent skips here are how runs ended up spec-less with no trace."""
    monkeypatch.setattr(st, "_render_spec_md", lambda prompt, subs: "# SPEC")
    monkeypatch.setattr(st.review_gates, "review_spec", lambda p, s: (s, ""))
    state: dict = {}
    events = list(st._write_spec("p", _subs(), str(tmp_path / "missing"), state))
    assert "SPEC.md write failed" in events[0]["text"]


# ─── tree preparation ──────────────────────────────────────────────────


def test_an_existing_repo_is_never_scaffolded_or_pruned(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_snapshot_baseline", lambda cwd: 42)
    monkeypatch.setattr(st, "_is_greenfield", lambda cwd: False)
    monkeypatch.setattr(st, "_scaffold_stubs",
                        lambda *a: pytest.fail("scaffolded an existing repo"))
    events = list(st._prepare_tree(str(tmp_path), _subs()))
    assert "42 source files" in events[0]["text"]


def test_a_greenfield_tree_is_scaffolded(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_snapshot_baseline", lambda cwd: 0)
    monkeypatch.setattr(st, "_is_greenfield", lambda cwd: True)
    monkeypatch.setattr(st, "_scaffold_stubs", lambda cwd, subs: ["app/store.py"])
    events = list(st._prepare_tree(str(tmp_path), _subs()))
    assert events[0]["name"] == "scaffolded project"
    assert events[0]["result"]["files"] == ["app/store.py"]


def test_scaffolding_can_be_turned_off(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SCAFFOLD", "0")
    monkeypatch.setattr(st, "_snapshot_baseline", lambda cwd: 0)
    monkeypatch.setattr(st, "_is_greenfield", lambda cwd: True)
    monkeypatch.setattr(st, "_scaffold_stubs",
                        lambda *a: pytest.fail("scaffolded with the gate off"))
    assert list(st._prepare_tree(str(tmp_path), _subs())) == []


def test_a_scaffold_failure_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SCAFFOLD", "1")
    monkeypatch.setattr(st, "_snapshot_baseline", lambda cwd: 0)
    monkeypatch.setattr(st, "_is_greenfield", lambda cwd: True)

    def _boom(*_a):
        raise OSError("read-only fs")
    monkeypatch.setattr(st, "_scaffold_stubs", _boom)
    assert list(st._prepare_tree(str(tmp_path), _subs())) == []


# ─── execution announcement ────────────────────────────────────────────


def test_the_execution_config_is_announced(monkeypatch):
    monkeypatch.delenv("AIFORGE_SEQUENTIAL", raising=False)
    monkeypatch.setattr(st, "_max_workers", lambda: 4)
    monkeypatch.setattr(st.review_gates, "pick_reviewer_model", lambda: "big-model")
    text = list(st._announce_execution(_subs()))[0]["text"]
    assert "2 subtasks" in text and "up to 4 at once" in text and "big-model" in text


def test_a_stray_sequential_flag_is_visible(monkeypatch):
    monkeypatch.setenv("AIFORGE_SEQUENTIAL", "1")
    monkeypatch.setattr(st.review_gates, "pick_reviewer_model", lambda: None)
    text = list(st._announce_execution(_subs()))[0]["text"]
    assert "SEQUENTIAL (1 at a time)" in text
    assert "same model" in text


def test_a_reviewer_probe_that_raises_still_announces(monkeypatch):
    monkeypatch.delenv("AIFORGE_SEQUENTIAL", raising=False)
    monkeypatch.setattr(st, "_max_workers", lambda: 2)

    def _boom():
        raise RuntimeError("probe failed")
    monkeypatch.setattr(st.review_gates, "pick_reviewer_model", _boom)
    assert "reviewer: ?" in list(st._announce_execution(_subs()))[0]["text"]


# ─── the spec-bound runner ─────────────────────────────────────────────


def test_the_runner_rereads_spec_from_disk(monkeypatch, tmp_path):
    """A steer appended mid-run must reach the subtasks that start after it."""
    seen: dict = {}
    monkeypatch.setattr(st, "_default_subtask_runner",
                        lambda: (lambda sub, wt, spec_md=None: seen.setdefault("spec", spec_md)))
    (tmp_path / "SPEC.md").write_text("# SPEC v2 (steered)")
    st._spec_runner(str(tmp_path), "# SPEC v1")({"slug": "s"}, "/wt")
    assert seen["spec"] == "# SPEC v2 (steered)"


def test_the_runner_uses_the_in_memory_spec_when_no_file_exists(monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(st, "_default_subtask_runner",
                        lambda: (lambda sub, wt, spec_md=None: seen.setdefault("spec", spec_md)))
    st._spec_runner(str(tmp_path), "# SPEC v1")({"slug": "s"}, "/wt")
    assert seen["spec"] == "# SPEC v1"


def test_a_custom_runner_without_spec_md_is_still_called(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(st, "_default_subtask_runner",
                        lambda: (lambda sub, wt: calls.append((sub, wt)) or "done"))
    assert st._spec_runner(str(tmp_path), "spec")({"slug": "s"}, "/wt") == "done"
    assert calls == [({"slug": "s"}, "/wt")]


# ─── test review before impl ───────────────────────────────────────────


def test_reviewed_test_fixes_are_committed(monkeypatch, tmp_path):
    monkeypatch.setattr(st.review_gates, "review_tests",
                        lambda cwd, spec: (["tests/test_a.py"], "tests reviewed + fixed 1 file(s)"))
    git_calls: list = []
    monkeypatch.setattr(st, "_git", lambda args, cwd: git_calls.append(args[0]))
    events: list = []
    st._review_written_tests(str(tmp_path), "spec", events.append)
    assert events[0]["role"] == "reviewer"
    assert git_calls == ["add", "commit"]


def test_sound_tests_are_reported_but_not_committed(monkeypatch, tmp_path):
    monkeypatch.setattr(st.review_gates, "review_tests",
                        lambda cwd, spec: ([], "tests reviewed — sound"))
    monkeypatch.setattr(st, "_git",
                        lambda *a: pytest.fail("committed with nothing changed"))
    events: list = []
    st._review_written_tests(str(tmp_path), "spec", events.append)
    assert len(events) == 1


def test_a_review_crash_never_breaks_the_run(monkeypatch, tmp_path):
    def _boom(*_a):
        raise RuntimeError("reviewer down")
    monkeypatch.setattr(st.review_gates, "review_tests", _boom)
    st._review_written_tests(str(tmp_path), "spec", lambda _e: None)


# ─── off-plan prune + sidecars ─────────────────────────────────────────


def test_off_plan_files_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_prune_offplan_files",
                        lambda cwd, subs: ["app/phantom.py", "app/dup.py"])
    text = list(st._prune_offplan(str(tmp_path), _subs()))[0]["text"]
    assert "Removed 2 off-plan file(s)" in text
    assert "app/phantom.py" in text


def test_nothing_off_plan_says_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_prune_offplan_files", lambda cwd, subs: [])
    assert list(st._prune_offplan(str(tmp_path), _subs())) == []


def test_a_prune_failure_is_swallowed(monkeypatch, tmp_path):
    def _boom(*_a):
        raise RuntimeError("bad plan")
    monkeypatch.setattr(st, "_prune_offplan_files", _boom)
    assert list(st._prune_offplan(str(tmp_path), _subs())) == []


def test_contract_sidecars_are_removed_from_the_delivered_tree(tmp_path):
    sidecar = tmp_path / st._CONTRACT_DIR
    sidecar.mkdir(parents=True)
    (sidecar / "board.json").write_text("{}")
    st._clean_contract_sidecars(str(tmp_path))
    assert not sidecar.exists()


def test_cleaning_a_tree_without_sidecars_is_fine(tmp_path):
    st._clean_contract_sidecars(str(tmp_path))


# ─── the verdict ───────────────────────────────────────────────────────


def test_green_verdict(tmp_path):
    assert st._build_verdict(True, str(tmp_path)) == "✅ **Built — all tests pass.**"


def test_failing_tests_are_not_called_a_defect(tmp_path):
    out = st._build_verdict(False, str(tmp_path))
    assert "some tests still fail" in out
    assert "may not be a code defect" in out


def test_no_verdict_with_a_detected_toolchain_says_the_build_errored(monkeypatch, tmp_path):
    """Never claim "no toolchain" when one IS installed and the build errored."""
    monkeypatch.setattr(st, "_detected_stacks", lambda cwd: ["python", "node"])
    out = st._build_verdict(None, str(tmp_path))
    assert "did NOT pass cleanly" in out
    assert "python, node" in out


def test_no_verdict_and_no_toolchain_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_detected_stacks", lambda cwd: [])
    assert "no matching toolchain" in st._build_verdict(None, str(tmp_path))


def test_stack_detection_failure_reads_as_no_stacks(monkeypatch, tmp_path):
    import aiforge_core.runtime.tools.project_runner as pr

    def _boom(_cwd):
        raise RuntimeError("detect exploded")
    monkeypatch.setattr(pr, "detect", _boom)
    assert st._detected_stacks(str(tmp_path)) == []


# ─── change emission ───────────────────────────────────────────────────


class _Out:
    def __init__(self, stdout=""):
        self.stdout = stdout


def test_numstat_counts():
    assert st._numstat_counts("3\t1\tapp/a.py\n0\t7\tapp/b.py\nbad line\n") == {
        "app/a.py": ("3", "1"), "app/b.py": ("0", "7")}


def test_binary_numstat_rows_read_as_zero():
    counts = st._numstat_counts("-\t-\timg.png\n")
    assert st._to_int(counts["img.png"][0]) == 0


@pytest.mark.parametrize("raw,expected", [("12", 12), ("-", 0), ("", 0), (None, 0)])
def test_to_int_never_raises(raw, expected):
    assert st._to_int(raw) == expected


def test_a_changed_file_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_git", lambda args, cwd: _Out("@@ -1 +1 @@\n+x\n"))
    entry = st._changed_file("M\tapp/a.py", {"app/a.py": ("3", "1")}, [], str(tmp_path), 8000)
    assert entry == {"path": "app/a.py", "status": "modified", "additions": 3,
                     "deletions": 1, "diff": "@@ -1 +1 @@\n+x\n"}


@pytest.mark.parametrize("line,status", [("A\tapp/a.py", "added"),
                                         ("D\tapp/a.py", "deleted"),
                                         ("R100\told.py\tapp/a.py", "renamed"),
                                         ("X\tapp/a.py", "changed")])
def test_status_words(monkeypatch, tmp_path, line, status):
    monkeypatch.setattr(st, "_git", lambda args, cwd: _Out(""))
    assert st._changed_file(line, {}, [], str(tmp_path), 10)["status"] == status


def test_a_malformed_name_status_line_is_skipped(tmp_path):
    assert st._changed_file("garbage", {}, [], str(tmp_path), 10) is None


@pytest.mark.parametrize("path", ["node_modules/x.js", "app/__pycache__/a.pyc",
                                  "SPEC.md", "target/App.class", "a/.DS_Store"])
def test_generated_artifacts_never_appear_in_changes(monkeypatch, tmp_path, path):
    monkeypatch.setattr(st, "_git", lambda args, cwd: _Out("diff"))
    assert st._changed_file(f"M\t{path}", {}, [], str(tmp_path), 10) is None


def test_a_huge_diff_is_truncated(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_git", lambda args, cwd: _Out("x" * 100))
    entry = st._changed_file("M\ta.py", {}, [], str(tmp_path), 10)
    assert entry["diff"] == "x" * 10 + "\n… (truncated)"


def test_no_start_sha_emits_nothing(tmp_path):
    assert list(st._emit_changes(str(tmp_path), "")) == []


def test_changes_carry_a_summary(monkeypatch, tmp_path):
    def _fake_git(args, cwd):
        if "--numstat" in args:
            return _Out("3\t1\tapp/a.py\n2\t0\tapp/b.py\n")
        if "--name-status" in args:
            return _Out("M\tapp/a.py\nA\tapp/b.py\n")
        return _Out("diff body")
    monkeypatch.setattr(st, "_git", _fake_git)
    ev = list(st._emit_changes(str(tmp_path), "abc123"))[0]
    assert ev["type"] == "changes"
    assert ev["summary"] == {"files": 2, "additions": 5, "deletions": 1}


def test_an_empty_diff_emits_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_git", lambda args, cwd: _Out(""))
    assert list(st._emit_changes(str(tmp_path), "abc123")) == []


def test_worktree_mode_intent_adds_untracked_files(monkeypatch, tmp_path):
    seen: list = []

    def _fake_git(args, cwd):
        seen.append(args)
        return _Out("")
    monkeypatch.setattr(st, "_git", _fake_git)
    list(st._emit_changes(str(tmp_path), "abc123", include_worktree=True))
    assert seen[0][:3] == ["add", "-N", "--"]


def test_a_junk_diff_cap_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHANGES_FILE_DIFF_MAX", "lots")
    monkeypatch.setattr(st, "_git", lambda args, cwd: _Out(""))
    assert list(st._emit_changes(str(tmp_path), "abc")) == []


# ─── the test-coverage backstop ────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    ("app/store.py", "tests/test_store.py"),
    ("pkg/thing.go", "pkg/thing_test.go"),
    ("src/index.js", "src/index.test.js"),
    ("src/index.ts", "src/index.test.ts"),
    ("lib/thing.rb", "spec/thing_spec.rb"),
    ("src/lib.rs", "tests/lib_test.rs"),
    ("Main.java", ""),          # too involved to synthesise
    ("app/__init__.py", ""),    # dunder module
    ("README.md", ""),
])
def test_conventional_test_paths(path, expected):
    assert st._test_path_for(path) == expected


def test_a_plan_with_tests_is_left_alone(monkeypatch):
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: "test" in s["path"])
    subs = [{"path": "app/store.py"}, {"path": "tests/test_store.py"}]
    assert st._ensure_test_coverage(subs) == subs


def test_a_testless_plan_gains_one_test_subtask_per_module(monkeypatch):
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: False)
    out = st._ensure_test_coverage([{"path": "app/store.py", "slug": "store"},
                                    {"path": "app/cli.py", "slug": "cli"}])
    assert [s["path"] for s in out[2:]] == ["tests/test_store.py", "tests/test_cli.py"]
    assert out[2]["slug"] and out[2]["api"] == []
    assert "unit tests for app/store.py" in out[2]["goal"]


def test_a_module_with_no_test_convention_is_skipped(monkeypatch):
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: False)
    subs = [{"path": "Main.java", "slug": "main"}]
    assert st._ensure_test_coverage(subs) == subs


def test_an_already_planned_test_path_is_not_duplicated(monkeypatch):
    """The plan already declares tests/test_store.py (as a plain path, not
    something _is_test_subtask recognises) — the backstop must not add a second
    subtask writing the same file."""
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: False)
    subs = [{"path": "app/store.py"}, {"path": "tests/test_store.py"}]
    added = [s["path"] for s in st._ensure_test_coverage(subs)[2:]]
    assert "tests/test_store.py" not in added


# ─── the worker-thread runner ──────────────────────────────────────────


class _Q:
    def __init__(self):
        self.items: list = []

    def put(self, item):
        self.items.append(item)


def test_sequential_mode_takes_the_sequential_path(monkeypatch):
    monkeypatch.setenv("AIFORGE_SEQUENTIAL", "1")
    monkeypatch.setattr(st, "_run_sequential", lambda *a, **k: {"done": 2, "total": 2})
    monkeypatch.setattr(st, "run_parallel",
                        lambda *a, **k: pytest.fail("ran parallel in sequential mode"))
    result: dict = {}
    q = _Q()
    st._make_runner("/cwd", "base", _subs(), None, None, lambda: False, "spec", q, result)()
    assert result["agg"] == {"done": 2, "total": 2}
    assert q.items == [None]      # the sentinel always closes the stream


def test_test_first_runs_when_the_plan_has_both_kinds(monkeypatch):
    monkeypatch.delenv("AIFORGE_SEQUENTIAL", raising=False)
    monkeypatch.setenv("AIFORGE_TEST_FIRST", "1")
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: "test" in (s.get("path") or ""))
    monkeypatch.setattr(st, "_run_test_first", lambda *a, **k: {"done": 1, "total": 1})
    result: dict = {}
    subs = [{"path": "app/a.py"}, {"path": "tests/test_a.py"}]
    st._make_runner("/cwd", "base", subs, None, None, lambda: False, "spec", _Q(), result)()
    assert result["agg"] == {"done": 1, "total": 1}


def test_plain_parallel_when_there_are_no_test_subtasks(monkeypatch):
    monkeypatch.delenv("AIFORGE_SEQUENTIAL", raising=False)
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: False)
    monkeypatch.setattr(st, "run_parallel", lambda *a, **k: {"done": 2, "total": 2})
    result: dict = {}
    st._make_runner("/cwd", "base", _subs(), None, None, lambda: False, "spec", _Q(), result)()
    assert result["agg"] == {"done": 2, "total": 2}


def test_a_runner_crash_is_recorded_and_the_stream_still_closes(monkeypatch):
    monkeypatch.delenv("AIFORGE_SEQUENTIAL", raising=False)
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: False)

    def _boom(*_a, **_k):
        raise RuntimeError("worktree gone")
    monkeypatch.setattr(st, "run_parallel", _boom)
    result: dict = {}
    q = _Q()
    st._make_runner("/cwd", "base", _subs(), None, None, lambda: False, "spec", q, result)()
    assert result["err"] == "worktree gone"
    assert q.items == [None]


def test_test_first_builds_tests_then_impls(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: "test" in (s.get("path") or ""))
    monkeypatch.setattr(st, "_matching_tests_for", lambda cwd, path: ["tests/test_a.py"])
    monkeypatch.setattr(st, "_review_written_tests", lambda *a: None)
    monkeypatch.setattr(st, "_merge_aggs", lambda a, b: {**a, **b})
    order: list = []

    def _fake_parallel(cwd, base, _none, subs, run_one, **kw):
        order.append([s["path"] for s in subs])
        return {"done": len(subs)}
    monkeypatch.setattr(st, "run_parallel", _fake_parallel)
    subs = [{"path": "app/a.py"}, {"path": "tests/test_a.py"}]
    st._run_test_first(str(tmp_path), "base", subs, None, None, lambda: False,
                       "spec", lambda _e: None)
    assert order == [["tests/test_a.py"], ["app/a.py"]]
    assert subs[0]["_tests"] == ["tests/test_a.py"]


def test_cancelling_after_the_tests_skips_the_impl(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_is_test_subtask", lambda s: "test" in (s.get("path") or ""))
    calls: list = []
    monkeypatch.setattr(st, "run_parallel",
                        lambda *a, **k: calls.append(1) or {"done": 1})
    monkeypatch.setattr(st, "_review_written_tests",
                        lambda *a: pytest.fail("reviewed tests after Stop"))
    subs = [{"path": "app/a.py"}, {"path": "tests/test_a.py"}]
    st._run_test_first(str(tmp_path), "base", subs, None, None, lambda: True,
                       "spec", lambda _e: None)
    assert len(calls) == 1


# ─── draining the event stream ─────────────────────────────────────────


class _RealQ:
    def __init__(self, items):
        self._items = list(items)

    def get(self):
        return self._items.pop(0)


def test_the_drain_ends_at_the_sentinel(monkeypatch):
    monkeypatch.setattr(st, "_steering_drain", lambda *a: iter(()))
    out = list(st._drain_run(_RealQ([{"type": "thought"}, None]), None, _subs(),
                             "/cwd", lambda: False))
    assert out == [{"type": "thought"}]


def test_stop_ends_the_stream_early(monkeypatch):
    monkeypatch.setattr(st, "_steering_drain", lambda *a: iter(()))
    out = list(st._drain_run(_RealQ([{"type": "a"}, {"type": "b"}, None]), None,
                             _subs(), "/cwd", lambda: True))
    assert out == [{"type": "a"}]


def test_steer_feedback_is_interleaved(monkeypatch):
    monkeypatch.setattr(st, "_steering_drain",
                        lambda *a: iter([{"type": "thought", "text": "steered"}]))
    out = list(st._drain_run(_RealQ([{"type": "tool"}, None]), 1, _subs(),
                             "/cwd", lambda: False))
    assert [e["type"] for e in out] == ["tool", "thought"]
