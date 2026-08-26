"""What is remembered, and what must never be.

The store is the learning loop's whole mechanism, and it is also the second
place a credential could come to live. It must not become that.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import next_step
from aiforge_core.runtime.next_step import _store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))


def _p(pid="p-1", action="verify the connection works", tool="bash"):
    return next_step.Prediction(id=pid, action=action, tool=tool,
                                args={"cmd": "pg_isready -h db.internal"},
                                confidence=0.9, rationale="a host was named",
                                verdict=next_step.ACT)


# ── the learning loop ────────────────────────────────────────────────────

def test_an_accepted_prediction_becomes_an_example():
    _store.remember(_p(), {"repo": "R", "message": "connect to the database"})
    _store.record_outcome("p-1", True)

    assert [r["action"] for r in _store.accepted("R", limit=5)] == [
        "verify the connection works"]


def test_a_dismissed_prediction_is_kept_but_never_used_as_an_example():
    """A feature that learns only from its wins drifts, and a dismissal is the
    clearer signal of the two."""
    _store.remember(_p(), {"repo": "R", "message": "connect to the database"})
    _store.record_outcome("p-1", False)

    assert _store.accepted("R", limit=5) == []
    assert _store.history(10)[0]["accepted"] is False


def test_a_pending_prediction_is_not_an_example_either():
    _store.remember(_p(), {"repo": "R", "message": "connect to the database"})
    assert _store.accepted("R", limit=5) == []


def test_examples_do_not_cross_repos():
    """What a user accepts in one codebase says little about another, and mixing
    them makes every prediction blander."""
    _store.remember(_p("p-1"), {"repo": "A", "message": "connect to the database"})
    _store.record_outcome("p-1", True)
    assert _store.accepted("B", limit=5) == []


def test_only_the_most_recent_examples_are_offered():
    for i in range(10):
        _store.remember(_p(pid=f"p-{i}", action=f"action {i}"),
                        {"repo": "R", "message": "connect to the database"})
        _store.record_outcome(f"p-{i}", True)
    rows = _store.accepted("R", limit=3)
    assert [r["action"] for r in rows] == ["action 7", "action 8", "action 9"]


# ── what must never be stored ────────────────────────────────────────────

def test_a_prediction_carrying_a_credential_is_not_stored():
    """redact is the one place in the product that judges secrets. A row it
    refuses is DROPPED, not stored scrubbed."""
    _store.remember(_p(action="use AKIAIOSFODNN7EXAMPLE to connect"),
                    {"repo": "R", "message": "here is the deploy key"})
    assert _store.history(10) == []


def test_a_credential_in_the_users_message_is_not_stored_either():
    _store.remember(_p(), {"repo": "R",
                           "message": "connect with password=hunter2ThatIsReal"})
    assert _store.history(10) == []


def test_argument_values_are_never_written():
    _store.remember(_p(), {"repo": "R", "message": "connect to the database"})
    assert "pg_isready" not in str(_store.history(10))


def test_an_ordinary_short_trigger_is_still_stored():
    """redact's noise rules judge whether a NOTE is worth replicating to a
    fleet. A prediction trigger is neither a note nor replicated, so a short one
    must not be thrown away."""
    _store.remember(_p(), {"repo": "R", "message": "do it"})
    assert len(_store.history(10)) == 1


# ── bounds and resilience ────────────────────────────────────────────────

def test_the_store_is_bounded():
    for i in range(_store.MAX_ROWS + 25):
        _store.remember(_p(pid=f"p-{i}"), {"repo": "R", "message": "connect now"})
    assert len(_store.history(10_000)) == _store.MAX_ROWS


def test_recording_an_outcome_for_an_unknown_id_is_not_an_error():
    """A chip in a browser tab left open across a restart is not an error the
    user can do anything about."""
    _store.record_outcome("p-nope", True)


def test_an_unreadable_store_does_not_break_anything(monkeypatch):
    def _boom():
        raise OSError("nope")

    monkeypatch.setattr(_store, "_path", _boom)
    assert _store.accepted("R", limit=5) == []
    assert _store.history(5) == []
    _store.remember(_p(), {"repo": "R", "message": "connect now"})


def test_a_filter_that_explodes_refuses_rather_than_stores(monkeypatch):
    from aiforge_core.memory.sync import redact

    monkeypatch.setattr(redact, "review",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    _store.remember(_p(), {"repo": "R", "message": "connect now"})
    assert _store.history(10) == []
