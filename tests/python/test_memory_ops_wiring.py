"""Tests for the memory ops wiring: the post-PR fire-and-forget delta
ingest."""
from __future__ import annotations

from aiforge_core.runtime import git_pr


# ── post-PR delta ingest ────────────────────────────────────────────────

class _T:
    project = "SomeRepo"
    identifier = "ONE-1"


def test_fire_delta_ingest_spawns_cli(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_POST_PR_INGEST", "1")
    monkeypatch.setattr(git_pr.shutil, "which",
                        lambda n: "/usr/local/bin/aiforge-memory")
    calls = {}

    def _popen(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw
        return None

    monkeypatch.setattr(git_pr.subprocess, "Popen", _popen)
    git_pr._fire_delta_ingest(_T(), "/tmp/worktree")
    assert calls["cmd"][0].endswith("aiforge-memory")
    assert calls["cmd"][1:] == ["ingest", "SomeRepo", "--path",
                                "/tmp/worktree", "--delta"]
    assert calls["kw"]["start_new_session"] is True


def test_fire_delta_ingest_disabled(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_POST_PR_INGEST", "0")
    monkeypatch.setattr(
        git_pr.subprocess, "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    git_pr._fire_delta_ingest(_T(), "/tmp/worktree")


def test_fire_delta_ingest_missing_cli_or_project(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_POST_PR_INGEST", "1")
    monkeypatch.setattr(
        git_pr.subprocess, "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    # CLI missing
    monkeypatch.setattr(git_pr.shutil, "which", lambda n: None)
    git_pr._fire_delta_ingest(_T(), "/tmp/worktree")
    # project missing
    monkeypatch.setattr(git_pr.shutil, "which", lambda n: "/bin/aiforge-memory")

    class _NoProj:
        project = ""
    git_pr._fire_delta_ingest(_NoProj(), "/tmp/worktree")