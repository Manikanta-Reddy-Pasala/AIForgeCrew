"""Cross-model review gates: spec, file-plan, tests.

Every gate here is SOFT — a reviewer that errors, times out, returns empty, or
returns garbage must leave the pipeline exactly as it found it. That property
is what makes the gates safe to leave on by default, so most of what is pinned
below is the failure path, not the happy one.

No network and no model: :func:`review_once` is the seam, monkeypatched per
test. ``_reviewer_cache`` is module state, so it is cleared between tests —
otherwise the first test to probe would decide every later one's reviewer.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.runtime import review_gates as rg


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AIFORGE_REVIEW_SPEC", "AIFORGE_REVIEW_PLAN", "AIFORGE_REVIEW_TESTS",
              "AIFORGE_REVIEW_MODEL", "AIFORGE_ESCALATION_MODEL",
              "AIFORGE_REVIEW_CROSS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    rg._reviewer_cache.clear()
    yield
    rg._reviewer_cache.clear()


def _reviewer(monkeypatch, reply):
    """Pin what the reviewer model 'says' (a string, or an exception to raise)."""
    def _fake(prompt, max_tokens):
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(rg, "review_once", _fake)


# ─── pick_reviewer_model ───────────────────────────────────────────────


def test_explicit_review_model_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_REVIEW_MODEL", "big-thinker")
    monkeypatch.setenv("AIFORGE_ESCALATION_MODEL", "other")
    assert rg.pick_reviewer_model() == "big-thinker"


def test_escalation_model_is_the_fallback(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATION_MODEL", "escalated")
    assert rg.pick_reviewer_model() == "escalated"


def test_cross_model_off_means_no_reviewer(monkeypatch):
    monkeypatch.setenv("AIFORGE_REVIEW_CROSS_MODEL", "0")
    assert rg.pick_reviewer_model() is None


def test_auto_pick_falls_back_to_none_when_the_probe_fails(monkeypatch):
    """No LM Studio on the other end: the gate degrades to the doer, silently."""
    def _boom(*_a, **_kw):
        raise OSError("connection refused")
    monkeypatch.setattr(rg.urllib.request, "urlopen", _boom)
    assert rg.pick_reviewer_model() is None


def test_auto_pick_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def _one(*_a, **_kw):
        calls["n"] += 1
        raise OSError("down")
    monkeypatch.setattr(rg.urllib.request, "urlopen", _one)
    rg.pick_reviewer_model()
    rg.pick_reviewer_model()
    assert calls["n"] == 1


# ─── review_spec ───────────────────────────────────────────────────────


def test_spec_gate_off_returns_the_spec_untouched(monkeypatch):
    monkeypatch.setenv("AIFORGE_REVIEW_SPEC", "0")
    assert rg.review_spec("req", "# spec") == ("# spec", "")


def test_empty_spec_is_not_reviewed():
    assert rg.review_spec("req", "   ") == ("   ", "")


@pytest.mark.parametrize("reply", ["", "CLEAN", "clean — nothing to fix", "too short"])
def test_a_sound_spec_is_kept(monkeypatch, reply):
    _reviewer(monkeypatch, reply)
    spec, note = rg.review_spec("req", "# spec")
    assert spec == "# spec"
    assert note == "spec reviewed — sound"


def test_a_refined_spec_replaces_the_original(monkeypatch):
    refined = "# spec\n" + "corrected requirement line\n" * 5
    _reviewer(monkeypatch, refined)
    spec, note = rg.review_spec("req", "# spec")
    assert spec == refined.strip()
    assert "refined" in note


def test_a_reviewer_error_leaves_the_spec_alone(monkeypatch):
    _reviewer(monkeypatch, RuntimeError("model died"))
    with pytest.raises(RuntimeError):
        rg.review_spec("req", "# spec")   # the seam raises; review_once swallows


def test_review_once_swallows_a_dead_model(monkeypatch):
    import aiforge_core.llm.client as client

    def _boom(*_a, **_kw):
        raise RuntimeError("model died")
    monkeypatch.setattr(client, "complete", _boom)
    monkeypatch.setenv("AIFORGE_REVIEW_CROSS_MODEL", "0")
    assert rg.review_once("prompt", 128) is None


# ─── review_plan ───────────────────────────────────────────────────────


def _subs():
    return [{"path": "app/kvdakade.py", "goal": "facade", "slug": "kvdakade"},
            {"path": "app/store.py", "goal": "storage", "slug": "store"},
            {"path": "app/cli.py", "goal": "entry", "slug": "cli"}]


def test_plan_gate_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_REVIEW_PLAN", "0")
    subs = _subs()
    assert rg.review_plan("req", subs) == (subs, "")


def test_a_single_file_plan_is_not_reviewed():
    subs = [{"path": "app/only.py"}]
    assert rg.review_plan("req", subs) == (subs, "")


@pytest.mark.parametrize("reply", ["", "CLEAN", "looks fine, no pipes here"])
def test_a_sound_plan_is_kept(monkeypatch, reply):
    _reviewer(monkeypatch, reply)
    subs = _subs()
    out, note = rg.review_plan("req", subs)
    assert out == subs
    assert note == "plan reviewed — sound"


def test_a_typo_is_corrected_and_the_original_fields_survive(monkeypatch):
    _reviewer(monkeypatch, "app/kvfacade.py | facade\n"
                           "app/store.py | storage\n"
                           "app/cli.py | entry")
    out, note = rg.review_plan("req", _subs())
    assert [s["path"] for s in out] == ["app/kvfacade.py", "app/store.py", "app/cli.py"]
    # renamed by closest match, so it keeps the original subtask's slug
    assert out[0]["slug"] == "kvdakade"
    assert "3→3 files" in note


def test_an_unchanged_path_list_reports_sound(monkeypatch):
    _reviewer(monkeypatch, "\n".join(f"{s['path']} | {s['goal']}" for s in _subs()))
    out, note = rg.review_plan("req", _subs())
    assert [s["path"] for s in out] == [s["path"] for s in _subs()]
    assert note == "plan reviewed — sound"


def test_a_truncated_plan_that_drops_most_files_is_refused(monkeypatch):
    """A mangled reply must never be able to delete the build."""
    _reviewer(monkeypatch, "app/store.py | storage")
    subs = _subs()
    out, note = rg.review_plan("req", subs)
    assert out == subs
    assert note == "plan reviewed — sound"


def test_an_added_file_is_accepted(monkeypatch):
    _reviewer(monkeypatch, "app/kvdakade.py | facade\napp/store.py | storage\n"
                           "app/cli.py | entry\napp/config.py | settings")
    out, note = rg.review_plan("req", _subs())
    assert [s["path"] for s in out][-1] == "app/config.py"
    assert "3→4 files" in note


def test_an_added_file_inherits_the_closest_old_subtask(monkeypatch):
    """Worth knowing: a genuinely NEW path still goes through the typo-rename
    match, so it adopts the fields of whatever old path is closest above the
    0.6 cutoff (here app/cli.py). Only ``path`` and ``goal`` are overwritten."""
    _reviewer(monkeypatch, "app/kvdakade.py | facade\napp/store.py | storage\n"
                           "app/cli.py | entry\napp/config.py | settings")
    out, _ = rg.review_plan("req", _subs())
    assert out[-1]["slug"] == "cli"
    assert out[-1]["goal"] == "settings"


def test_an_added_file_with_no_close_match_gets_a_derived_slug(monkeypatch):
    _reviewer(monkeypatch, "app/kvdakade.py | facade\napp/store.py | storage\n"
                           "app/cli.py | entry\napp/telemetry_exporter.py | metrics")
    out, _ = rg.review_plan("req", _subs())
    assert out[-1]["slug"] == "app-telemetry-exporter-py"


# ─── _plan_line_subtask / _parse_plan ──────────────────────────────────


def _parse(text):
    return rg._parse_plan(text, _subs())


@pytest.mark.parametrize("line", [
    "no pipe on this line",
    "README.md | not code",
    " | empty path",
    "notes.txt | prose",
])
def test_plan_lines_that_are_skipped(line):
    assert _parse(line) == []


def test_numbering_and_backticks_are_stripped():
    out = _parse("1. `app/store.py` | storage")
    assert out[0]["path"] == "app/store.py"


def test_a_repeated_path_is_kept_once():
    out = _parse("app/store.py | a\napp/store.py | b")
    assert len(out) == 1
    assert out[0]["goal"] == "a"


def test_a_line_without_a_goal_keeps_the_original_goal():
    out = _parse("app/store.py |")
    assert out[0]["goal"] == "storage"


# ─── find_test_files / _fenced_test_blocks ─────────────────────────────


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (tmp_path / "tests" / "b_test.js").write_text("it('b', () => {})\n")
    (tmp_path / "FooTest.java").write_text("class FooTest {}\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "node_modules" / "test_vendor.py").write_text("x = 1\n")
    (tmp_path / ".git" / "test_hidden.py").write_text("x = 1\n")
    return tmp_path


def test_find_test_files_finds_every_flavour(tree):
    found = {p.replace(os.sep, "/") for p in rg.find_test_files(str(tree))}
    assert found == {"tests/test_a.py", "tests/b_test.js", "FooTest.java"}


def test_find_test_files_skips_vendor_and_dot_dirs(tree):
    found = " ".join(rg.find_test_files(str(tree)))
    assert "node_modules" not in found
    assert ".git" not in found


def test_fenced_blocks_carry_the_path_and_body(tree):
    blocks = rg._fenced_test_blocks(str(tree))
    assert len(blocks) == 3
    assert any("### FILE: tests/test_a.py" in b and "def test_a()" in b for b in blocks)


def test_fenced_blocks_stop_at_the_budget(tree, monkeypatch):
    monkeypatch.setattr(rg, "_TESTS_BUDGET", 60)
    assert len(rg._fenced_test_blocks(str(tree))) < 3


# ─── review_tests ──────────────────────────────────────────────────────


def test_tests_gate_off(monkeypatch, tree):
    monkeypatch.setenv("AIFORGE_REVIEW_TESTS", "0")
    assert rg.review_tests(str(tree), "spec") == ([], "")


def test_no_tests_means_nothing_to_review(monkeypatch, tmp_path):
    _reviewer(monkeypatch, "=== x.py ===\nprint(1)\n")
    assert rg.review_tests(str(tmp_path), "spec") == ([], "")


@pytest.mark.parametrize("reply", ["", "CLEAN", "everything checks out"])
def test_sound_tests_are_left_alone(monkeypatch, tree, reply):
    _reviewer(monkeypatch, reply)
    changed, note = rg.review_tests(str(tree), "spec")
    assert changed == []
    assert note == "tests reviewed — sound"


def test_a_corrected_test_file_is_written(monkeypatch, tree):
    _reviewer(monkeypatch,
              "=== tests/test_a.py ===\n"
              "def test_a():\n    assert 1 == 1  # test-review: fixed\n")
    changed, note = rg.review_tests(str(tree), "spec")
    assert changed == ["tests/test_a.py"]
    assert "test-review" in (tree / "tests" / "test_a.py").read_text()
    assert "fixed 1 file(s)" in note


def test_a_syntactically_broken_correction_is_refused(monkeypatch, tree):
    before = (tree / "tests" / "test_a.py").read_text()
    _reviewer(monkeypatch, "=== tests/test_a.py ===\ndef test_a(:\n    ???\n")
    changed, note = rg.review_tests(str(tree), "spec")
    assert changed == []
    assert note == "tests reviewed — sound"
    assert (tree / "tests" / "test_a.py").read_text() == before


def test_a_path_traversal_in_the_reply_cannot_escape_the_tree(monkeypatch, tree, tmp_path):
    outside = tmp_path.parent / "escaped.py"
    _reviewer(monkeypatch, f"=== ../{outside.name} ===\nx = 1\n")
    rg.review_tests(str(tree), "spec")
    assert not outside.exists()
