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


def _stub_verify(monkeypatch, result):
    """Patch the LLM verification to return `result` (dict) or raise (→ None)."""
    from types import SimpleNamespace as NS

    def fake(role, messages, model, **k):
        if result is None:
            raise RuntimeError("no model")
        return NS(model_dump=lambda: result)
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", fake)


def test_capture_working_workflow_verified(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "npm ci"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "npm test"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "flaky"},
         "result": {"ok": False}},             # failed → NOT even considered
    ]}])
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"title": "Set up CI"})
    _stub_verify(monkeypatch, {"is_reusable": True, "name": "ci-setup",
                               "description": "install + test",
                               "steps": ["npm ci", "npm test"], "triggers": ["ci"]})
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
    assert seen["name"] == "session-ci-setup"       # LLM-refined name
    assert seen["triggers"] == ["ci"]
    assert "`npm ci`" in seen["body"] and "`npm test`" in seen["body"]
    assert "flaky" not in seen["body"]
    assert caps and "repo:myrepo" in caps[0][1] and "workflow" in caps[0][1]


def test_capture_skips_when_llm_says_not_reusable(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "ls"}, "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "cat x"}, "result": {"ok": True}},
    ]}])
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"title": "poking around"})
    _stub_verify(monkeypatch, {"is_reusable": False})
    from aiforge_core.runtime import workflows
    called = {"n": 0}
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"ok": True})
    r = sl.capture_working_workflow(9, repo="r")
    assert r.get("skipped") == "not_reusable"
    assert called["n"] == 0                          # nothing written


def test_capture_falls_back_when_no_model(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "make build"}, "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "make deploy"}, "result": {"ok": True}},
    ]}])
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"title": "Release"})
    _stub_verify(monkeypatch, None)                  # no model → raise → fallback
    seen = {}
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda name, description, body, **k: seen.update(body=body, name=name) or {"ok": True})
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "capture", lambda *a, **k: {})
    r = sl.capture_working_workflow(3, repo="r")
    assert r["ok"]
    assert "make build" in seen["body"] and "unverified" in seen["body"]


def test_capture_skips_too_few_working(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "ls"},
         "result": {"ok": True}}]}])
    assert sl.capture_working_workflow(1, repo="r")["skipped"] == "too_few_working_steps"
