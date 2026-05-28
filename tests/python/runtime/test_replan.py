"""Tests for the REPLAN signal helper (gap A2).

``should_replan`` decides whether the Doer loop has thrashed enough that
the next attempt deserves a fresh/narrowed plan instead of another blind
Doer retry. ``build_replan_note`` formats the short instruction string
stashed into session state for a subsequent Planner invocation.
"""
from __future__ import annotations

from aiforge_core.runtime.replan import build_replan_note, should_replan


class TestShouldReplan:
    def test_two_consecutive_fails_triggers(self):
        assert should_replan(["fail", "fail"]) is True

    def test_trailing_fails_among_history_triggers(self):
        assert should_replan(["pass", "fail", "fail"]) is True

    def test_fail_then_pass_does_not_trigger(self):
        assert should_replan(["fail", "pass"]) is False

    def test_pass_only_does_not_trigger(self):
        assert should_replan(["pass"]) is False

    def test_empty_does_not_trigger(self):
        assert should_replan([]) is False

    def test_single_fail_below_default_threshold(self):
        # default max_fail=2 needs two trailing fails
        assert should_replan(["fail"]) is False

    def test_custom_max_fail_three(self):
        assert should_replan(["fail", "fail"], max_fail=3) is False
        assert should_replan(["fail", "fail", "fail"], max_fail=3) is True

    def test_custom_max_fail_one(self):
        assert should_replan(["fail"], max_fail=1) is True
        assert should_replan(["pass"], max_fail=1) is False

    def test_mixed_verdicts_only_last_n_matter(self):
        # last two are fail -> trigger even though earlier ones passed
        assert should_replan(["pass", "pass", "fail", "fail"]) is True
        # last two are not both fail
        assert should_replan(["fail", "fail", "pass", "fail"]) is False


class TestBuildReplanNote:
    def test_note_mentions_count_and_rationale(self):
        note = build_replan_note(["fail", "fail"], "tests still red")
        assert "2" in note
        assert "tests still red" in note

    def test_note_suggests_narrowing(self):
        note = build_replan_note(["fail", "fail"], "compile error")
        low = note.lower()
        assert "narrow" in low or "smaller" in low

    def test_note_is_a_nonempty_string(self):
        note = build_replan_note(["fail"], "")
        assert isinstance(note, str)
        assert note.strip()
