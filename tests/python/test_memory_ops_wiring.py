"""Tests for the memory ops wiring: central Neo4j conn params + the
post-PR fire-and-forget delta ingest."""
from __future__ import annotations

from aiforge_core.memory.neo4j_conn import neo4j_params
from aiforge_core.runtime import git_pr

# ── neo4j_params fallback chain ─────────────────────────────────────────

def test_neo4j_params_prefers_aiforge_env(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_NEO4J_URI", "bolt://a:1")
    monkeypatch.setenv("AIFORGE_NEO4J_USER", "ua")
    monkeypatch.setenv("AIFORGE_NEO4J_PASSWORD", "pa")
    monkeypatch.setenv("NEO4J_PASSWORD", "pb")
    assert neo4j_params() == ("bolt://a:1", "ua", "pa")


def test_neo4j_params_falls_back_to_plain_env(monkeypatch) -> None:
    for k in ("AIFORGE_NEO4J_URI", "AIFORGE_NEO4J_USER",
              "AIFORGE_NEO4J_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEO4J_URI", "bolt://b:2")
    monkeypatch.setenv("NEO4J_USER", "ub")
    monkeypatch.setenv("NEO4J_PASSWORD", "pb")
    assert neo4j_params() == ("bolt://b:2", "ub", "pb")


def test_neo4j_params_defaults(monkeypatch) -> None:
    for k in ("AIFORGE_NEO4J_URI", "AIFORGE_NEO4J_USER",
              "AIFORGE_NEO4J_PASSWORD", "NEO4J_URI", "NEO4J_USER",
              "NEO4J_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert neo4j_params() == ("bolt://127.0.0.1:7687", "neo4j", "password")


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