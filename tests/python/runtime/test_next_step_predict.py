"""The one capped call, its parsing, and its floor.

Everything here FAILS OPEN. This runs at the end of every turn, and a feature
that improves a good turn must never be able to break one.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import next_step
from aiforge_core.runtime.next_step import _predict


def _ctx(**kw):
    base = {"message": "connect to db.internal and check it is reachable",
            "did": "read `deploy/env.py`", "repo": "AIForgeCrew",
            "clean_tree": True}
    return {**base, **kw}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("AIFORGE_PREDICT_DISABLE", "AIFORGE_PREDICT_ACT",
              "AIFORGE_PREDICT_MIN_CONFIDENCE"):
        monkeypatch.delenv(k, raising=False)


def _reply(monkeypatch, raw):
    monkeypatch.setattr(_predict, "_llm", lambda *a, **k: raw)


def test_a_confident_safe_prediction_comes_back_as_act(monkeypatch):
    _reply(monkeypatch, '{"action":"check the connection","tool":"bash",'
                        '"args":{"cmd":"pg_isready -h db.internal"},'
                        '"confidence":0.9,"rationale":"a host was named"}')
    p = next_step.predict(_ctx())
    assert p is not None
    assert p.verdict == next_step.ACT
    assert p.action == "check the connection"
    assert p.rationale == "a host was named"


def test_an_irreversible_prediction_comes_back_as_offer(monkeypatch):
    """The model is not trusted to decide this — _risk is."""
    _reply(monkeypatch, '{"action":"push the fix","tool":"bash",'
                        '"args":{"cmd":"git push origin main"},'
                        '"confidence":0.99,"rationale":"the fix is committed"}')
    assert next_step.predict(_ctx()).verdict == next_step.OFFER


def test_below_the_floor_nothing_is_emitted_at_all(monkeypatch):
    """Not even an offer: a guess the model itself doubts is noise, and noise
    teaches the user to ignore the chip that matters."""
    _reply(monkeypatch, '{"action":"maybe restart it","tool":"","args":{},'
                        '"confidence":0.3,"rationale":"unsure"}')
    assert next_step.predict(_ctx()) is None


@pytest.mark.parametrize("raw", [
    "", "not json at all", "{", '{"action":""}', '{"nope": 1}', None,
    '{"action":"x","confidence":"very"}',     # unusable confidence
    '{"action":"x"}',                          # no confidence at all
])
def test_a_useless_reply_fails_open(monkeypatch, raw):
    _reply(monkeypatch, raw)
    assert next_step.predict(_ctx()) is None


def test_an_llm_error_fails_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("model is down")

    monkeypatch.setattr(_predict, "_llm", _boom)
    assert next_step.predict(_ctx()) is None


def test_the_kill_switch_makes_no_call_at_all(monkeypatch):
    calls = []
    monkeypatch.setattr(_predict, "_llm", lambda *a, **k: calls.append(1) or "{}")
    monkeypatch.setenv("AIFORGE_PREDICT_DISABLE", "1")

    assert next_step.predict(_ctx()) is None
    assert calls == [], "the kill switch must cost nothing, not just emit nothing"


def test_prose_around_the_json_is_tolerated(monkeypatch):
    _reply(monkeypatch, 'Sure! {"action":"check it","tool":"","args":{},'
                        '"confidence":0.9,"rationale":"x"} hope that helps')
    assert next_step.predict(_ctx()).action == "check it"


def test_every_prediction_has_its_own_id(monkeypatch):
    _reply(monkeypatch, '{"action":"check it","tool":"","args":{},'
                        '"confidence":0.9,"rationale":"x"}')
    a, b = next_step.predict(_ctx()), next_step.predict(_ctx())
    assert a.id
    assert b.id
    assert a.id != b.id


def test_the_event_never_carries_argument_values(monkeypatch):
    """Values are the half that may hold a credential; the sentence is what the
    UI shows."""
    _reply(monkeypatch, '{"action":"connect","tool":"bash",'
                        '"args":{"cmd":"psql postgres://u:p4ssw0rd@db/x"},'
                        '"confidence":0.9,"rationale":"x"}')
    ev = next_step.predict(_ctx()).as_event()
    assert "args" not in ev
    assert "p4ssw0rd" not in str(ev)


def test_accepted_history_is_offered_to_the_model(monkeypatch):
    """The learning loop, seen from the prompt side."""
    seen = {}

    def _capture(role, msgs, **k):
        seen["prompt"] = msgs
        return ('{"action":"x","tool":"","args":{},"confidence":0.9,'
                '"rationale":"y"}')

    monkeypatch.setattr(_predict, "_llm", _capture)
    next_step.outcome_row({"id": "p-1", "trigger": "gave a host and a user",
                           "action": "verify the connection works",
                           "tool": "bash", "repo": "AIForgeCrew"},
                          accepted=True)

    next_step.predict(_ctx())

    assert "verify the connection works" in str(seen["prompt"])


def test_another_repos_history_is_not_offered(monkeypatch):
    seen = {}
    monkeypatch.setattr(_predict, "_llm",
                        lambda role, msgs, **k: seen.update(p=msgs) or
                        '{"action":"x","tool":"","args":{},"confidence":0.9,'
                        '"rationale":"y"}')
    next_step.outcome_row({"id": "p-1", "trigger": "t",
                           "action": "something from elsewhere",
                           "tool": "", "repo": "OtherRepo"}, accepted=True)

    next_step.predict(_ctx())

    assert "something from elsewhere" not in str(seen["p"])
