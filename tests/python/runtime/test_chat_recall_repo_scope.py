"""F2: chat recall must scope `repo` the SAME way the chat WRITE path does.

The write path (chat_learner/chat_summary via api.py) files chat facts under
``rule_capture.repo_key(cwd)``. If recall calls unified_query.query WITHOUT
that repo, sqlite_memory.recall filters against the wrong repo and the chat's
own facts become invisible. These tests pin the derivation into both recall
entry points: ``_memory_recall`` (session-start proactive recall) and the
``memory_lookup`` tool (``_t_memory_lookup``).
"""
import os
import subprocess

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


# ── M3: subdir resolves the SAME repo as the root (git-toplevel) ─────────────

def _git_init(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_subdir_recalls_same_repo_as_root(monkeypatch, tmp_path):
    """A chat cwd deep inside a repo must resolve the same repo key as its
    git-toplevel basename (not the raw subdir basename)."""
    root = tmp_path / "cool-repo"
    sub = root / "services" / "api"
    sub.mkdir(parents=True)
    _git_init(str(root))
    chat_agent._GIT_TOPLEVEL_CACHE.clear()

    from_root = chat_agent._chat_repo_key(str(root))
    from_sub = chat_agent._chat_repo_key(str(sub))
    assert from_root == "cool-repo"
    assert from_sub == "cool-repo"  # subdir basename would have been "api"


def test_repo_key_env_fallback_before_literal(monkeypatch):
    """When cwd yields no key, AIFORGE_AFM_REPO is used before "repo"."""
    monkeypatch.setenv("AIFORGE_AFM_REPO", "envrepo")
    chat_agent._GIT_TOPLEVEL_CACHE.clear()
    # repo_key("") is None and git-toplevel(None) is None → env wins.
    assert chat_agent._chat_repo_key("") == "envrepo"
    assert chat_agent._chat_repo_key(None) == "envrepo"
