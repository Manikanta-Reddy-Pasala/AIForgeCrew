"""persist_turn must carry an 'awaiting' turn's flag into the stored message so
the reload reconcile (loadSession + clear liveTurn) doesn't lose the reply
affordance — the frontend's msgAwaiting() keys off a persisted step."""
from __future__ import annotations

from aiforge_core.runtime import chat_persist


class _FakeStore:
    def __init__(self):
        self.saved = []

    # chat_store.add_message grew mode/duration_s kwargs (persist_turn now
    # records which chat mode produced the turn and how long it took), so the
    # fake must accept them or persist_turn dies with a TypeError.
    def add_message(self, session_id, role, content, steps=None,
                    mode="simple", duration_s=None, **_kw):
        self.saved.append({"role": role, "content": content, "steps": steps or [],
                           "mode": mode, "duration_s": duration_s})
        return len(self.saved)


def _patch(monkeypatch):
    store = _FakeStore()
    import aiforge_core.runtime.chat_store as cs
    monkeypatch.setattr(cs, "add_message", store.add_message)
    # persist_turn imports chat_store lazily inside the function.
    return store


def test_awaiting_turn_persists_marker_step(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_AUTO_MEMORY", "0")
    store = _patch(monkeypatch)
    chat_persist.persist_turn(
        session_id=1, cwd=str(tmp_path), prompt="do x",
        final_text="I keep trying the same step — please clarify.",
        steps=[{"type": "thought", "text": "..."}],
        team=False, cancelled=False, awaiting=True)
    assert store.saved, "message should be persisted"
    steps = store.saved[-1]["steps"]
    # msgAwaiting() recognises a step with type 'awaiting' OR awaiting_input=True.
    assert any(s.get("type") == "awaiting" or s.get("awaiting_input") is True
               for s in steps), "awaiting marker step must be persisted"


def test_non_awaiting_turn_has_no_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_AUTO_MEMORY", "0")
    store = _patch(monkeypatch)
    chat_persist.persist_turn(
        session_id=1, cwd=str(tmp_path), prompt="do x",
        final_text="Done — wrote foo.py.",
        steps=[{"type": "tool", "name": "write"}],
        team=False, cancelled=False, awaiting=False)
    steps = store.saved[-1]["steps"]
    assert not any(s.get("type") == "awaiting" for s in steps)
