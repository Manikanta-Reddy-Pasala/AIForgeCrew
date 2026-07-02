"""F2: chat recall must scope `repo` the SAME way the chat WRITE path does.

The write path (chat_learner/chat_summary via api.py) files chat facts under
``rule_capture.repo_key(cwd)``. If recall calls unified_query.query WITHOUT
that repo, sqlite_memory.recall filters against the wrong repo and the chat's
own facts become invisible. These tests pin the derivation into both recall
entry points: ``_memory_recall`` (session-start proactive recall) and the
``memory_lookup`` tool (``_t_memory_lookup``).
"""
import os

from aiforge_core.runtime import chat_agent, rule_capture


def _patch_query_capture(monkeypatch):
    captured = {}

    def _fake_query(text, **kwargs):
        captured["text"] = text
        captured["kwargs"] = kwargs
        return {"hits": []}

    monkeypatch.setattr(
        "aiforge_core.memory.unified_query.query", _fake_query,
    )
    return captured


def test_memory_recall_passes_repo_key(monkeypatch, tmp_path):
    cwd = str(tmp_path / "myrepo")
    os.makedirs(cwd, exist_ok=True)
    captured = _patch_query_capture(monkeypatch)

    chat_agent._memory_recall(cwd, "how did we wire the sync loop?")

    expected = rule_capture.repo_key(cwd)
    assert expected == "myrepo"
    assert captured["kwargs"].get("repo") == expected


def test_memory_lookup_tool_passes_repo_key(monkeypatch, tmp_path):
    cwd = str(tmp_path / "otherrepo")
    os.makedirs(cwd, exist_ok=True)
    captured = _patch_query_capture(monkeypatch)

    chat_agent._t_memory_lookup({"query": "prior decision on X"}, cwd)

    expected = rule_capture.repo_key(cwd)
    assert expected == "otherrepo"
    assert captured["kwargs"].get("repo") == expected
