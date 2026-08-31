"""Running the merged tree's tests, and reading its failures.

Two things here have bitten before and are pinned hard. First, output
collection: a Maven/Gradle/Go compile error lives under ``results[].output``,
not the top-level keys, and losing it made the reconciler see zero failures and
give up on a build it could have fixed. Second, the fail count: a fixture
ERROR is reported as "N errors", never "failed", so counting only failures made
the loop stop early believing it was nearly done.
"""
from __future__ import annotations

import subprocess

import pytest

from aiforge_core.runtime.parallel_subtasks._reconcile import _testrun as tr


# ─── round budget ──────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("5", 5), ("0", 0), ("99", 16),
                                          ("-3", 0), ("many", 12)])
def test_the_round_budget_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_RECONCILE_ROUNDS", raw)
    assert tr._reconcile_rounds() == expected


def test_the_default_round_budget(monkeypatch):
    monkeypatch.delenv("AIFORGE_RECONCILE_ROUNDS", raising=False)
    assert tr._reconcile_rounds() == 12


# ─── which model to escalate to ────────────────────────────────────────


def test_an_explicit_escalation_model_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATION_MODEL", "big-thinker")
    assert tr._escalation_model() == "big-thinker"


def test_a_stronger_reasoning_role_is_used_when_it_differs(monkeypatch):
    """A deploy that gives reasoning roles a bigger model escalates without
    anyone setting an extra env var."""
    monkeypatch.delenv("AIFORGE_ESCALATION_MODEL", raising=False)
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "load_all", lambda: {"doer": {"model": "coder"},
                                                 "reasoner": {"model": "thinker"}})
    assert tr._escalation_model() == "thinker"


def test_the_same_model_everywhere_means_nothing_to_escalate_to(monkeypatch):
    monkeypatch.delenv("AIFORGE_ESCALATION_MODEL", raising=False)
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "load_all", lambda: {"doer": {"model": "coder"},
                                                 "reasoner": {"model": "coder"}})
    assert tr._escalation_model() is None


def test_a_broken_config_is_not_fatal(monkeypatch):
    monkeypatch.delenv("AIFORGE_ESCALATION_MODEL", raising=False)
    import aiforge_core.config.agent_config as ac

    def _boom():
        raise RuntimeError("bad yaml")
    monkeypatch.setattr(ac, "load_all", _boom)
    assert tr._escalation_model() is None


# ─── collecting the run output ─────────────────────────────────────────


def test_a_compile_error_buried_in_a_sub_result_is_recovered():
    """project() puts each command's real output under results[].output — a
    javac error the reconciler never saw was a build it could not fix."""
    out = tr._collect_run_output({"ok": False, "results": [
        {"output": "Main.java:[12] cannot find symbol"}]})
    assert "cannot find symbol" in out


def test_every_top_level_key_is_gathered():
    out = tr._collect_run_output({"error": "e", "output": "o", "stdout": "so",
                                  "stderr": "se", "logs": "l", "details": "d",
                                  "message": "m"})
    assert out.split("\n") == ["e", "o", "so", "se", "l", "d", "m"]


def test_a_sub_result_error_key_also_counts():
    assert "boom" in tr._collect_run_output({"results": [{"error": "boom"}]})


def test_a_clean_result_collects_nothing():
    assert tr._collect_run_output({"ok": True}) == ""


def test_a_non_dict_sub_result_is_ignored():
    assert tr._collect_run_output({"results": ["a string"]}) == ""


# ─── the raw fallback command ──────────────────────────────────────────


def test_maven_is_run_directly_when_the_output_was_lost(monkeypatch, tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    seen: dict = {}

    class _P:
        stdout, stderr = "compile error", ""
    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return _P()
    monkeypatch.setattr(subprocess, "run", _run)
    assert tr._raw_build_test_output(str(tmp_path), []) == "compile error"
    assert seen["cmd"][0] == "mvn"


@pytest.mark.parametrize("cfg", ["build.gradle", "build.gradle.kts"])
def test_gradle_is_run_directly(monkeypatch, tmp_path, cfg):
    (tmp_path / cfg).write_text("")

    class _P:
        stdout, stderr = "gradle out", ""
    seen: dict = {}

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return _P()
    monkeypatch.setattr(subprocess, "run", _run)
    assert tr._raw_build_test_output(str(tmp_path), []) == "gradle out"
    assert seen["cmd"][0] == "gradle"


def test_a_tree_with_no_jvm_build_file_has_no_fallback(tmp_path):
    assert tr._raw_build_test_output(str(tmp_path), []) == ""


def test_a_missing_toolchain_is_not_fatal(monkeypatch, tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")

    def _boom(*_a, **_kw):
        raise FileNotFoundError("mvn")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert tr._raw_build_test_output(str(tmp_path), []) == ""


# ─── dependency pre-warm ───────────────────────────────────────────────


def test_a_compiled_stack_is_prewarmed(monkeypatch, tmp_path):
    """A cold JUnit/crates download on the FIRST run flagged a false failure on
    code that actually passed."""
    monkeypatch.delenv("AIFORGE_RECONCILE_PREWARM", raising=False)
    import aiforge_core.runtime.tools.project_runner as pr
    seen: dict = {}
    monkeypatch.setattr(pr, "project", lambda **kw: seen.update(kw))
    tr._prewarm_deps(str(tmp_path), ["maven"])
    assert seen["action"] == "install"


def test_an_interpreted_stack_is_not_prewarmed(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_RECONCILE_PREWARM", raising=False)
    import aiforge_core.runtime.tools.project_runner as pr
    monkeypatch.setattr(pr, "project", lambda **kw: pytest.fail("prewarmed python"))
    tr._prewarm_deps(str(tmp_path), ["python"])


def test_the_prewarm_can_be_turned_off(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_RECONCILE_PREWARM", "0")
    import aiforge_core.runtime.tools.project_runner as pr
    monkeypatch.setattr(pr, "project", lambda **kw: pytest.fail("prewarmed with gate off"))
    tr._prewarm_deps(str(tmp_path), ["maven"])


def test_a_failed_prewarm_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_RECONCILE_PREWARM", raising=False)
    import aiforge_core.runtime.tools.project_runner as pr

    def _boom(**_kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(pr, "project", _boom)
    tr._prewarm_deps(str(tmp_path), ["rust"])


# ─── the static-check gate ─────────────────────────────────────────────


def test_a_red_run_skips_the_linters(monkeypatch, tmp_path):
    import aiforge_core.runtime.integration_report as ir
    monkeypatch.setattr(ir, "run_static_checks",
                        lambda cwd: pytest.fail("linted a failing tree"))
    assert tr._static_check_gate(str(tmp_path), False, "3 failed") == (False, "3 failed")


def test_a_green_run_that_fails_lint_goes_red(monkeypatch, tmp_path):
    import aiforge_core.runtime.integration_report as ir
    monkeypatch.setattr(ir, "run_static_checks", lambda cwd: (False, "\nruff: E501"))
    assert tr._static_check_gate(str(tmp_path), True, "ok") == (False, "ok\nruff: E501")


def test_a_green_run_that_passes_lint_stays_green(monkeypatch, tmp_path):
    import aiforge_core.runtime.integration_report as ir
    monkeypatch.setattr(ir, "run_static_checks", lambda cwd: (True, ""))
    assert tr._static_check_gate(str(tmp_path), True, "ok") == (True, "ok")


def test_a_missing_linter_never_fails_the_run(monkeypatch, tmp_path):
    import aiforge_core.runtime.integration_report as ir

    def _boom(_cwd):
        raise RuntimeError("no tool")
    monkeypatch.setattr(ir, "run_static_checks", _boom)
    assert tr._static_check_gate(str(tmp_path), True, "ok") == (True, "ok")


# ─── running the project's tests ───────────────────────────────────────


@pytest.fixture()
def runner(monkeypatch):
    import aiforge_core.runtime.tools.project_runner as pr
    state = {"stacks": ["python"], "has_tests": True, "res": {"ok": True}}
    monkeypatch.setattr(pr, "detect", lambda cwd: {"stacks": state["stacks"]})
    monkeypatch.setattr(pr, "_has_tests", lambda cwd, stacks: state["has_tests"])
    monkeypatch.setattr(pr, "project", lambda **kw: state.setdefault("called", kw) and None
                        or state["res"])
    monkeypatch.setattr(tr, "_prewarm_deps", lambda cwd, stacks: None)
    monkeypatch.setattr(tr, "_static_check_gate", lambda cwd, ok, out: (ok, out))
    return state


def test_a_tree_with_no_recognised_stack_has_nothing_to_reconcile(runner, tmp_path):
    runner["stacks"] = []
    assert tr._run_project_tests(str(tmp_path)) == (True, "")


def test_a_stack_without_tests_is_built_instead(runner, tmp_path):
    runner["has_tests"] = False
    tr._run_project_tests(str(tmp_path))
    assert runner["called"]["action"] == "build"


def test_a_failing_run_with_no_captured_output_falls_back_to_the_raw_command(
        runner, tmp_path, monkeypatch):
    runner["res"] = {"ok": False}
    monkeypatch.setattr(tr, "_raw_build_test_output", lambda cwd, stacks: "javac: boom")
    assert tr._run_project_tests(str(tmp_path)) == (False, "javac: boom")


def test_the_managed_venv_pytest_is_preferred_for_python(monkeypatch, tmp_path):
    """A plain project(action=test) misses the third-party deps, so pytest
    fails to import and the captured output is EMPTY."""
    import aiforge_core.runtime.integration_report as ir
    monkeypatch.setattr(ir, "run_bare_python_tests", lambda cwd: (False, "2 failed"))
    monkeypatch.setattr(tr, "_run_project_tests",
                        lambda cwd: pytest.fail("skipped the managed venv"))
    assert tr._project_test_output(str(tmp_path)) == (False, "2 failed")


def test_a_non_python_tree_falls_through_to_the_project_runner(monkeypatch, tmp_path):
    import aiforge_core.runtime.integration_report as ir
    monkeypatch.setattr(ir, "run_bare_python_tests", lambda cwd: None)
    monkeypatch.setattr(tr, "_run_project_tests", lambda cwd: (True, "built"))
    assert tr._project_test_output(str(tmp_path)) == (True, "built")


def test_a_runner_crash_reads_as_a_failure_with_the_reason(monkeypatch, tmp_path):
    import aiforge_core.runtime.integration_report as ir

    def _boom(_cwd):
        raise RuntimeError("runner exploded")
    monkeypatch.setattr(ir, "run_bare_python_tests", _boom)
    assert tr._project_test_output(str(tmp_path)) == (False, "runner exploded")


# ─── routing a mid-run steer ───────────────────────────────────────────


_SUBS = [{"slug": "store", "path": "app/store.py", "goal": "the cache store"},
         {"slug": "cli", "path": "app/cli.py", "goal": "command line entry"}]


def _classifier(monkeypatch, result):
    import aiforge_core.llm.structured as st

    def _sc(role, messages, model, **kw):
        if isinstance(result, Exception):
            raise result
        return model(**result)
    monkeypatch.setattr(st, "structured_complete", _sc)


def test_the_classifier_picks_a_subtask(monkeypatch):
    _classifier(monkeypatch, {"target": "store", "note": "use an LRU"})
    assert tr._route_steering("make the cache LRU", _SUBS) == {
        "target": "store", "note": "use an LRU"}


@pytest.mark.parametrize("target", ["global", "new"])
def test_the_classifier_can_say_global_or_new(monkeypatch, target):
    _classifier(monkeypatch, {"target": target, "note": ""})
    assert tr._route_steering("add logging everywhere", _SUBS)["target"] == target


def test_a_hallucinated_target_falls_through_to_the_keyword_match(monkeypatch):
    _classifier(monkeypatch, {"target": "nonexistent", "note": "x"})
    assert tr._route_steering("the store should be faster", _SUBS)["target"] == "store"


def test_a_dead_classifier_falls_back_to_token_overlap(monkeypatch):
    _classifier(monkeypatch, RuntimeError("model down"))
    assert tr._route_steering("fix the cli entry", _SUBS)["target"] == "cli"


def test_a_comment_matching_nothing_is_global(monkeypatch):
    _classifier(monkeypatch, RuntimeError("model down"))
    assert tr._route_steering("hurry up please", _SUBS) == {"target": "global", "note": ""}


# ─── classifying the residual ──────────────────────────────────────────


@pytest.mark.parametrize("output", [
    "ImportError: cannot import name 'Binary' from 'expr'",
    "ModuleNotFoundError: No module named 'store'",
    "AttributeError: 'Board' object has no attribute 'drop'",
    "NameError: name 'COLORS' is not defined",
    "TypeError: drop() got an unexpected keyword argument 'col'",
    "TypeError: drop() missing 1 required positional argument",
    "Main.java:[3] cannot find symbol",
    "package com.app.util does not exist",
])
def test_a_cross_file_structural_mismatch_is_hard(output):
    assert tr._is_hard_residual(output) is True


@pytest.mark.parametrize("output", ["assert 2 == 3", "5 failed", ""])
def test_a_plain_logic_failure_is_not(output):
    assert tr._is_hard_residual(output) is False


# ─── counting failures ─────────────────────────────────────────────────


def test_failures_and_errors_are_summed():
    """A fixture ERROR is never reported as "failed" — counting only failures
    made the loop stop early thinking it was nearly done."""
    assert tr._fail_count("3 failed, 2 errors in 1.2s") == 5


def test_errors_alone_are_counted():
    assert tr._fail_count("== 4 errors in 0.3s ==") == 4


def test_an_explicit_zero_is_green():
    assert tr._fail_count("0 failed, 12 passed") == 0


def test_a_raw_error_with_no_counts_is_the_worst_case():
    assert tr._fail_count("Traceback (most recent call last):") == 999


def test_a_clean_run_is_zero():
    assert tr._fail_count("12 passed in 0.4s") == 0


def test_no_output_at_all_is_zero():
    assert tr._fail_count("") == 0


# ─── directed hints ────────────────────────────────────────────────────


def test_an_import_error_becomes_a_concrete_instruction():
    hints = tr._directed_hints("ImportError: cannot import name 'Binary' from 'expr'")
    assert hints and any("Binary" in h for h in hints)


def test_leaked_state_is_recognised():
    assert tr._leaked_state("ValueError: user already exists") is True
    assert tr._leaked_state("2 errors\nsomething already exists") is True
    assert tr._leaked_state("2 failed") is False


def test_a_state_leak_gets_its_own_hint():
    hints = tr._directed_hints("E   ValueError: Username already exists")
    assert tr._STATE_LEAK_HINT in hints


def test_a_javax_import_is_pointed_at_jakarta():
    assert "jakarta." in tr._java_package_hint("javax.persistence")


def test_hints_are_deduped_and_capped():
    out = tr._dedupe_keep_order(["a", "b", "a", "c", "b"])
    assert out == ["a", "b", "c"]
    assert len(tr._directed_hints("cannot import name 'X' from 'm'\n" * 60)) <= 20


def test_clean_output_produces_no_hints():
    assert tr._directed_hints("12 passed in 0.4s") == []


# ─── the config-validity gate ──────────────────────────────────────────


def test_a_valid_tree_has_no_config_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (tmp_path / "package.json").write_text('{"name": "x"}')
    (tmp_path / "pom.xml").write_text("<project></project>")
    assert tr._broken_project_config(str(tmp_path)) is None


def test_an_unterminated_string_in_pyproject_is_caught(tmp_path):
    """One of these made every pytest run die at config parse, so reconcile
    burned all 12 passes reporting "0 failing" while patching the wrong files."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x\n')
    assert tr._broken_project_config(str(tmp_path)).startswith("pyproject.toml:")


def test_broken_package_json_is_caught(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert tr._broken_project_config(str(tmp_path)).startswith("package.json:")


def test_a_malformed_pom_is_caught(tmp_path):
    (tmp_path / "pom.xml").write_text("<project>")
    assert tr._broken_project_config(str(tmp_path)).startswith("pom.xml:")


def test_a_tree_with_no_config_files(tmp_path):
    assert tr._broken_project_config(str(tmp_path)) is None
