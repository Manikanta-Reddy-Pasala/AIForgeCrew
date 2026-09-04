"""The bounded repair loop, the SPEC render, and the delivery audit.

The loop's central property is that it is MONOTONIC: every round is snapshotted
first, and a round that raises the failure count is rolled back, so a local
model's bad patch can never leave the tree worse than it found it. A lateral
move (same count) is deliberately KEPT — it lets the model refactor toward the
seam — but it counts as a stall, and four stalls end the loop.

Everything is driven through stubs for the test runner and the fixer: the
questions here are about arithmetic and control flow, not about models.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.parallel_subtasks._reconcile import _integration as ig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AIFORGE_RECONCILE_SKIP_PREEXISTING", "AIFORGE_RECONCILE_TEST_AUDIT",
              "AIFORGE_RECONCILE_INTEGRATION", "AIFORGE_ESCALATION_MODEL"):
        monkeypatch.delenv(k, raising=False)


def _runner(monkeypatch, results):
    """Stub the project test runner with a queue of (ok, output) results."""
    queue = list(results)

    def _run(cwd):
        return queue.pop(0) if len(queue) > 1 else queue[0]
    monkeypatch.setattr(ig, "_project_test_output", _run)


# ─── the pre-existing-failure gate ─────────────────────────────────────


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(ig, "_fail_count", lambda out: 0)
    monkeypatch.setattr(ig, "_broken_project_config", lambda cwd: "")
    monkeypatch.setattr(ig, "_is_greenfield", lambda cwd: False)
    monkeypatch.setattr(ig, "_change_in_error", lambda cwd, out: False)


def test_a_collection_error_untouched_by_this_turn_is_pre_existing(gate):
    assert ig._is_preexisting_failure("/cwd", "ImportError: no module named zzz") is True


def test_a_failure_naming_a_changed_file_is_ours(gate, monkeypatch):
    monkeypatch.setattr(ig, "_change_in_error", lambda cwd, out: True)
    assert ig._is_preexisting_failure("/cwd", "out") is False


def test_real_parsed_failures_are_never_pre_existing(gate, monkeypatch):
    monkeypatch.setattr(ig, "_fail_count", lambda out: 3)
    assert ig._is_preexisting_failure("/cwd", "3 failed") is False


def test_a_broken_config_in_this_tree_is_ours_to_fix(gate, monkeypatch):
    monkeypatch.setattr(ig, "_broken_project_config", lambda cwd: "bad toml")
    assert ig._is_preexisting_failure("/cwd", "out") is False


def test_on_a_greenfield_build_every_failure_is_this_turns(gate, monkeypatch):
    monkeypatch.setattr(ig, "_is_greenfield", lambda cwd: True)
    assert ig._is_preexisting_failure("/cwd", "out") is False


def test_the_gate_can_be_turned_off(gate, monkeypatch):
    monkeypatch.setenv("AIFORGE_RECONCILE_SKIP_PREEXISTING", "0")
    assert ig._is_preexisting_failure("/cwd", "out") is False


# ─── per-round strategy ────────────────────────────────────────────────


def test_the_first_round_uses_the_coder(monkeypatch):
    monkeypatch.setattr(ig, "_escalation_model", lambda: "big-thinker")
    monkeypatch.setattr(ig, "_is_hard_residual", lambda out: False)
    assert ig._round_strategy("out", stalls=0) == (None, False)


def test_a_plain_failure_escalates_after_two_stalls(monkeypatch):
    monkeypatch.setattr(ig, "_escalation_model", lambda: "big-thinker")
    monkeypatch.setattr(ig, "_is_hard_residual", lambda out: False)
    assert ig._round_strategy("out", stalls=1) == (None, False)
    assert ig._round_strategy("out", stalls=2) == ("big-thinker", True)


def test_a_structurally_hard_residual_escalates_early(monkeypatch):
    """Don't burn a second stall round on a cross-file signature mismatch the
    coder plus repo map already failed to crack."""
    monkeypatch.setattr(ig, "_escalation_model", lambda: "big-thinker")
    monkeypatch.setattr(ig, "_is_hard_residual", lambda out: True)
    assert ig._round_strategy("out", stalls=1) == ("big-thinker", False)


def test_no_escalation_model_configured(monkeypatch):
    monkeypatch.setattr(ig, "_escalation_model", lambda: None)
    monkeypatch.setattr(ig, "_is_hard_residual", lambda out: True)
    assert ig._round_strategy("out", stalls=3) == (None, True)


def test_the_test_audit_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_RECONCILE_TEST_AUDIT", "0")
    monkeypatch.setattr(ig, "_escalation_model", lambda: None)
    monkeypatch.setattr(ig, "_is_hard_residual", lambda out: False)
    assert ig._round_strategy("out", stalls=5) == (None, False)


# ─── rollback ──────────────────────────────────────────────────────────


def test_a_snapshot_restores_edits_and_removes_new_files(tmp_path):
    (tmp_path / "kept.py").write_text("bad patch\n")
    (tmp_path / "invented.py").write_text("phantom\n")
    ig._restore_snapshot(str(tmp_path), {"kept.py": "original\n"})
    assert (tmp_path / "kept.py").read_text() == "original\n"
    assert not (tmp_path / "invented.py").exists()


def test_a_failed_restore_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(ig.os, "remove",
                        lambda p: (_ for _ in ()).throw(OSError("busy")))
    (tmp_path / "invented.py").write_text("phantom\n")
    ig._restore_snapshot(str(tmp_path), {})
    assert (tmp_path / "invented.py").exists()


# ─── round narration ───────────────────────────────────────────────────


def test_the_round_line_names_the_failure_count():
    assert ig._round_plan_text(2, 12, 5, None, False) == (
        "Integration failed (5 failing) — pass 2/12: patching the offending files…")


def test_zero_failures_with_a_red_run_is_explained_not_contradicted():
    """"0 failing" on a red run means the harness died before tests ran."""
    text = ig._round_plan_text(1, 12, 0, None, False)
    assert "run ERRORED before tests executed" in text


def test_the_round_line_names_the_escalation_model():
    assert "escalating the residual to big-thinker" in ig._round_plan_text(
        3, 12, 4, "big-thinker", True)


def test_the_round_line_says_when_it_is_auditing_tests():
    assert "auditing whether a stuck test is itself wrong" in ig._round_plan_text(
        3, 12, 4, None, True)


# ─── the deterministic pre-fix ─────────────────────────────────────────


def test_the_quiet_prune_returns_what_it_removed(monkeypatch):
    monkeypatch.setattr(ig, "_prune_dead_python_imports", lambda cwd: ["pkg/__init__.py"])
    assert ig._prune_quietly("/cwd") == ["pkg/__init__.py"]


def test_a_prune_failure_is_swallowed(monkeypatch):
    def _boom(_cwd):
        raise RuntimeError("unparseable tree")
    monkeypatch.setattr(ig, "_prune_dead_python_imports", _boom)
    assert ig._prune_quietly("/cwd") == []


# ─── one repair round ──────────────────────────────────────────────────


@pytest.fixture
def round_env(monkeypatch, tmp_path):
    monkeypatch.setattr(ig, "_prune_quietly", lambda cwd: [])
    monkeypatch.setattr(ig, "_escalation_model", lambda: None)
    monkeypatch.setattr(ig, "_is_hard_residual", lambda out: False)
    monkeypatch.setattr(ig, "_directed_hints", lambda out: [])
    monkeypatch.setattr(ig, "_rewrite_fix", lambda *a, **k: ["app/a.py"])
    (tmp_path / "a.py").write_text("original\n")
    return tmp_path


def test_an_improving_round_is_kept_and_clears_the_stall_count(round_env, monkeypatch):
    monkeypatch.setattr(ig, "_fail_count", lambda out: 1)
    _runner(monkeypatch, [(False, "1 failed")])
    state: dict = {}
    events = list(ig._repair_round(str(round_env), "3 failed", 1, 12, 3, 2, state))
    assert state["prev_fails"] == 1
    assert state["stalls"] == 0
    assert events[-1]["name"] == "patched files"
    assert events[-1]["args"]["status"] == "1 failing"


def test_a_lateral_round_is_kept_but_counts_as_a_stall(round_env, monkeypatch):
    """Same count is not progress, but rolling it back would stop the model
    refactoring toward the seam."""
    monkeypatch.setattr(ig, "_fail_count", lambda out: 3)
    _runner(monkeypatch, [(False, "3 failed")])
    state: dict = {}
    list(ig._repair_round(str(round_env), "3 failed", 1, 12, 3, 0, state))
    assert state["prev_fails"] == 3
    assert state["stalls"] == 1


def test_a_regressing_round_is_rolled_back(round_env, monkeypatch):
    counts = iter([5, 3])          # after the patch, then after the restore
    monkeypatch.setattr(ig, "_fail_count", lambda out: next(counts))
    _runner(monkeypatch, [(False, "5 failed")])
    restored: list = []
    monkeypatch.setattr(ig, "_restore_snapshot",
                        lambda cwd, snap: restored.append(sorted(snap)))
    state: dict = {}
    events = list(ig._repair_round(str(round_env), "3 failed", 2, 12, 3, 0, state))
    assert restored == [["a.py"]]           # the pre-round tree was snapshotted
    assert state["prev_fails"] == 3         # unchanged — the round is discarded
    assert state["stalls"] == 1
    assert "REGRESSED" in events[-1]["text"]


def test_a_transient_fixer_error_does_not_stop_the_round(round_env, monkeypatch):
    monkeypatch.setattr(ig, "_fail_count", lambda out: 3)
    _runner(monkeypatch, [(False, "3 failed")])

    def _boom(*_a, **_k):
        raise RuntimeError("model timed out")
    monkeypatch.setattr(ig, "_rewrite_fix", _boom)
    state: dict = {}
    events = list(ig._repair_round(str(round_env), "3 failed", 1, 12, 3, 0, state))
    assert "transient error" in events[1]["text"]
    assert state["stalls"] == 1


def test_a_tree_that_cannot_collect_reports_the_output(round_env, monkeypatch):
    monkeypatch.setattr(ig, "_fail_count", lambda out: 999)
    _runner(monkeypatch, [(False, "collection error")])
    state: dict = {}
    events = list(ig._repair_round(str(round_env), "collection error", 1, 12, 999, 0, state))
    ev = events[-1]
    assert ev["args"]["status"] == "tests can't run (collection/build error)"
    assert ev["result"]["ok"] is False
    assert ev["result"]["output"]


# ─── the loop ──────────────────────────────────────────────────────────


def test_the_loop_stops_when_the_tree_goes_green(monkeypatch):
    monkeypatch.setattr(ig, "_reconcile_rounds", lambda: 12)
    monkeypatch.setattr(ig, "_fail_count", lambda out: 2)
    rounds = {"n": 0}

    def _round(cwd, output, r, mx, prev, stalls, state):
        rounds["n"] += 1
        state.update(ok=True, output="", prev_fails=0, stalls=0)
        return iter(())
    monkeypatch.setattr(ig, "_repair_round", _round)
    state: dict = {}
    list(ig._repair_loop("/cwd", "2 failed", None, state))
    assert rounds["n"] == 1
    assert state["ok"] is True


def test_the_loop_is_bounded_by_the_round_cap(monkeypatch):
    monkeypatch.setattr(ig, "_reconcile_rounds", lambda: 3)
    monkeypatch.setattr(ig, "_fail_count", lambda out: 2)

    def _round(cwd, output, r, mx, prev, stalls, state):
        state.update(ok=False, output=output, prev_fails=2, stalls=0)
        return iter(())
    monkeypatch.setattr(ig, "_repair_round", _round)
    state: dict = {}
    list(ig._repair_loop("/cwd", "2 failed", None, state))
    assert state["rounds"] == 3


def test_four_no_progress_rounds_end_the_loop(monkeypatch):
    monkeypatch.setattr(ig, "_reconcile_rounds", lambda: 12)
    monkeypatch.setattr(ig, "_fail_count", lambda out: 2)

    def _round(cwd, output, r, mx, prev, stalls, state):
        state.update(ok=False, output=output, prev_fails=2, stalls=4)
        return iter(())
    monkeypatch.setattr(ig, "_repair_round", _round)
    state: dict = {}
    list(ig._repair_loop("/cwd", "2 failed", None, state))
    assert state["rounds"] == 1


def test_stop_halts_the_loop(monkeypatch):
    monkeypatch.setattr(ig, "_reconcile_rounds", lambda: 12)
    monkeypatch.setattr(ig, "_fail_count", lambda out: 2)
    monkeypatch.setattr(ig, "_repair_round",
                        lambda *a: pytest.fail("ran a round after Stop"))
    state: dict = {}
    list(ig._repair_loop("/cwd", "2 failed", lambda: True, state))
    assert state["rounds"] == 0


# ─── the entry point ───────────────────────────────────────────────────


@pytest.fixture
def report(monkeypatch):
    import aiforge_core.runtime.integration_report as ir
    monkeypatch.setattr(ir, "build_and_test_report", lambda cwd: {"ok": True, "md": "# report"})
    monkeypatch.setattr(ig, "_prune_quietly", lambda cwd: [])


def test_a_green_tree_needs_no_repair(monkeypatch, report):
    _runner(monkeypatch, [(True, "")])
    monkeypatch.setattr(ig, "_repair_loop", lambda *a: pytest.fail("repaired a green tree"))
    result: dict = {}
    assert list(ig._reconcile_integration("/cwd", result)) == []
    assert result["ok"] is True
    assert result["rep"]["md"] == "# report"


def test_stop_before_the_first_run_still_reports(monkeypatch, report):
    result: dict = {}
    assert list(ig._reconcile_integration("/cwd", result, should_cancel=lambda: True)) == []
    assert result["rep"]["md"] == "# report"


def test_the_whole_reconcile_can_be_turned_off(monkeypatch, report):
    monkeypatch.setenv("AIFORGE_RECONCILE_INTEGRATION", "0")
    _runner(monkeypatch, [(False, "3 failed")])
    result: dict = {}
    assert list(ig._reconcile_integration("/cwd", result)) == []
    assert result["ok"] is False


def test_pruned_reexports_are_surfaced(monkeypatch, report):
    monkeypatch.setattr(ig, "_prune_quietly", lambda cwd: ["pkg/__init__.py"])
    _runner(monkeypatch, [(True, "")])
    events = list(ig._reconcile_integration("/cwd", {}))
    assert events[0]["name"] == "pruned dead re-exports"


def test_a_pre_existing_failure_is_not_a_regression(monkeypatch, report):
    _runner(monkeypatch, [(False, "ImportError")])
    monkeypatch.setattr(ig, "_is_preexisting_failure", lambda cwd, out: True)
    monkeypatch.setattr(ig, "_repair_loop",
                        lambda *a: pytest.fail("repaired a pre-existing failure"))
    result: dict = {}
    events = list(ig._reconcile_integration("/cwd", result))
    assert result["ok"] is None            # neither pass nor fail — not ours
    assert "not the cause" in events[0]["text"]


def test_a_broken_config_is_pointed_at_first(monkeypatch, report):
    """One unterminated string in pyproject.toml made every run die at config
    parse, and reconcile burned its passes patching unrelated files."""
    _runner(monkeypatch, [(False, "exit 1")])
    monkeypatch.setattr(ig, "_is_preexisting_failure", lambda cwd, out: False)
    monkeypatch.setattr(ig, "_broken_project_config", lambda cwd: "pyproject.toml: bad string")
    seen: dict = {}

    def _loop(cwd, output, cancel, state):
        seen["output"] = output
        state.update(ok=True, rounds=1)
        return iter(())
    monkeypatch.setattr(ig, "_repair_loop", _loop)
    events = list(ig._reconcile_integration("/cwd", {}))
    assert "project config invalid" in events[0]["text"]
    assert seen["output"].startswith("CONFIG ERROR")


def test_a_green_finish_is_announced(monkeypatch, report):
    _runner(monkeypatch, [(False, "3 failed")])
    monkeypatch.setattr(ig, "_is_preexisting_failure", lambda cwd, out: False)
    monkeypatch.setattr(ig, "_broken_project_config", lambda cwd: "")

    def _loop(cwd, output, cancel, state):
        state.update(ok=True, rounds=2)
        return iter(())
    monkeypatch.setattr(ig, "_repair_loop", _loop)
    result: dict = {}
    events = list(ig._reconcile_integration("/cwd", result))
    assert "green after 2 pass(es)" in events[-1]["text"]
    assert result["ok"] is True


def test_a_red_finish_says_so(monkeypatch, report):
    _runner(monkeypatch, [(False, "3 failed")])
    monkeypatch.setattr(ig, "_is_preexisting_failure", lambda cwd, out: False)
    monkeypatch.setattr(ig, "_broken_project_config", lambda cwd: "")

    def _loop(cwd, output, cancel, state):
        state.update(ok=False, rounds=12)
        return iter(())
    monkeypatch.setattr(ig, "_repair_loop", _loop)
    events = list(ig._reconcile_integration("/cwd", {}))
    assert "some tests still" in events[-1]["text"]


# ─── SPEC.md render ────────────────────────────────────────────────────


def test_the_spec_carries_goal_tree_contract_and_subtasks():
    md = ig._render_spec_md("Build an LRU cache", [
        {"slug": "store", "path": "app/store.py", "goal": "the cache",
         "api": ["class Store", "def get(k)"], "acceptance": ["evicts LRU"]},
        {"slug": "cli", "path": "app/cli.py", "goal": "entry point"},
    ])
    assert "## Goal\n\nBuild an LRU cache" in md
    assert "- `app/store.py`" in md
    assert "### `app/store.py` exposes" in md
    assert "- `class Store`" in md
    assert "## Subtasks (2)" in md
    assert "1. **store** — the cache" in md
    assert "   - [ ] evicts LRU" in md


def test_a_plan_without_paths_omits_the_file_tree():
    md = ig._render_spec_md("goal", [{"slug": "a", "goal": "think"}])
    assert "File tree" not in md
    assert "API contract" not in md


def test_a_plan_without_declared_apis_omits_the_contract():
    md = ig._render_spec_md("goal", [{"path": "a.py", "goal": "g"}])
    assert "API contract" not in md
    assert "- `a.py`" in md


def test_a_subtask_without_a_slug_is_numbered():
    assert "1. **sub-1**" in ig._render_spec_md("goal", [{"goal": "g"}])


# ─── the delivery audit ────────────────────────────────────────────────


@pytest.fixture
def verifier(monkeypatch):
    import aiforge_core.llm.client as client
    seen: dict = {}

    def _complete(role, convo, **kw):
        seen["role"] = role
        seen["user"] = convo[1]["content"]
        return seen.get("reply", "  Everything is covered.  ")
    monkeypatch.setattr(client, "complete", _complete)
    return seen


def test_the_auditor_sees_the_spec_and_the_produced_tree(verifier, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/store.py").write_text("x = 1\n")
    assert ig._verify_against_spec(str(tmp_path), "# SPEC") == "Everything is covered."
    assert verifier["role"] == "verifier"
    assert "app/store.py" in verifier["user"]
    assert "# SPEC" in verifier["user"]


def test_an_empty_tree_is_still_auditable(verifier, tmp_path):
    ig._verify_against_spec(str(tmp_path), "# SPEC")
    assert "(no files)" in verifier["user"]


def test_vendor_dirs_are_not_listed(verifier, tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/dep.js").write_text("x")
    ig._verify_against_spec(str(tmp_path), "# SPEC")
    assert "node_modules" not in verifier["user"]


def test_the_listing_is_capped(verifier, tmp_path):
    for i in range(500):
        (tmp_path / f"f{i}.py").write_text("x")
    ig._verify_against_spec(str(tmp_path), "# SPEC")
    assert verifier["user"].count("\n") < 500


def test_a_verifier_failure_never_blocks_delivery(monkeypatch, tmp_path):
    import aiforge_core.llm.client as client

    def _boom(*_a, **_kw):
        raise RuntimeError("verifier down")
    monkeypatch.setattr(client, "complete", _boom)
    assert ig._verify_against_spec(str(tmp_path), "# SPEC") == ""
