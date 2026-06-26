"""S2 — lightweight_run_one must reject out-of-scope writes.

When a subtask carries a scope allowlist, writes whose relative path doesn't
match any glob are rejected instead of landing on disk. No allowlist → current
behavior is preserved (any file is written).
"""
from __future__ import annotations

import os

from aiforge_core.runtime import parallel_subtasks as ps


def _stub_complete(monkeypatch, output: str):
    monkeypatch.setattr(
        "aiforge_core.llm.client.complete",
        lambda *a, **k: output,
    )


def test_out_of_scope_write_rejected(tmp_path, monkeypatch):
    out = "=== secrets.env ===\nAPI_KEY=leak\n"
    _stub_complete(monkeypatch, out)
    subtask = {"slug": "s", "goal": "do it",
               "scope_allowlist_globs": ["src/*.py"]}
    res = ps.lightweight_run_one(subtask, str(tmp_path))
    assert res["ok"] is False
    assert "secrets.env" in res.get("rejected", [])
    assert not os.path.exists(os.path.join(str(tmp_path), "secrets.env"))


def test_in_scope_write_allowed(tmp_path, monkeypatch):
    out = "=== src/app.py ===\nprint('ok')\n"
    _stub_complete(monkeypatch, out)
    subtask = {"slug": "s", "goal": "do it",
               "scope_allowlist_globs": ["src/*.py"]}
    res = ps.lightweight_run_one(subtask, str(tmp_path))
    assert res["ok"] is True
    assert "src/app.py" in res["files"]
    assert os.path.exists(os.path.join(str(tmp_path), "src", "app.py"))


def test_mixed_scope_writes_only_in_scope(tmp_path, monkeypatch):
    out = ("=== src/app.py ===\nprint('ok')\n"
           "=== secrets.env ===\nAPI_KEY=leak\n")
    _stub_complete(monkeypatch, out)
    subtask = {"slug": "s", "goal": "do it",
               "scope_allowlist_globs": ["src/*.py"]}
    res = ps.lightweight_run_one(subtask, str(tmp_path))
    assert res["ok"] is True
    assert "src/app.py" in res["files"]
    assert "secrets.env" in res.get("rejected", [])
    assert not os.path.exists(os.path.join(str(tmp_path), "secrets.env"))


def test_no_allowlist_preserves_behavior(tmp_path, monkeypatch):
    out = "=== anywhere.py ===\nx = 1\n"
    _stub_complete(monkeypatch, out)
    subtask = {"slug": "s", "goal": "do it"}   # no scope_allowlist_globs
    res = ps.lightweight_run_one(subtask, str(tmp_path))
    assert res["ok"] is True
    assert os.path.exists(os.path.join(str(tmp_path), "anywhere.py"))
