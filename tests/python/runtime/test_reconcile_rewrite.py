"""The patch applier and the minimal-context rewrite prompt.

The applier is surgical on purpose: a SEARCH block that does not match the
file character-for-character is RECORDED and skipped, never approximated, and
nothing is written that fails the syntax guard. The prompt builder is the other
half — it exists to keep a local model's window small, so the tests pin what
goes in (goal, mandates, hints, repo map, bounded file blocks) and what the
escalation path changes (whole files instead of patches, its own temperature
and token cap).
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.parallel_subtasks._reconcile import _rewrite as rw


def _patch(rel, search, replace):
    return (f"### FILE: {rel}\n"
            f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n")


# ─── the syntax guard ──────────────────────────────────────────────────


def test_broken_content_is_refused():
    assert rw._syntax_ok("a.py", "def (:\n") is False


def test_valid_content_passes():
    assert rw._syntax_ok("a.py", "x = 1\n") is True


def test_an_unavailable_guard_never_blocks_a_patch(monkeypatch):
    """False must mean PROVEN broken — a missing guard is not evidence."""
    import aiforge_core.runtime.syntax_guard as sg

    def _boom(*_a):
        raise RuntimeError("guard unavailable")
    monkeypatch.setattr(sg, "validate_syntax", _boom)
    assert rw._syntax_ok("a.py", "def (:\n") is True


# ─── applying patches ──────────────────────────────────────────────────


def test_an_exact_search_is_swapped(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    written, failures = rw._apply_patches(str(tmp_path), _patch("a.py", "x = 1", "x = 99"))
    assert written == ["a.py"] and failures == []
    assert (tmp_path / "a.py").read_text() == "x = 99\ny = 2\n"


def test_only_the_first_occurrence_is_replaced(tmp_path):
    (tmp_path / "a.py").write_text("v = 1\nv = 1\n")
    rw._apply_patches(str(tmp_path), _patch("a.py", "v = 1", "v = 2"))
    assert (tmp_path / "a.py").read_text() == "v = 2\nv = 1\n"


def test_a_search_that_does_not_match_is_recorded_not_guessed(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    written, failures = rw._apply_patches(str(tmp_path), _patch("a.py", "x  =  1", "x = 2"))
    assert written == []
    assert failures == [("a.py", "SEARCH block not found (indent/char mismatch)")]
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_a_patch_for_a_missing_file_is_recorded(tmp_path):
    written, failures = rw._apply_patches(str(tmp_path), _patch("ghost.py", "a", "b"))
    assert written == []
    assert failures == [("ghost.py", "file not found")]


def test_output_with_no_file_headers(tmp_path):
    assert rw._apply_patches(str(tmp_path), "sorry, I can't help with that") == (
        [], [("", "no ### FILE headers")])


def test_a_patch_that_breaks_syntax_is_rolled_back(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    written, failures = rw._apply_patches(
        str(tmp_path), _patch("a.py", "    return 1", "    return ((("))
    assert written == []
    assert failures == [("a.py", "syntax broke after patch")]
    assert (tmp_path / "a.py").read_text() == "def f():\n    return 1\n"


def test_a_no_op_patch_writes_nothing(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    written, failures = rw._apply_patches(str(tmp_path), _patch("a.py", "x = 1", "x = 1"))
    assert written == [] and failures == []


def test_several_files_in_one_reply(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    out = _patch("a.py", "x = 1", "x = 2") + _patch("b.py", "y = 1", "y = 2")
    written, failures = rw._apply_patches(str(tmp_path), out)
    assert written == ["a.py", "b.py"] and failures == []


def test_two_blocks_for_the_same_file_both_apply(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 1\n")
    seg = ("### FILE: a.py\n"
           "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
           "<<<<<<< SEARCH\ny = 1\n=======\ny = 2\n>>>>>>> REPLACE\n")
    rw._apply_patches(str(tmp_path), seg)
    assert (tmp_path / "a.py").read_text() == "x = 2\ny = 2\n"


@pytest.mark.parametrize("rel", ["../../etc/passwd", "../outside.py"])
def test_a_patch_path_outside_the_workspace_is_refused(tmp_path, rel):
    """The path comes from the model. Stripping ".." leaves "//etc/passwd",
    which os.path.join keeps ABSOLUTE — the applier would have read and
    rewritten a file outside the workspace."""
    written, failures = rw._apply_patches(str(tmp_path), _patch(rel, "a", "b"))
    assert written == []
    assert failures[0][1] == "path outside the workspace"


def test_a_whole_file_outside_the_workspace_is_refused(tmp_path):
    written: list = []
    rw._write_whole_files(str(tmp_path), "=== ../../tmp/escaped.py ===\nx = 1\n", written)
    assert written == []


def test_an_unreadable_target_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    real_open = open

    def _fussy(path, *a, **kw):
        if str(path).endswith("a.py"):
            raise PermissionError("locked")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _fussy)
    assert rw._apply_patches(str(tmp_path), _patch("a.py", "x = 1", "x = 2")) == ([], [])


# ─── whole-file fallback ───────────────────────────────────────────────


def test_whole_files_are_accepted_when_the_model_ignored_the_patch_format(tmp_path):
    written: list = []
    rw._write_whole_files(str(tmp_path), "=== app/a.py ===\nx = 2\n", written)
    assert written == ["app/a.py"]
    assert (tmp_path / "app/a.py").read_text() == "x = 2\n"


def test_a_broken_whole_file_is_refused(tmp_path):
    written: list = []
    rw._write_whole_files(str(tmp_path), "=== a.py ===\ndef (:\n", written)
    assert written == [] and not (tmp_path / "a.py").exists()


def test_an_empty_whole_file_is_skipped(tmp_path):
    written: list = []
    rw._write_whole_files(str(tmp_path), "=== a.py ===\n   \n", written)
    assert written == []


def test_an_unwritable_whole_file_is_skipped(tmp_path, monkeypatch):
    def _boom(*_a, **_kw):
        raise PermissionError("read-only")
    monkeypatch.setattr(rw.os, "makedirs", _boom)
    written: list = []
    rw._write_whole_files(str(tmp_path), "=== app/a.py ===\nx = 2\n", written)
    assert written == []


# ─── bounded context ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("100", 100), ("1", 10), ("junk", 50)])
def test_int_env_clamps_and_falls_back(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_X", raw)
    assert rw._int_env("AIFORGE_X", 50, 10) == expected


def test_file_blocks_are_fenced_and_bounded(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y" * 5000)
    blocks = rw._file_blocks(str(tmp_path), "fail in a.py and b.py", 200)
    assert any(b.startswith("### FILE: a.py\n```") for b in blocks)
    assert all(len(b) <= 200 for b in blocks)


def test_the_repo_map_is_bounded(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_RECONCILE_REPOMAP", raising=False)
    import aiforge_core.memory.code_context as cc
    monkeypatch.setattr(cc, "aider_digest", lambda cwd, files: "sym " * 5000)
    assert len(rw._reconcile_repomap(str(tmp_path))) == 4000


def test_the_repo_map_can_be_turned_off(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_RECONCILE_REPOMAP", "0")
    import aiforge_core.memory.code_context as cc
    monkeypatch.setattr(cc, "aider_digest",
                        lambda *a: pytest.fail("built a repo map with the gate off"))
    assert rw._reconcile_repomap(str(tmp_path)) == ""


def test_a_repo_map_failure_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_RECONCILE_REPOMAP", raising=False)
    import aiforge_core.memory.code_context as cc

    def _boom(*_a):
        raise RuntimeError("index missing")
    monkeypatch.setattr(cc, "aider_digest", _boom)
    assert rw._reconcile_repomap(str(tmp_path)) == ""


# ─── the fix prompt ────────────────────────────────────────────────────


@pytest.fixture()
def prompt_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_RECONCILE_REPOMAP", "0")
    (tmp_path / "SPEC.md").write_text("## Goal\nBuild an LRU cache.\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    return tmp_path


def test_the_prompt_carries_goal_output_hints_and_files(prompt_env):
    p = rw._fix_prompt(str(prompt_env), "E fail in a.py", ["Binary vs BinaryExpr"],
                       False, [])
    assert "ORIGINAL GOAL:" in p and "Build an LRU cache." in p
    assert "KNOWN MISMATCHES TO RECONCILE:\n- Binary vs BinaryExpr" in p
    assert "### FILE: a.py" in p
    assert "FAILING TEST/BUILD OUTPUT:" in p


def test_user_mandates_are_marked_as_overriding(prompt_env):
    p = rw._fix_prompt(str(prompt_env), "out", [], False, ["must log errors"])
    assert "MANDATORY" in p and "- must log errors" in p


def test_a_spec_less_workspace_omits_the_goal_block(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_RECONCILE_REPOMAP", "0")
    assert "ORIGINAL GOAL:" not in rw._fix_prompt(str(tmp_path), "out", [], False, [])


def test_the_audit_principle_is_swapped_in_for_the_test_wins_one(prompt_env):
    audit = rw._fix_prompt(str(prompt_env), "out", [], True, [])
    plain = rw._fix_prompt(str(prompt_env), "out", [], False, [])
    assert rw._AUDIT_PRINCIPLE in audit and rw._TEST_WINS_PRINCIPLE not in audit
    assert rw._TEST_WINS_PRINCIPLE in plain


def test_the_failing_output_is_tailed(prompt_env):
    p = rw._fix_prompt(str(prompt_env), "HEAD" + "z" * 5000 + "TAIL", [], False, [])
    assert "TAIL" in p and "HEAD" not in p


# ─── escalation ────────────────────────────────────────────────────────


def test_the_escalation_temperature_comes_from_the_override_table(monkeypatch):
    import aiforge_core.config.model_overrides as mo
    monkeypatch.setattr(mo, "lookup", lambda m: {"temperature": 0.6})
    assert rw._escalation_temperature("big-thinker") == 0.6


def test_no_override_means_no_pinned_temperature(monkeypatch):
    import aiforge_core.config.model_overrides as mo
    monkeypatch.setattr(mo, "lookup", lambda m: None)
    assert rw._escalation_temperature("m") is None


def test_a_broken_override_table_is_not_fatal(monkeypatch):
    import aiforge_core.config.model_overrides as mo

    def _boom(_m):
        raise RuntimeError("bad table")
    monkeypatch.setattr(mo, "lookup", _boom)
    assert rw._escalation_temperature("m") is None


# ─── the resolver ──────────────────────────────────────────────────────


@pytest.fixture()
def llm(monkeypatch):
    """Capture the one completion call the resolver makes."""
    import aiforge_core.llm.client as client
    seen: dict = {}

    def _complete(role, messages, **kw):
        seen["role"] = role
        seen["system"] = messages[0]["content"]
        seen["prompt"] = messages[1]["content"]
        seen.update(kw)
        return seen.get("reply", "")
    monkeypatch.setattr(client, "complete", _complete)
    monkeypatch.setenv("AIFORGE_RECONCILE_REPOMAP", "0")
    return seen


def test_the_default_path_asks_for_patches(llm, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    llm["reply"] = _patch("a.py", "x = 1", "x = 2")
    assert rw._rewrite_fix(str(tmp_path), "fail in a.py", []) == ["a.py"]
    assert llm["system"] == rw._PATCH_SYS
    assert llm["extras"] is None


def test_escalation_asks_for_whole_files_instead(llm, tmp_path, monkeypatch):
    """A reasoning model cannot reliably reproduce a char-perfect SEARCH block,
    so the escalation round switches format rather than losing the round."""
    import aiforge_core.config.model_overrides as mo
    monkeypatch.setattr(mo, "lookup", lambda m: {"temperature": 0.6})
    llm["reply"] = "=== a.py ===\nx = 2\n"
    assert rw._rewrite_fix(str(tmp_path), "out", [], model="big-thinker") == ["a.py"]
    assert llm["system"] == rw._WHOLE_FILE_SYS
    assert llm["extras"] == {"model": "big-thinker"}
    assert llm["temperature"] == 0.6
    assert rw._WHOLE_FILE_OVERRIDE in llm["prompt"]


def test_escalation_caps_its_completion_length(llm, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", "8192")
    monkeypatch.setenv("AIFORGE_ESCALATION_MAX_TOKENS", "2560")
    rw._rewrite_fix(str(tmp_path), "out", [], model="m")
    assert llm["max_tokens"] == 2560


def test_a_failed_patch_round_falls_back_to_whole_files(llm, tmp_path):
    """The model ignored the patch format entirely — accept the files rather
    than burn the round."""
    llm["reply"] = "=== a.py ===\nx = 2\n"
    assert rw._rewrite_fix(str(tmp_path), "out", []) == ["a.py"]
    assert (tmp_path / "a.py").read_text() == "x = 2\n"


def test_an_empty_reply_writes_nothing(llm, tmp_path):
    llm["reply"] = ""
    assert rw._rewrite_fix(str(tmp_path), "out", []) == []


def test_user_mandates_for_this_workspace_reach_the_prompt(llm, tmp_path):
    from aiforge_core.runtime.parallel_subtasks._stream import _USER_MANDATES
    _USER_MANDATES[str(tmp_path)] = ["must keep the CLI flags"]
    try:
        rw._rewrite_fix(str(tmp_path), "out", [])
        assert "must keep the CLI flags" in llm["prompt"]
    finally:
        _USER_MANDATES.pop(str(tmp_path), None)
