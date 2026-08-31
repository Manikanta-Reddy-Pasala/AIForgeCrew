"""The shared preference-cue gate.

Three capture paths (rule_capture, preference_capture, chat_learner's LLM-down
fallback) used to hand-roll near-identical cue regexes and drift apart, so a
message caught by one was missed by another. This pins BOTH halves of the
contract of the single table they share now: every cue the former gates keyed
on still fires, and an ordinary task still does NOT — the gate's whole point is
keeping the LLM classify call off the hot path.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.capture_cues import has_cue


@pytest.mark.parametrize("text", [
    "always run the tests before committing",
    "never force-push to main",
    "From now on use ruff instead of flake8",
    "going forward, tag releases with v-prefix",
    "whenever a test fails, show the diff",
    "remember that the staging db is read-only",
    "I prefer tabs",
    "i want short commit messages",
    "i like the terse style",
    "my preference is squash merges",
    "my default is the dark theme",
    "make sure to update the changelog",
    "by default, skip the slow tests",
    "the default branch is main",
    "use pytest as the runner",
    "use uv for the venv",
    "set AIFORGE_REQUIRE_HTTPS when deploying",
    "don't ask before committing",
    "dont commit generated files",
    "do not touch the vendored code",
    "auto-approve the safe tools",
    "auto commit after every green run",
    "commit directly, without asking",
    "stop asking about formatting",
    "no need to ask for read-only commands",
    "this is a rule: two-space indents",
    "our convention is snake_case",
    "that setting should persist",
    "for all repos, use the same hooks",
    "for every PR, run the linter",
])
def test_cue_fires(text):
    assert has_cue(text) is True


@pytest.mark.parametrize("text", [
    "fix the bug in the parser",
    "why is this test failing?",
    "add a column to the users table",
    "run the build",
    "explain what this function does",
    "the deploy finished at 3pm",
])
def test_ordinary_task_pays_nothing(text):
    assert has_cue(text) is False


@pytest.mark.parametrize("text", ["", None])
def test_empty_input(text):
    assert has_cue(text) is False


def test_cues_are_case_insensitive():
    assert has_cue("ALWAYS squash") is True
    assert has_cue("Never rebase") is True


def test_cue_matches_on_word_boundaries_only():
    # "prefer" inside a longer word is not a preference cue.
    assert has_cue("the preferential path was taken") is False
    assert has_cue("nevermore") is False


def test_set_cue_reaches_past_the_first_letter():
    """``set\\s+\\w`` was dead: the alternation ends in ``\\b``, so a one-char
    match only held when the following word was a single letter. "set the
    default" and "set AIFORGE_X" — the two shapes the cue exists for — both
    fell through the gate."""
    assert has_cue("set AIFORGE_REQUIRE_HTTPS when deploying") is True
    assert has_cue("set the timeout to 30") is True
