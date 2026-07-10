"""Session execution ledger — no-repeat context + working-workflow capture."""
from __future__ import annotations

from aiforge_core.runtime import session_ledger as sl


def _msgs(monkeypatch, messages):
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: messages)


def test_ledger_dedupes_and_tracks_outcome(monkeypatch):
    _msgs(monkeypatch, [
        {"role": "assistant", "steps": [
            {"type": "tool", "name": "run_command", "args": {"cmd": "pytest"},
             "result": {"ok": False}},
            {"type": "tool", "name": "file_write", "args": {"path": "a.py"},
             "result": {"ok": True}},
        ]},
        {"role": "assistant", "steps": [
            {"type": "tool", "name": "run_command", "args": {"cmd": "pytest"},
             "result": {"ok": True}},          # retry succeeded → outcome flips
            {"type": "tool", "name": "grep", "args": {"q": "x"},
             "result": {"ok": True}},          # read-only → not in ledger
        ]},
    ])
    items = sl.ledger_items(1)
    keys = [i["key"] for i in items]
    assert keys == ["cmd:pytest", "write:a.py"]      # deduped, order preserved
    assert items[0]["outcome"] is True               # latest outcome wins
    blk = sl.ledger_block(1)
    assert "ALREADY EXECUTED" in blk and "✅ ran `pytest`" in blk
    assert "wrote `a.py`" in blk


def test_ledger_empty_when_no_tools(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "thought", "text": "hmm"}]}])
    assert sl.ledger_block(1) == ""


def test_capture_working_workflow(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "npm ci"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "npm test"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "flaky"},
         "result": {"ok": False}},             # failed → NOT in the workflow
    ]}])
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session",
                        lambda sid: {"title": "Set up CI"})
    seen = {}
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda name, description, body, **k: seen.update(
                            name=name, body=body, triggers=k.get("triggers")) or {"ok": True, "name": name})
    from aiforge_core.memory import md_store
    caps = []
    monkeypatch.setattr(md_store, "capture",
                        lambda kind, text, **k: caps.append((kind, k.get("tags"))) or {})
    r = sl.capture_working_workflow(7, repo="myrepo")
    assert r["ok"]
    assert seen["name"] == "session-set-up-ci"
    assert "`npm ci`" in seen["body"] and "`npm test`" in seen["body"]
    assert "flaky" not in seen["body"]              # failed step excluded
    assert caps and "repo:myrepo" in caps[0][1] and "workflow" in caps[0][1]


def test_capture_skips_too_few_working(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "ls"},
         "result": {"ok": True}}]}])
    assert sl.capture_working_workflow(1, repo="r")["skipped"] == "too_few_working_steps"
