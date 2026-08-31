"""What actually runs ONE subtask of a parallel build.

Two runners live here. The heavy one drives the full Doer chat loop in the
subtask's own worktree; the light one spends a single LLM call and writes the
files itself. Both exist because a fan-out of N subtasks on a shared local
model only finishes if each slice is cheap — and both had to learn the same
lessons the hard way:

  * a fresh context re-guesses the package directory (``mini_lang/`` vs
    ``miniLang/``) and the merge ends up with three split dirs, so the target
    path is pinned in the prompt AND enforced in code afterwards;
  * the lightweight path writes files DIRECTLY — no file_write, no build
    marker in an isolated worktree — so a truncated file would sail through
    the subtask and only explode at the post-merge build;
  * the runaway safety cap emits a plain message like any answer, so "the
    loop produced a message" is not the same as "the subtask succeeded".

The scope allowlist is the file-ownership contract that makes reconcile a
disjoint union instead of a same-file merge, so a write outside it is rejected
rather than merged.
"""
from __future__ import annotations

import os
import types as pytypes

import pytest

from aiforge_core.runtime.parallel_subtasks import _runners as R


# ─── the Doer prompt ───────────────────────────────────────────────────


def test_the_target_path_is_pinned_byte_for_byte():
    msg = R._doer_message({}, "", "pkg/mini_lang/lexer.py", "write a lexer")
    assert "pkg/mini_lang/lexer.py" in msg
    assert "do NOT rename" in msg and "re-case" in msg


def test_a_subtask_without_a_path_gets_no_pin():
    msg = R._doer_message({}, "", "", "do a thing")
    assert "TARGET FILE" not in msg and "GOAL: do a thing" in msg


def test_the_shared_spec_is_carried_into_every_fresh_context():
    msg = R._doer_message({}, "# Spec\nbuild a lang", "", "g")
    assert "PROJECT SPEC" in msg and "build a lang" in msg


def test_a_blank_spec_is_left_out():
    assert "PROJECT SPEC" not in R._doer_message({}, "   \n ", "", "g")


def test_the_spec_is_capped_so_one_subtask_cannot_eat_the_context():
    msg = R._doer_message({}, "x" * 9000, "", "g")
    assert "x" * 6000 in msg and "x" * 6001 not in msg


def test_acceptance_and_scope_are_listed_when_present():
    msg = R._doer_message({"acceptance": ["tokenises ints", "handles eof"],
                           "scope_allowlist_globs": ["a.py", "b.py"]},
                          "", "", "g")
    assert "- tokenises ints" in msg and "- handles eof" in msg
    assert "SCOPE (only touch these): a.py, b.py" in msg


def test_a_subtask_that_blew_its_budget_is_told_to_build_the_core_first():
    """The retry of a too-big subtask must not start broad again — that is how
    it ran out of budget the first time."""
    msg = R._doer_message({"_too_big": True}, "", "", "g")
    assert "build the CORE first" in msg


def test_a_previous_failure_is_quoted_back_verbatim():
    msg = R._doer_message({"_retry_error": "NameError: tok"}, "", "", "g")
    assert "NameError: tok" in msg and "Fix exactly that" in msg


def test_a_giant_previous_error_is_truncated():
    msg = R._doer_message({"_retry_error": "e" * 2000}, "", "", "g")
    assert msg.count("e" * 800) == 1 and "e" * 900 not in msg


# ─── driving the Doer loop ─────────────────────────────────────────────


@pytest.fixture()
def chat(monkeypatch):
    """Stand in for the Doer chat loop; ``events`` is what it yields."""
    import aiforge_core.runtime.chat_agent as ca
    state: dict = {"events": [], "calls": []}

    def _run(convo, **kw):
        state["calls"].append({"convo": convo, **kw})
        if isinstance(state["events"], Exception):
            raise state["events"]
        yield from state["events"]
    monkeypatch.setattr(ca, "run_chat_agent", _run)
    return state


def test_a_final_answer_is_success(chat):
    chat["events"] = [{"type": "message", "text": "done"}]
    assert R._drive_doer("m", "/wt", None, None) == {"ok": True}


def test_the_runaway_cap_message_is_a_failure_not_an_answer(chat):
    """The safety cap stops a thrashing Doer with a plain message — counting
    that as success would merge an unfinished subtask."""
    chat["events"] = [{"type": "message", "text": "(stopped: cap reached)"}]
    assert R._drive_doer("m", "/wt", None, None) == {"ok": False}


def test_a_question_back_to_the_user_is_not_a_finished_subtask(chat):
    chat["events"] = [{"type": "message", "text": "which one?",
                       "awaiting_input": True}]
    assert R._drive_doer("m", "/wt", None, None) == {"ok": False}


def test_an_error_event_ends_the_run_with_its_text(chat):
    chat["events"] = [{"type": "error", "text": "no model"},
                      {"type": "message", "text": "done"}]
    assert R._drive_doer("m", "/wt", None, None) == {"ok": False,
                                                     "error": "no model"}


def test_a_crash_in_the_loop_is_reported_not_raised(chat):
    chat["events"] = RuntimeError("boom")
    assert R._drive_doer("m", "/wt", None, None) == {"ok": False,
                                                     "error": "boom"}


def test_the_loop_runs_in_the_worktree_as_the_doer_with_a_hard_finish(chat):
    chat["events"] = [{"type": "message", "text": "ok"}]
    R._drive_doer("msg", "/wt", ["a.py"], "cf")
    call = chat["calls"][0]
    assert call["cwd"] == "/wt" and call["role"] == "doer"
    assert call["scope_globs"] == ["a.py"] and call["strict_finish"] is True
    assert call["convo"] == [{"role": "user", "content": "msg"}]


# ─── default_run_one ───────────────────────────────────────────────────


@pytest.fixture()
def driver(monkeypatch):
    """Capture what default_run_one hands to the Doer."""
    seen: dict = {"result": {"ok": True}}

    def _drive(msg, worktree, own_scope, complete_fn):
        seen.update(msg=msg, worktree=worktree, scope=own_scope)
        return seen["result"]
    monkeypatch.setattr(R, "_drive_doer", _drive)
    monkeypatch.setattr(R, "_enforce_target_path",
                        lambda wt, p: seen.update(enforced=(wt, p)))
    return seen


def test_the_full_doer_runs_the_subtask_in_its_worktree(driver):
    res = R.default_run_one({"goal": "lex it", "path": "a/lexer.py"}, "/wt")
    assert res == {"ok": True} and driver["worktree"] == "/wt"
    assert "lex it" in driver["msg"]


def test_writes_are_pinned_to_the_one_file_the_subtask_owns(driver):
    """Hard file ownership is what makes the reconcile a disjoint union."""
    R.default_run_one({"path": "a/lexer.py"}, "/wt")
    assert driver["scope"] == ["a/lexer.py"]


def test_an_explicit_allowlist_beats_the_single_target_path(driver):
    R.default_run_one({"path": "a.py", "scope_allowlist_globs": ["src/**"]},
                      "/wt")
    assert driver["scope"] == ["src/**"]


def test_a_subtask_owning_nothing_stays_unscoped(driver):
    R.default_run_one({"goal": "plan"}, "/wt")
    assert driver["scope"] is None


def test_the_canonical_path_is_enforced_after_the_run(driver):
    """The prompt pin is not 100% on a local model, so the move happens in
    code too."""
    R.default_run_one({"path": "/a/lexer.py"}, "/wt")
    assert driver["enforced"] == ("/wt", "a/lexer.py")


def test_nothing_is_moved_when_the_subtask_owns_no_file(driver):
    R.default_run_one({"goal": "g"}, "/wt")
    assert "enforced" not in driver


def test_the_slug_stands_in_for_a_missing_goal(driver):
    R.default_run_one({"slug": "lexer"}, "/wt")
    assert "GOAL: lexer" in driver["msg"]


def test_a_missing_llm_client_fails_the_subtask_cleanly(monkeypatch):
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "aiforge_core.llm.client":
            raise ImportError("no llm")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _imp)
    res = R.default_run_one({"goal": "g"}, "/wt")
    assert res["ok"] is False and "import: no llm" in res["error"]


# ─── path enforcement ──────────────────────────────────────────────────


def test_a_recased_directory_is_moved_onto_the_canonical_path(tmp_path):
    """miniLang/lexer.py written instead of mini_lang/lexer.py — move it, or
    the merge splits the package into variant dirs."""
    (tmp_path / "miniLang").mkdir()
    (tmp_path / "miniLang" / "lexer.py").write_text("code")
    R._enforce_target_path(str(tmp_path), "mini_lang/lexer.py")
    assert (tmp_path / "mini_lang" / "lexer.py").read_text() == "code"
    assert not (tmp_path / "miniLang").exists(), "emptied variant dir pruned"


def test_a_variant_dir_that_still_holds_files_is_kept(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lexer.py").write_text("code")
    (tmp_path / "src" / "other.py").write_text("keep")
    R._enforce_target_path(str(tmp_path), "pkg/lexer.py")
    assert (tmp_path / "src" / "other.py").exists()


def test_a_file_already_at_the_target_is_left_alone(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "lexer.py").write_text("code")
    R._enforce_target_path(str(tmp_path), "pkg/lexer.py")
    assert (tmp_path / "pkg" / "lexer.py").read_text() == "code"


def test_the_move_never_looks_inside_git_or_the_worktree_store(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "lexer.py").write_text("not mine")
    R._enforce_target_path(str(tmp_path), "pkg/lexer.py")
    assert not (tmp_path / "pkg").exists()


def test_a_missing_basename_anywhere_is_simply_a_no_op(tmp_path):
    R._enforce_target_path(str(tmp_path), "pkg/lexer.py")
    assert not (tmp_path / "pkg").exists()


def test_an_unmovable_file_does_not_break_the_runner(tmp_path, monkeypatch):
    """Enforcement is best-effort — a failed move must never fail the run."""
    import shutil
    (tmp_path / "v").mkdir()
    (tmp_path / "v" / "lexer.py").write_text("code")
    monkeypatch.setattr(shutil, "move",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    R._enforce_target_path(str(tmp_path), "pkg/lexer.py")  # no raise
    assert (tmp_path / "v" / "lexer.py").exists()


# ─── parsing the model's file blocks ───────────────────────────────────


def test_each_marked_block_becomes_one_file():
    out = R._parse_file_blocks(
        "=== a/x.py ===\nprint(1)\n=== b/y.py ===\nprint(2)\n")
    assert out == {"a/x.py": "print(1)\n", "b/y.py": "print(2)\n"}


def test_a_fenced_body_is_unwrapped():
    out = R._parse_file_blocks("=== x.py ===\n```python\nprint(1)\n```\n")
    assert out == {"x.py": "print(1)\n"}


def test_a_backticked_path_label_is_cleaned():
    assert "x.py" in R._parse_file_blocks("=== `x.py` ===\nbody\n")


def test_prose_with_no_blocks_yields_nothing():
    assert R._parse_file_blocks("Sure! Here is the code.") == {}


def test_a_block_with_an_empty_body_is_dropped():
    assert R._parse_file_blocks("=== x.py ===\n\n=== y.py ===\nb\n") \
        == {"y.py": "b\n"}


# ─── scope ─────────────────────────────────────────────────────────────


def test_scope_is_decided_by_the_one_shared_matcher():
    """Parallel and single-Doer mode must enforce IDENTICAL scope semantics."""
    assert R._in_scope("src/a.py", ["src/**"]) is True
    assert R._in_scope("other/a.py", ["src/**"]) is False


def test_a_matcher_slip_allows_the_write_rather_than_dropping_it(monkeypatch):
    from aiforge_core.runtime import scope_guard as sg
    monkeypatch.setattr(sg, "_matches_any",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert R._in_scope("a.py", ["b.py"]) is True


# ─── the API the test demands of the impl ──────────────────────────────


def test_the_methods_the_test_calls_are_pulled_out():
    api = R._required_api_from_tests("g = Grid()\ng.place(1)\ng._is_valid(2)")
    assert "place" in api and "_is_valid" in api


def test_assertions_and_builtins_are_not_demanded_of_the_unit():
    """`.assertEqual(...)` on the test object is not the impl's API."""
    api = R._required_api_from_tests(
        "self.assertEqual(x.size(), 1)\nprint(x)\nx.toString()")
    assert api == ["size"]


def test_the_same_call_is_only_listed_once():
    assert R._required_api_from_tests("a.push()\nb.push()") == ["push"]


def test_the_api_list_is_capped_so_the_prompt_stays_small():
    src = "\n".join(f"o.m{i}()" for i in range(40))
    assert len(R._required_api_from_tests(src)) == 24


def test_no_tests_means_no_demanded_api():
    assert R._required_api_from_tests("") == []


# ─── language rules ────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["a/Array.hpp", "a/array.cpp", "x.h", "y.cc"])
def test_cpp_targets_are_told_templates_are_header_only(path):
    """A template body in a .cpp is a link error — observed on a split
    DynamicArray<T>."""
    assert "TEMPLATE" in R._lang_rules(path)


@pytest.mark.parametrize("path", ["a.py", "a.java", "", "a.rs"])
def test_other_languages_get_no_extra_rules(path):
    assert R._lang_rules(path) == ""


# ─── the tests-are-the-spec block ──────────────────────────────────────


def test_the_test_source_is_handed_over_as_ground_truth():
    block = R._tests_block("assert Board().color == 'cyan'")
    assert "THE TEST IS THE SPECIFICATION" in block
    assert "assert Board().color == 'cyan'" in block


def test_the_required_api_is_spelled_out_beside_the_tests():
    """A local model still misses a method the test calls — an uncompilable
    test file was the observed result."""
    assert "REQUIRED API" in R._tests_block("s.pop()")


def test_a_test_calling_nothing_needs_no_api_block():
    assert "REQUIRED API" not in R._tests_block("assert 1 == 1")


def test_no_tests_means_no_block_at_all():
    assert R._tests_block("") == ""


def test_a_huge_test_file_is_capped():
    block = R._tests_block("z" * 9000)
    assert "z" * 6000 in block and "z" * 6001 not in block


# ─── the single-shot prompt ────────────────────────────────────────────


def test_the_single_shot_prompt_carries_spec_target_and_goal():
    p = R._subtask_prompt({}, "# Spec", "pkg/lexer.py", "lex it")
    assert "# Spec" in p and "pkg/lexer.py" in p and "SUBTASK: lex it" in p
    assert "Output ONLY the file(s)" in p


def test_files_already_on_disk_are_named_so_imports_are_not_guessed():
    p = R._subtask_prompt({"_existing_files": "lexer.py: class Lexer"}, "",
                          "p.py", "g")
    assert "EXISTING PROJECT FILES" in p and "class Lexer" in p


def test_a_retry_leads_with_what_went_wrong_last_time():
    p = R._subtask_prompt({"_retry_error": "SyntaxError"}, "", "p.py", "g")
    assert p.startswith("⚠ YOUR PREVIOUS ATTEMPT FAILED")


def test_the_tests_and_language_rules_ride_along():
    p = R._subtask_prompt({"_tests": "b.pop()"}, "", "a.hpp", "g")
    assert "TESTS (ground truth)" in p and "TEMPLATE" in p


# ─── forcing the content onto the owned path ───────────────────────────


def test_the_block_whose_basename_matches_wins():
    out = R._remap_to_canonical({"miniLang/lexer.py": "a", "other.py": "b"},
                                "mini_lang/lexer.py")
    assert out == {"mini_lang/lexer.py": "a"}


def test_a_relabelled_single_block_is_forced_onto_the_target():
    out = R._remap_to_canonical({"whatever.py": "code"}, "pkg/lexer.py")
    assert out == {"pkg/lexer.py": "code"}


# ─── the syntax gate ───────────────────────────────────────────────────


def test_a_broken_file_is_rejected_before_it_reaches_the_worktree():
    """Lightweight writes files directly and an isolated worktree has no build
    marker — without this gate a truncated file surfaces only post-merge."""
    assert R._syntax_rejection("a.py", "def f(:\n") is not None


def test_a_good_file_passes_the_gate():
    assert R._syntax_rejection("a.py", "def f():\n    return 1\n") is None


def test_a_crashing_guard_never_takes_the_runner_down(monkeypatch):
    from aiforge_core.runtime import syntax_guard as sg
    monkeypatch.setattr(sg, "validate_syntax",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert R._syntax_rejection("a.py", "anything") is None


# ─── writing the files ─────────────────────────────────────────────────


def test_the_files_land_in_the_worktree(tmp_path):
    written, rejected, bad = R._write_subtask_files(
        {"pkg/a.py": "x = 1\n"}, str(tmp_path), [])
    assert written == ["pkg/a.py"] and not rejected and bad is None
    assert (tmp_path / "pkg" / "a.py").read_text() == "x = 1\n"


def test_a_write_outside_the_allowlist_is_rejected_not_written(tmp_path):
    written, rejected, _ = R._write_subtask_files(
        {"src/a.py": "x=1\n", "other/b.py": "y=1\n"}, str(tmp_path), ["src/**"])
    assert written == ["src/a.py"] and rejected == ["other/b.py"]
    assert not (tmp_path / "other").exists()


def test_a_path_climbing_out_of_the_worktree_is_refused(tmp_path):
    """Deleting ".." leaves "/etc/a.py", which os.path.join returns unchanged
    because it is ABSOLUTE — the write would land outside the worktree."""
    written, rejected, bad = R._write_subtask_files(
        {"/../etc/a.py": "x=1\n"}, str(tmp_path), [])
    assert written == [] and rejected == ["/etc/a.py"] and bad is None


def test_a_normal_nested_path_is_still_inside(tmp_path):
    written, rejected, _ = R._write_subtask_files({"a/b/c.py": "x=1\n"},
                                                  str(tmp_path), [])
    assert written == ["a/b/c.py"] and not rejected


def test_an_unresolvable_worktree_refuses_the_write(tmp_path):
    assert R._inside("relative/wt", "/abs/a.py") is False


def test_one_broken_file_stops_the_whole_write(tmp_path):
    written, _, bad = R._write_subtask_files({"a.py": "def f(:\n"},
                                             str(tmp_path), [])
    assert written == [] and bad is not None and bad.startswith("a.py: ")


def test_an_unwritable_destination_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(R.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    written, _, bad = R._write_subtask_files({"a.py": "x=1\n"},
                                             str(tmp_path), [])
    assert written == [] and bad is None


# ─── lightweight_run_one ───────────────────────────────────────────────


@pytest.fixture()
def one_shot(monkeypatch):
    """The single LLM call the lightweight runner spends."""
    from aiforge_core.llm import client as llm
    seen: dict = {"out": "=== pkg/lexer.py ===\nx = 1\n"}

    def _complete(role, convo, max_tokens=None):
        seen.update(role=role, prompt=convo[-1]["content"],
                    max_tokens=max_tokens)
        if isinstance(seen["out"], Exception):
            raise seen["out"]
        return seen["out"]
    monkeypatch.setattr(llm, "complete", _complete)
    return seen


def test_one_call_produces_the_files_on_disk(one_shot, tmp_path):
    res = R.lightweight_run_one({"goal": "lex", "path": "pkg/lexer.py"},
                                str(tmp_path))
    assert res == {"ok": True, "files": ["pkg/lexer.py"]}
    assert (tmp_path / "pkg" / "lexer.py").read_text() == "x = 1\n"
    assert one_shot["role"] == "doer"


def test_the_models_own_label_is_ignored_when_the_subtask_owns_a_path(
        one_shot, tmp_path):
    one_shot["out"] = "=== miniLang/lexer.py ===\nx = 1\n"
    R.lightweight_run_one({"path": "mini_lang/lexer.py"}, str(tmp_path))
    assert (tmp_path / "mini_lang" / "lexer.py").exists()
    assert not (tmp_path / "miniLang").exists()


def test_without_a_target_path_the_labels_are_honoured(one_shot, tmp_path):
    one_shot["out"] = "=== a.py ===\nx=1\n=== b.py ===\ny=1\n"
    res = R.lightweight_run_one({"goal": "g"}, str(tmp_path))
    assert sorted(res["files"]) == ["a.py", "b.py"]


def test_the_output_budget_comes_from_config_not_a_hardcoded_cap(
        one_shot, tmp_path, monkeypatch):
    """A hardcoded 2048 truncated a big test file mid-string — a SyntaxError
    that only surfaced at the post-merge integration test."""
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", "16000")
    R.lightweight_run_one({"path": "pkg/lexer.py"}, str(tmp_path))
    assert one_shot["max_tokens"] == 16000


@pytest.mark.parametrize("val,expected", [("not-a-number", 8192),
                                          ("512", 2048)])
def test_the_budget_has_a_sane_floor_and_default(one_shot, tmp_path,
                                                 monkeypatch, val, expected):
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", val)
    R.lightweight_run_one({"path": "pkg/lexer.py"}, str(tmp_path))
    assert one_shot["max_tokens"] == expected


def test_prose_instead_of_code_is_a_failed_subtask(one_shot, tmp_path):
    one_shot["out"] = "I would start by writing a lexer."
    assert R.lightweight_run_one({"goal": "g"}, str(tmp_path)) == {
        "ok": False, "error": "no file blocks produced"}


def test_a_model_error_fails_the_subtask_instead_of_raising(one_shot, tmp_path):
    one_shot["out"] = RuntimeError("rate limited")
    res = R.lightweight_run_one({"goal": "g"}, str(tmp_path))
    assert res == {"ok": False, "error": "rate limited"}


def test_a_broken_emitted_file_fails_with_the_syntax_error(one_shot, tmp_path):
    one_shot["out"] = "=== a.py ===\ndef f(:\n"
    res = R.lightweight_run_one({"goal": "g"}, str(tmp_path))
    assert res["ok"] is False and res["error"].startswith("a.py: ")


def test_everything_out_of_scope_is_a_failure_naming_the_rejects(one_shot,
                                                                 tmp_path):
    one_shot["out"] = "=== other/b.py ===\ny=1\n"
    res = R.lightweight_run_one({"goal": "g",
                                 "scope_allowlist_globs": ["src/**"]},
                                str(tmp_path))
    assert res["ok"] is False and res["rejected"] == ["other/b.py"]
    assert "all writes out of scope" in res["error"]


def test_a_partial_write_succeeds_but_reports_what_was_dropped(one_shot,
                                                               tmp_path):
    one_shot["out"] = "=== src/a.py ===\nx=1\n=== other/b.py ===\ny=1\n"
    res = R.lightweight_run_one({"goal": "g",
                                 "scope_allowlist_globs": ["src/**"]},
                                str(tmp_path))
    assert res["ok"] is True and res["rejected"] == ["other/b.py"]


def test_a_missing_llm_client_fails_the_light_runner_too(monkeypatch, tmp_path):
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "aiforge_core.llm.client":
            raise ImportError("no llm")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _imp)
    res = R.lightweight_run_one({"goal": "g"}, str(tmp_path))
    assert res == {"ok": False, "error": "no llm"}


# ─── which runner ──────────────────────────────────────────────────────


def test_the_cheap_runner_is_the_default(monkeypatch):
    monkeypatch.delenv("AIFORGE_PARALLEL_FULL_DOER", raising=False)
    assert R._default_subtask_runner() is R.lightweight_run_one


@pytest.mark.parametrize("val", ["1", "true"])
def test_the_full_doer_is_opt_in(monkeypatch, val):
    monkeypatch.setenv("AIFORGE_PARALLEL_FULL_DOER", val)
    assert R._default_subtask_runner() is R.default_run_one


def test_any_other_value_keeps_the_cheap_runner(monkeypatch):
    monkeypatch.setenv("AIFORGE_PARALLEL_FULL_DOER", "yes")
    assert R._default_subtask_runner() is R.lightweight_run_one


# ─── run_subtasks_parallel ─────────────────────────────────────────────


@pytest.fixture()
def parallel(monkeypatch):
    """The whole entry point's collaborators: subtask store, ticket store,
    workspace and the orchestrator."""
    import aiforge_core.runtime.parallel_subtasks as pkg
    from aiforge_core.tickets import store as tstore
    from aiforge_core.tickets import subtasks as st
    seen: dict = {"subs": [{"slug": "a"}, {"slug": "b"}], "statuses": [],
                  "agg": {"ok": True, "total": 2, "done": 2},
                  "decomposed": [], "emits": []}

    monkeypatch.setattr(st, "get_subtasks", lambda tid: seen["subs"])
    monkeypatch.setattr(st, "set_subtasks",
                        lambda tid, items, role=None: items)
    monkeypatch.setattr(pkg, "_decompose", lambda p: seen["decomposed"])
    monkeypatch.setattr(tstore, "update_status",
                        lambda tid, status, role=None:
                        seen["statuses"].append(status))
    monkeypatch.setattr(R, "_run_workspace", lambda t, tid: ("/wt", "main"))
    monkeypatch.setattr(R, "_emit",
                        lambda *a: seen["emits"].append(a))

    def _run_parallel(wt, base, tid, subs, run_one, **kw):
        seen.update(wt=wt, base=base, tid=tid, count=len(subs),
                    run_one=run_one)
        if isinstance(seen["agg"], Exception):
            raise seen["agg"]
        return seen["agg"]
    monkeypatch.setattr(R, "run_parallel", _run_parallel)
    R._INFLIGHT.clear()
    yield seen
    R._INFLIGHT.clear()


def _ticket(tid=7):
    return pytypes.SimpleNamespace(id=tid, identifier="ONE-7", title="t",
                                   body="b")


def test_the_ticket_is_fanned_out_and_closed_out(parallel):
    agg = R.run_subtasks_parallel(_ticket())
    assert agg == {"ok": True, "total": 2, "done": 2}
    assert parallel["statuses"] == ["in_progress", "done"]
    assert parallel["wt"] == "/wt" and parallel["base"] == "main"
    assert parallel["count"] == 2


def test_a_failed_run_blocks_the_ticket(parallel):
    parallel["agg"] = {"ok": False, "total": 2, "failed": 1}
    R.run_subtasks_parallel(_ticket())
    assert parallel["statuses"] == ["in_progress", "blocked"]


def test_a_crash_mid_run_still_blocks_the_ticket_and_re_raises(parallel):
    parallel["agg"] = RuntimeError("worktree gone")
    with pytest.raises(RuntimeError):
        R.run_subtasks_parallel(_ticket())
    assert parallel["statuses"] == ["in_progress", "blocked"]
    assert not R._INFLIGHT, "the in-flight guard is released on the way out"


def test_a_fresh_ticket_is_decomposed_on_demand(parallel):
    """"Run in parallel" must work straight from todo, before any planning."""
    parallel["subs"] = []
    parallel["decomposed"] = [{"slug": "x"}, {"slug": "y"}]
    R.run_subtasks_parallel(_ticket())
    assert parallel["count"] == 2


def test_a_ticket_that_will_not_split_is_left_alone(parallel):
    parallel["subs"] = []
    parallel["decomposed"] = [{"slug": "only-one"}]
    assert R.run_subtasks_parallel(_ticket()) == {
        "ok": True, "total": 0, "note": "could not decompose into subtasks"}
    assert parallel["statuses"] == [], "no status churn on a no-op"


def test_a_second_run_for_the_same_ticket_is_refused(parallel):
    """Concurrent POSTs would collide on the per-slug worktree paths."""
    R._INFLIGHT.add(7)
    assert R.run_subtasks_parallel(_ticket()) == {
        "ok": False, "error": "already running for this ticket"}


def test_a_different_ticket_may_run_alongside(parallel):
    R._INFLIGHT.add(99)
    assert R.run_subtasks_parallel(_ticket(7))["ok"] is True


def test_an_explicit_runner_overrides_the_default(parallel):
    mine = object()
    R.run_subtasks_parallel(_ticket(), run_one=mine)
    assert parallel["run_one"] is mine


def test_the_review_is_emitted_with_the_run_totals(parallel):
    parallel["agg"] = {"ok": True, "total": 2, "done": 2, "merged": 2,
                       "review": "looks good"}
    R.run_subtasks_parallel(_ticket())
    _tid, slug, kind, review, counts = parallel["emits"][0]
    assert (slug, kind, review) == ("*", "parallel_review", "looks good")
    assert counts["total"] == 2 and counts["merged"] == 2


def test_a_status_store_that_is_down_does_not_fail_the_run(monkeypatch):
    store = pytypes.SimpleNamespace(
        update_status=lambda *a, **k: (_ for _ in ()).throw(OSError("db")))
    R._set_status(store, 1, "done")  # no raise


# ─── the workspace a run happens in ────────────────────────────────────


def test_a_repo_ticket_merges_into_its_own_working_branch(monkeypatch):
    from aiforge_core.runtime import workspace as ws
    monkeypatch.setattr(ws, "ensure_branch_and_worktree", lambda t: "/repo/wt")
    monkeypatch.setattr(R, "_git", lambda args, wt: pytypes.SimpleNamespace(
        stdout="feature/one-7\n"))
    assert R._run_workspace(_ticket(), 7) == ("/repo/wt", "feature/one-7")


def test_a_detached_checkout_falls_back_to_head(monkeypatch):
    from aiforge_core.runtime import workspace as ws
    monkeypatch.setattr(ws, "ensure_branch_and_worktree", lambda t: "/repo/wt")
    monkeypatch.setattr(R, "_git",
                        lambda args, wt: pytypes.SimpleNamespace(stdout=""))
    assert R._run_workspace(_ticket(), 7)[1] == "HEAD"


def test_a_standalone_ticket_gets_its_own_git_workspace(monkeypatch, tmp_path):
    """Without a target repo the parallel run must still work end-to-end."""
    from aiforge_core.config import paths
    from aiforge_core.runtime import workspace as ws
    monkeypatch.setattr(ws, "ensure_branch_and_worktree", lambda t: None)
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(R, "_ensure_git_workspace", lambda wt: "main")
    wt, base = R._run_workspace(_ticket(), 7)
    assert wt == os.path.join(str(tmp_path), "ticket-workspaces", "ONE-7")
    assert base == "main"
