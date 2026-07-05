"""Unit tests for the team-mode steering before_model callback in
isolation (the real-ADK-graph mechanism itself is covered by
tests/python/runtime/test_pipeline_steer_callback.py)."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_interject as ci
from aiforge_core.runtime import chat_steer_callback as csc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AIFORGE_CURRENT_SESSION", raising=False)
    ci.clear(7)
    yield
    ci.clear(7)


class _FakeRequest:
    def __init__(self, contents):
        self.contents = contents


def test_noop_when_no_session_env():
    cb = csc.make_steer_before_model_callback("doer")
    req = _FakeRequest(["seed"])
    assert cb(llm_request=req) is None
    assert req.contents == ["seed"]   # untouched


def test_noop_when_no_llm_request(monkeypatch):
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "7")
    cb = csc.make_steer_before_model_callback("doer")
    assert cb(llm_request=None) is None


def test_noop_when_nothing_pending(monkeypatch):
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "7")
    cb = csc.make_steer_before_model_callback("doer")
    req = _FakeRequest(["seed"])
    assert cb(llm_request=req) is None
    assert req.contents == ["seed"]


def test_folds_drained_steer_and_marks_applied(monkeypatch):
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "7")
    ci.push(7, "focus on error handling")
    cb = csc.make_steer_before_model_callback("doer")
    req = _FakeRequest(["seed"])
    assert cb(llm_request=req) is None
    assert len(req.contents) == 2
    added = req.contents[-1]
    assert added.role == "user"
    text = "".join(getattr(p, "text", "") or "" for p in added.parts)
    assert "focus on error handling" in text
    # Drained — nothing left pending, and the callback recorded what it
    # applied for chat_pipeline's event loop to acknowledge.
    assert ci.pending(7) is False
    assert ci.pop_applied(7) == ["focus on error handling"]


def test_bad_session_env_is_safe(monkeypatch):
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "not-an-int")
    cb = csc.make_steer_before_model_callback("doer")
    req = _FakeRequest(["seed"])
    assert cb(llm_request=req) is None
    assert req.contents == ["seed"]


def test_never_raises_on_broken_request(monkeypatch):
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "7")
    ci.push(7, "x")
    cb = csc.make_steer_before_model_callback("doer")

    class _Broken:
        @property
        def contents(self):
            raise RuntimeError("boom")

    assert cb(llm_request=_Broken()) is None
