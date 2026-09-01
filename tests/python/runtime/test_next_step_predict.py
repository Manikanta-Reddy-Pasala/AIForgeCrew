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


# ── the echo: a "next step" that is the request over again ───────────────
#
# The production failure this guards. Asked what comes next after a request it
# has already fully answered, the model rephrases that request back — it did so
# on every one of the first three live predictions, each at confidence 0.95,
# each with no tool. The chip auto-sent it and the user's own question ran a
# second time. The floor cannot catch this: the model is not wrong about what
# was wanted, only about it still being wanted.


def test_a_restated_request_is_not_a_next_step(monkeypatch):
    """Verbatim from ~/.aiforge/next_step_history.json on the NUC."""
    _reply(monkeypatch, '{"action":"Explain what the run lock in chat_pipeline '
                        'protects.","tool":"","args":{},"confidence":0.95,'
                        '"rationale":"they asked about the run lock"}')
    ctx = _ctx(message="In one short sentence: what does the run lock in "
                       "chat_pipeline protect?",
               did="explained the run lock")
    assert next_step.predict(ctx) is None


def test_a_restatement_of_what_was_done_is_dropped_too(monkeypatch):
    _reply(monkeypatch, '{"action":"read deploy/env.py and report the host",'
                        '"tool":"read_file","args":{"path":"deploy/env.py"},'
                        '"confidence":0.95,"rationale":"x"}')
    ctx = _ctx(message="what is in the deploy config?",
               did="read deploy/env.py and reported the host")
    assert next_step.predict(ctx) is None


def test_high_confidence_does_not_rescue_an_echo(monkeypatch):
    """Dropped BEFORE the floor, because this failure arrives at the top of the
    confidence range by construction."""
    _reply(monkeypatch, '{"action":"check the status of the cn-network-manager '
                        'service","tool":"","args":{},"confidence":1.0,'
                        '"rationale":"x"}')
    ctx = _ctx(message="give me a shell command to check the status of the "
                       "cn-network-manager service", did="gave the command")
    assert next_step.predict(ctx) is None


def test_a_genuine_follow_up_survives(monkeypatch):
    """The guard must not eat real suggestions. This one shares most of its
    subject with the request and is still a different action."""
    _reply(monkeypatch, '{"action":"run the tests for chat_pipeline",'
                        '"tool":"bash","args":{"cmd":"pytest tests/python"},'
                        '"confidence":0.9,"rationale":"the fix needs proving"}')
    ctx = _ctx(message="fix the run lock in chat_pipeline",
               did="edited chat_pipeline.py")
    p = next_step.predict(ctx)
    assert p is not None
    assert p.action == "run the tests for chat_pipeline"


def test_a_terse_action_is_never_judged_an_echo(monkeypatch):
    """Too few content words to compare. Guessing would silently drop good
    short suggestions."""
    _reply(monkeypatch, '{"action":"check it","tool":"read_file",'
                        '"args":{"path":"x"},"confidence":0.9,"rationale":"x"}')
    assert next_step.predict(_ctx(message="check it")) is not None


def test_the_prompt_allows_no_next_step(monkeypatch):
    """A model with no way to say "nothing is next" invents one."""
    seen = {}
    monkeypatch.setattr(_predict, "_llm",
                        lambda role, msgs, **k: seen.update(p=msgs) or
                        '{"action":"x","tool":"","args":{},"confidence":0.9,'
                        '"rationale":"y"}')
    next_step.predict(_ctx())
    prompt = str(seen["p"])
    assert '"confidence":0.0' in prompt
    assert "restate" in prompt
