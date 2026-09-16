"""A mid-run instruction must never vanish without a word.

drain() removes the pending steers, so they exist only in that loop. A raise
partway through abandoned the rest, and a failed SPEC.md write still answered
"folded into the plan" — the user watched for a change that was never recorded.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.parallel_subtasks import _stream


@pytest.fixture
def subs():
    return [{"slug": "sub-1", "goal": "build the queue", "status": "pending"}]


def _drain(monkeypatch, session_id, texts, subs, cwd, apply_side_effect=None):
    monkeypatch.setattr(_stream.chat_interject if hasattr(_stream, "chat_interject")
                        else _stream, "_noop", None, raising=False)

    class _FakeInterject:
        @staticmethod
        def pending(_sid):
            return True

        @staticmethod
        def drain(_sid):
            return list(texts)

    class _FakeSteer:
        @staticmethod
        def steer_event(text):
            return {"type": "steer", "text": text}

    import sys
    monkeypatch.setitem(sys.modules, "aiforge_core.runtime.chat_interject",
                        _FakeInterject)
    monkeypatch.setitem(sys.modules, "aiforge_core.runtime.chat_steer", _FakeSteer)
    if apply_side_effect is not None:
        monkeypatch.setattr(_stream, "_apply_steer", apply_side_effect)
    return list(_stream._steering_drain(session_id, subs, str(cwd)))


def test_one_failing_steer_does_not_swallow_the_others(monkeypatch, subs, tmp_path):
    seen: list[str] = []

    def _apply(text, _subs, _cwd):
        seen.append(text)
        if text == "second":
            raise RuntimeError("router down")
        return f"applied {text}"

    events = _drain(monkeypatch, 7, ["first", "second", "third"], subs, tmp_path,
                    apply_side_effect=_apply)
    assert seen == ["first", "second", "third"]      # all three were attempted
    texts = " ".join(str(e.get("text", "")) for e in events)
    assert "could not apply your instruction" in texts   # and the failure shows
    assert "second" in texts
    assert "applied third" in texts


def test_a_failed_spec_write_is_reported_not_confirmed(tmp_path, monkeypatch):
    """The mandate still binds the reconcile, but the answer must not claim the
    spec was updated when the write failed."""
    monkeypatch.setattr(_stream, "_route_steering",
                        lambda *a, **k: {"target": "global", "note": "n"})
    missing = tmp_path / "gone"                      # no such directory
    out = _stream._apply_steer("also add retries", [], str(missing))
    assert "could NOT write it into SPEC.md" in out
    assert "binding on the final reconcile" in out


def test_a_good_steer_reaches_spec_md(tmp_path, monkeypatch):
    monkeypatch.setattr(_stream, "_route_steering",
                        lambda *a, **k: {"target": "global", "note": "n"})
    out = _stream._apply_steer("also add retries", [], str(tmp_path))
    assert "could NOT write" not in out
    spec = (tmp_path / "SPEC.md").read_text(encoding="utf-8")
    assert "**MUST:** also add retries" in spec


def test_routing_failure_still_records_the_requirement(tmp_path, monkeypatch):
    """If the model that classifies the steer is down, the instruction is still
    a requirement — it must not be dropped."""
    def _boom(*_a, **_k):
        raise RuntimeError("LLM endpoint unreachable")

    monkeypatch.setattr(_stream, "_route_steering", _boom)
    out = _stream._apply_steer("never touch prod", [], str(tmp_path))
    assert "could NOT write" not in out
    spec = (tmp_path / "SPEC.md").read_text(encoding="utf-8")
    assert "never touch prod" in spec
