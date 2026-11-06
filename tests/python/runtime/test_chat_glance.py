"""One line per chat in the list: where it stands, without opening it."""
from __future__ import annotations

from aiforge_core.runtime import chat_glance as G


def _items(*states):
    return [{"type": "subtasks", "items": [{"slug": f"p{i}", "status": s} for i, s in enumerate(states)]}]


def test_the_outcome_of_a_turn_in_one_line():
    files = [{"type": "changes", "files": [{"path": "a.py"}, {"path": "b.py"}]}]
    assert G.summarize(_items("done", "done") + files, "All done.", False, False) == \
        {"kind": "done", "text": "Done · 2/2 tasks · 2 files changed"}
    assert G.summarize([], "Paris.", False, False) == {"kind": "done", "text": "Answered"}
    assert G.summarize(_items("done", "running", "pending"), "(stopped)", False, True) == \
        {"kind": "stopped", "text": "Stopped at task 2 of 3"}
    assert G.summarize(_items("done"), "Which one?", True, False)["kind"] == "waiting"
    assert G.summarize(_items("done", "left", "done") + files, "x", False, False) == \
        {"kind": "partial", "text": "Done · 1 left of 3 tasks · 2 files changed"}
    assert G.summarize([], "⚠ model call failed: timeout", False, False)["kind"] == "failed"
    assert G.summarize([{"type": "error", "text": "boom"}], "", False, False)["kind"] == "failed"
    assert G.summarize([], "", False, False) is None
    assert G.summarize([], "Error handling in X works by retrying.", False, False)["kind"] == "done"
    assert G.summarize([], "(no final summary: …)", False, False)["kind"] == "partial"
    assert G.summarize([{"type": "plan_ready"}] + _items("planned", "planned"), "plan", False, False) \
        == {"kind": "waiting", "text": "Plan ready — approve to run it"}
    assert G.summarize(_items("failed", "running", "pending") + [{"type": "stopped"}], "x", False, False) \
        == {"kind": "stopped", "text": "Stopped at task 2 of 3"}
    ev = [{"type": "turn_evidence", "evidence": {"steps": [
        {"kind": "edit", "ok": True, "what": "/r/a.py"}, {"kind": "edit", "ok": True, "what": "/r/a.py"},
        {"kind": "edit", "ok": False, "what": "/r/b.py"}]}}]
    assert G.summarize(ev + files, "ok", False, False)["text"] == "Done · 1 file changed"


def _no_store(monkeypatch, msgs=2):
    from aiforge_core.runtime import chat_private, chat_store
    monkeypatch.setattr(chat_private, "is_temporary", lambda sid: sid == 9)
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: [{}] * msgs)


def test_lines_prefer_live_then_background_then_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _no_store(monkeypatch)
    G.record(1, _items("done"), "ok", False, False)
    G.record(2, [], "(stopped)", False, True)
    G.record(9, [], "ok", False, False)                     # a temporary chat: never kept
    rows = [{"id": i, "running": i == 3, "message_count": 2} for i in (1, 2, 3, 4, 9)]
    out = G.lines(rows, background={2})
    assert out[1]["kind"] == "done" and out[2]["kind"] == "background"
    assert out[3]["kind"] == "running" and 4 not in out and 9 not in out


def test_a_line_older_than_the_chats_last_message_is_not_shown(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _no_store(monkeypatch)
    G.record(1, [], "ok", False, False)
    assert G.lines([{"id": 1, "running": False, "message_count": 3}], set()) == {}
    assert G.lines([{"id": 1, "running": False, "message_count": 2}], set())  # a rename: still shown
