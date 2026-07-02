"""Rescan fixes: chat write/read repo-key agree (subdir), env-int parse is guarded."""
from __future__ import annotations
import importlib
import os


def test_graph_pipeline_survives_garbage_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_MAX_DOER_ITERS", "abc")
    monkeypatch.setenv("AIFORGE_LOOP_MAX_WALL_S", "not-a-number")
    import aiforge_core.runtime.graph_pipeline as gp
    importlib.reload(gp)
    assert gp.MAX_DOER_ITERS == 4      # degraded to default, no crash
    assert gp.DOER_MAX_WALL_S == 0
    monkeypatch.delenv("AIFORGE_MAX_DOER_ITERS", raising=False)
    monkeypatch.delenv("AIFORGE_LOOP_MAX_WALL_S", raising=False)
    importlib.reload(gp)


def test_chat_write_repo_matches_recall_key(monkeypatch):
    # The write tool must file under the SAME key the recall path resolves,
    # so a subdir chat can recall what it wrote.
    from aiforge_core.runtime import chat_agent as ca
    captured = {}

    def _fake_mw(**kw):
        captured.update(kw)
        return {"ok": True, "id": "x"}

    monkeypatch.setattr("aiforge_core.runtime.tools.memory_write.memory_write", _fake_mw)
    cwd = os.getcwd()
    ca._t_memory_write({"text": "a durable fact"}, cwd)
    assert captured["repo"] == ca._chat_repo_key(cwd)   # write == read key
