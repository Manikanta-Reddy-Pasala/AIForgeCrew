"""The echo guard: a "next step" that is the finished request over again.

The production failure. Asked what comes next after a request it had already
answered in full, the model handed back a rewording of that request — on every
one of the first three live predictions, each with no tool and each at
confidence 0.95. The chip acted on it, sent the text as a fresh chat message,
and the user's own question ran a second time.

Two independent defences, tested separately here because either one alone would
have stopped it and neither is asked to be perfect:

* ``_risk`` refuses to ACT on a prediction that names no tool, whatever the
  confidence. This is the one that makes the failure impossible rather than
  unlikely.
* ``_predict.is_restatement`` drops a prediction that is mostly a rewording of
  the turn it followed. Lexical, so it catches the blatant case and not the
  artful paraphrase — a filter, never the safety argument.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import next_step
from aiforge_core.runtime.next_step import _predict, _risk


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("AIFORGE_PREDICT_DISABLE", "AIFORGE_PREDICT_ACT",
              "AIFORGE_PREDICT_MIN_CONFIDENCE"):
        monkeypatch.delenv(k, raising=False)


def _reply(monkeypatch, raw):
    monkeypatch.setattr(_predict, "_llm", lambda *a, **k: raw)


def _ctx(**kw):
    base = {"message": "", "did": "", "repo": "AIForgeCrew", "clean_tree": True}
    return {**base, **kw}


# ── normalising, so two sentences can be compared at all ─────────────────

def test_connectives_do_not_count_as_agreement():
    """Left in, "what does the ... have" would agree with anything."""
    assert _predict._content_words("what does the reindex have") == {"reindex"}


def test_a_plural_matches_its_singular():
    """Correct linguistics is not the goal — both sides are mangled the same
    way, so a consistent rule compares exactly as well as a right one."""
    assert _predict._content_words("protects") == _predict._content_words("protect")
    assert "lock" in _predict._content_words("locks")


def test_a_double_s_is_left_alone():
    assert "class" in _predict._content_words("class")


def test_punctuation_and_case_are_not_differences():
    assert (_predict._content_words("Reindex, chat_pipeline.")
            == _predict._content_words("reindex CHAT_PIPELINE"))


def test_a_path_survives_as_one_word():
    assert "deploy/env.py" in _predict._content_words("read deploy/env.py now")


# ── the ratio, at its edges ──────────────────────────────────────────────
#
# Deterministic tokens, so these pin the threshold itself rather than the
# vocabulary of some example sentence.

_ACTION = "alpha beta gamma delta epsilon"


def test_above_the_ratio_is_an_echo():
    """4 of 5 content words shared = 0.8."""
    assert _predict._echoes(_ACTION, "alpha beta gamma delta zeta") is True


def test_below_the_ratio_is_not():
    """3 of 5 = 0.6. A next step may share most of its subject with the
    request and still be a different action."""
    assert _predict._echoes(_ACTION, "alpha beta gamma zeta eta") is False


def test_a_real_follow_up_clears_the_bar():
    assert _predict._echoes("run the tests for chat_pipeline",
                            "fix the run lock in chat_pipeline") is False


def test_the_worst_observed_echo_is_caught():
    """Verbatim from ~/.aiforge/next_step_history.json on the NUC."""
    assert _predict._echoes(
        "Explain what the run lock in chat_pipeline protects.",
        "In one short sentence: what does the run lock in chat_pipeline "
        "protect?") is True


# ── what is too small to judge ───────────────────────────────────────────

@pytest.mark.parametrize("action", ["", "   ", "go", "do it", "check it"])
def test_a_terse_action_is_never_called_an_echo(action):
    """Guessing on two or three words would silently drop good short
    suggestions, and the cost of a wrong drop is a feature that seems dead."""
    assert _predict.is_restatement(action, _ctx(message=action)) is False


def test_nothing_to_compare_against_is_not_an_echo():
    assert _predict.is_restatement("alpha beta gamma delta", _ctx()) is False


# ── both sides of the turn are compared ──────────────────────────────────

def test_a_rewording_of_the_request_is_an_echo():
    assert _predict.is_restatement(
        _ACTION, _ctx(message="alpha beta gamma delta zeta")) is True


def test_a_rewording_of_what_was_done_is_an_echo_too():
    """"Read the file I just read for you" is as useless as repeating the
    question, and arrives by the same route."""
    assert _predict.is_restatement(
        _ACTION, _ctx(did="alpha beta gamma delta zeta")) is True


@pytest.mark.parametrize("ctx", [None, {}, {"message": None, "did": None},
                                 {"message": 17}, {"did": ["a", "b"]}])
def test_a_malformed_context_never_raises(ctx):
    """This runs at the end of every turn. It may be wrong; it may not throw."""
    assert _predict.is_restatement("alpha beta gamma delta", ctx) is False


# ── end to end: the echo never reaches the user ──────────────────────────

def test_an_echo_is_dropped_before_the_confidence_floor(monkeypatch):
    """Dropped BEFORE the floor on purpose. This failure arrives at the top of
    the confidence range by construction: the model is right about what was
    wanted and wrong only about it still being wanted, so a floor can never
    catch it."""
    monkeypatch.setenv("AIFORGE_PREDICT_MIN_CONFIDENCE", "0.1")
    _reply(monkeypatch, '{"action":"alpha beta gamma delta epsilon",'
                        '"tool":"read_file","args":{"path":"x"},'
                        '"confidence":1.0,"rationale":"x"}')
    assert next_step.predict(_ctx(message="alpha beta gamma delta zeta")) is None


def test_a_non_echo_still_comes_through(monkeypatch):
    """The guard must not be the reason the feature goes quiet."""
    _reply(monkeypatch, '{"action":"alpha beta gamma delta epsilon",'
                        '"tool":"read_file","args":{"path":"x"},'
                        '"confidence":0.9,"rationale":"x"}')
    p = next_step.predict(_ctx(message="wholly unrelated question about pears"))
    assert p is not None
    assert p.verdict == next_step.ACT


# ── the defence that does not depend on catching the wording ─────────────

@pytest.mark.parametrize("confidence", [0.75, 0.9, 0.95, 0.99, 1.0])
def test_a_toolless_prediction_never_acts_at_any_confidence(monkeypatch,
                                                            confidence):
    """The property that makes the failure impossible rather than unlikely: a
    prediction naming no tool cannot auto-send itself, however sure the model
    is and however novel its wording."""
    _reply(monkeypatch, '{"action":"something quite unlike the question",'
                        '"tool":"","args":{},'
                        f'"confidence":{confidence},"rationale":"x"}}')
    p = next_step.predict(_ctx(message="pears, and the price of them"))
    assert p is not None
    assert p.verdict == next_step.OFFER


@pytest.mark.parametrize("tool", ["", "   ", None])
def test_a_missing_tool_is_the_most_unknown_case(tool):
    assert _risk.tier(tool, {}) == 3


def test_a_named_read_only_tool_still_acts():
    """Guarding against over-correction: the fix must not have turned the
    feature off."""
    assert _risk.verdict("read_file", {"path": "x"}, confidence=0.9,
                         clean_tree=False) == _risk.ACT


# ── the prompt ───────────────────────────────────────────────────────────

def test_the_model_is_given_a_way_to_say_nothing_is_next():
    """It invented one every turn when it had none."""
    assert '"confidence":0.0' in _predict._SYS
    assert "restate" in _predict._SYS.lower()


def test_the_prompt_still_asks_for_a_tool_name():
    assert "names the tool" in _predict._SYS
