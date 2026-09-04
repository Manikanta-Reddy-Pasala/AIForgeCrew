"""The same suggestion must not arrive in chat after chat.

The report was two complaints in one sentence — "the next step suggestion is
not good, and the same suggestions come in different chats". They have one
cause between them: every prediction started from an empty room. The history
file recorded what had been offered and what the user said to it, and the
prediction path read exactly one slice of that (accepted rows, as examples). A
dismissal fed back into nothing; an offer nobody had answered fed back into
nothing. So the next turn — in the next session, on the next day — proposed it
again at full confidence.

These pin the three things that changed: a repeat is suppressed ACROSS
sessions, a dismissal buys a longer silence and is shown to the model as a
do-not-repeat list, and a sentence that cannot name a tool has to be a strong
guess to be worth an interruption.
"""
from __future__ import annotations

import time

import pytest

from aiforge_core.runtime import next_step
from aiforge_core.runtime.next_step import _predict as _np
from aiforge_core.runtime.next_step import _repeat, _store


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in ("AIFORGE_PREDICT_REPEAT_H", "AIFORGE_PREDICT_DISMISS_DAYS",
                "AIFORGE_PREDICT_TOOLLESS_MIN", "AIFORGE_PREDICT_DISABLE",
                "AIFORGE_PREDICT_MIN_CONFIDENCE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _reply(monkeypatch, action: str, *, tool: str = "run_tests",
           confidence: float = 0.95):
    payload = (f'{{"action":"{action}","tool":"{tool}","args":{{}},'
               f'"confidence":{confidence},"rationale":"x"}}')
    monkeypatch.setattr(_np, "_llm", lambda *a, **k: payload)


def _ctx(message="add a test for the parser", did="added one", repo="/repo"):
    return {"message": message, "did": did, "repo": repo, "clean_tree": True}


# ── a repeat, across sessions ───────────────────────────────────────────────

def test_the_same_suggestion_is_not_made_twice(monkeypatch):
    """There is one history file for the whole install, so "a different chat"
    is not a different room — which is exactly what the user saw."""
    _reply(monkeypatch, "run the test suite for the parser")
    first = next_step.predict(_ctx())
    assert first is not None

    second = next_step.predict(_ctx(message="and now the lexer"))
    assert second is None


def test_a_rewording_counts_as_the_same_suggestion(monkeypatch):
    """The model rewords itself every turn; string equality would have caught
    none of the repeats that prompted this."""
    _reply(monkeypatch, "run the test suite for the parser")
    assert next_step.predict(_ctx()) is not None
    _reply(monkeypatch, "run the parser test suite")
    assert next_step.predict(_ctx()) is None


def test_a_genuinely_different_suggestion_still_comes_through(monkeypatch):
    """Guarding against over-correction: a feature that suppresses everything
    is not an improvement on one that repeats itself."""
    _reply(monkeypatch, "run the test suite for the parser")
    assert next_step.predict(_ctx()) is not None
    _reply(monkeypatch, "update the changelog with the new option")
    assert next_step.predict(_ctx()) is not None


def test_the_same_suggestion_in_another_repo_is_not_a_repeat(monkeypatch):
    """What a user was offered in one codebase says nothing about another —
    the same rule the accepted-examples list already follows."""
    _reply(monkeypatch, "run the test suite for the parser")
    assert next_step.predict(_ctx(repo="/a")) is not None
    assert next_step.predict(_ctx(repo="/b")) is not None


def test_the_window_expires(monkeypatch):
    """A suggestion that was wrong in March may be right in June; a store that
    never forgets becomes a list of things the product may never say again."""
    _reply(monkeypatch, "run the test suite for the parser")
    assert next_step.predict(_ctx()) is not None
    monkeypatch.setenv("AIFORGE_PREDICT_REPEAT_H", "0.0001")   # ~0.36s
    monkeypatch.setenv("AIFORGE_PREDICT_DISMISS_DAYS", "0")
    time.sleep(0.5)
    assert next_step.predict(_ctx()) is not None


def test_the_windows_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_PREDICT_REPEAT_H", "0")
    monkeypatch.setenv("AIFORGE_PREDICT_DISMISS_DAYS", "0")
    _reply(monkeypatch, "run the test suite for the parser")
    assert next_step.predict(_ctx()) is not None
    assert next_step.predict(_ctx()) is not None


# ── a dismissal is louder, and lasts longer ─────────────────────────────────

def test_a_dismissed_suggestion_stays_gone_past_the_repeat_window(monkeypatch):
    _reply(monkeypatch, "run the test suite for the parser")
    p = next_step.predict(_ctx())
    next_step.outcome(p.id, accepted=False)

    monkeypatch.setenv("AIFORGE_PREDICT_REPEAT_H", "0")   # only the dismissal holds
    assert next_step.predict(_ctx()) is None


def test_dismissals_are_shown_to_the_model_as_do_not_repeat(monkeypatch):
    """The clearest signal in the store fed back into nothing. Now it is in the
    prompt, so the model stops generating the sentence rather than the gate
    quietly dropping it every time."""
    _reply(monkeypatch, "run the test suite for the parser")
    p = next_step.predict(_ctx())
    next_step.outcome(p.id, accepted=False)

    seen: dict = {}

    def _capture(role, messages, **kw):
        seen["prompt"] = messages[-1]["content"]
        return '{"action":"open the changelog","tool":"read_file",' \
               '"args":{},"confidence":0.95,"rationale":"x"}'

    monkeypatch.setattr(_np, "_llm", _capture)
    next_step.predict(_ctx())
    assert "DISMISSED" in seen["prompt"]
    assert "run the test suite for the parser" in seen["prompt"]


def test_an_accepted_suggestion_is_still_offered_as_an_example(monkeypatch):
    """Accepted rows were the one thing already fed back; the change must not
    have cost that."""
    _reply(monkeypatch, "run the test suite for the parser")
    p = next_step.predict(_ctx())
    next_step.outcome(p.id, accepted=True)

    seen: dict = {}

    def _capture(role, messages, **kw):
        seen["prompt"] = messages[-1]["content"]
        return '{"action":"open the changelog","tool":"read_file",' \
               '"args":{},"confidence":0.95,"rationale":"x"}'

    monkeypatch.setattr(_np, "_llm", _capture)
    next_step.predict(_ctx())
    assert "previously accepted" in seen["prompt"]


# ── advice is not a next step ───────────────────────────────────────────────

def test_a_weak_toolless_prediction_is_dropped(monkeypatch):
    """"Consider reviewing the changes" at 0.8 is what "the suggestions are not
    good" mostly meant: plausible, unactionable, and impossible to judge by
    whether acting on it helped."""
    _reply(monkeypatch, "consider reviewing the changes", tool="",
           confidence=0.8)
    assert next_step.predict(_ctx()) is None


def test_a_strong_toolless_prediction_is_still_offered(monkeypatch):
    _reply(monkeypatch, "ask the user which environment to target", tool="",
           confidence=0.95)
    p = next_step.predict(_ctx())
    assert p is not None
    assert p.verdict == next_step.OFFER


def test_a_tool_named_prediction_keeps_the_ordinary_floor(monkeypatch):
    _reply(monkeypatch, "run the parser tests", tool="run_tests",
           confidence=0.8)
    assert next_step.predict(_ctx()) is not None


# ── the store helpers, directly ─────────────────────────────────────────────

def test_recent_for_matches_by_meaning_not_by_string():
    _store.append({"id": "a", "repo": "/repo", "trigger": "t",
                   "action": "run the test suite", "tool": "run_tests",
                   "verdict": "ACT", "confidence": 0.9}, accepted=True)
    assert _store.recent_for("/repo", "run the tests", within_s=3600)
    assert _store.recent_for("/repo", "deploy to production",
                             within_s=3600) is None


def test_suppression_fails_open(monkeypatch):
    """This runs at the end of every turn; an unreadable history must cost a
    duplicate suggestion, never a broken turn."""
    monkeypatch.setattr(_store, "recent_for",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert _repeat.suppressed("anything at all", {"repo": "/repo"}) is False
