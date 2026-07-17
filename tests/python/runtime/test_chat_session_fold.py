"""Bug3 — compaction on chat switch/close: fold helpers are best-effort and
respect the enable switch."""
from __future__ import annotations

from aiforge_core.runtime import chat_session_fold


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_ON_SWITCH", "0")
    assert chat_session_fold._enabled() is False
    # fold_async must not spawn/raise when disabled
    chat_session_fold.fold_async(5)
    chat_session_fold.fold_previous_async(5)


def test_fold_sync_soft_fails(monkeypatch):
    # compact_session blowing up must never propagate out of fold_sync.
    import aiforge_core.runtime.chat_okr as okr

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(okr, "compact_session", _boom)
    res = chat_session_fold.fold_sync(1)
    assert res.get("ok") is False and "db down" in res.get("error", "")


def test_fold_delegates_to_compact_session(monkeypatch):
    seen = {}
    import aiforge_core.runtime.chat_okr as okr
    monkeypatch.setattr(chat_session_fold, "_repo_for", lambda sid: "myrepo")
    monkeypatch.setattr(okr, "compact_session",
                        lambda sid, repo=None: seen.update(sid=sid, repo=repo)
                        or {"ok": True, "captured": 3})
    res = chat_session_fold.fold_sync(7)
    assert seen == {"sid": 7, "repo": "myrepo"}
    assert res["captured"] == 3
