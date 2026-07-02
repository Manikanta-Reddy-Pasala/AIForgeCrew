"""Concurrency isolation for request-scoped runtime state.

The multi-threaded API serves concurrent chats on DIFFERENT repos. The old
code stashed the active repo root / workspace dir / delegation depth in
``os.environ`` (process-global) which concurrent runs clobbered. These state
values now live in ``contextvars.ContextVar`` (auto-isolated per thread /
async-task). These tests prove:

* two threads each see their OWN value — no clobber,
* the getter is contextvar-first, env-fallback, None-safe,
* the path jail (`chat_agent._workspace_root`) resolves per-context,
* the delegation depth counter nests correctly and stays per-context.
"""
from __future__ import annotations

import os
import threading

import pytest

from aiforge_core.runtime import request_context as rc


# ───────────────────────── repo_root ──────────────────────────────

def test_repo_root_getter_precedence_and_none_safe(monkeypatch):
    # nothing set → None (soft, never raises)
    monkeypatch.delenv("AIFORGE_REPO_ROOT", raising=False)
    assert rc.get_repo_root() is None
    # env only → env value
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/env/repo")
    assert rc.get_repo_root() == "/env/repo"
    # contextvar wins over env
    tok = rc.set_repo_root("/ctx/repo")
    try:
        assert rc.get_repo_root() == "/ctx/repo"
    finally:
        rc.reset_repo_root(tok)
    # after reset, falls back to env again
    assert rc.get_repo_root() == "/env/repo"


def test_repo_root_isolation_across_threads(monkeypatch):
    # A shared env value would be the clobber target under the old design.
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/shared/env")
    barrier = threading.Barrier(2)
    results: dict[str, str | None] = {}

    def worker(name: str, path: str):
        rc.set_repo_root(path)
        barrier.wait()            # both have set BEFORE either reads
        barrier.wait()            # hold until both have read
        results[name] = rc.get_repo_root()

    tA = threading.Thread(target=worker, args=("A", "/repoA"))
    tB = threading.Thread(target=worker, args=("B", "/repoB"))
    tA.start(); tB.start()
    tA.join(); tB.join()

    assert results["A"] == "/repoA"
    assert results["B"] == "/repoB"


# ───────────────────────── workspace_dir ──────────────────────────

def test_workspace_dir_getter_precedence_and_none_safe(monkeypatch):
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    assert rc.get_workspace_dir() is None
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", "/env/ws")
    assert rc.get_workspace_dir() == "/env/ws"
    tok = rc.set_workspace_dir("/ctx/ws")
    try:
        assert rc.get_workspace_dir() == "/ctx/ws"
    finally:
        rc.reset_workspace_dir(tok)
    assert rc.get_workspace_dir() == "/env/ws"


def test_workspace_dir_isolation_across_threads(monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", "/shared/env")
    barrier = threading.Barrier(2)
    results: dict[str, str | None] = {}

    def worker(name: str, path: str):
        rc.set_workspace_dir(path)
        barrier.wait()
        barrier.wait()
        results[name] = rc.get_workspace_dir()

    tA = threading.Thread(target=worker, args=("A", "/wsA"))
    tB = threading.Thread(target=worker, args=("B", "/wsB"))
    tA.start(); tB.start()
    tA.join(); tB.join()

    assert results["A"] == "/wsA"
    assert results["B"] == "/wsB"


def test_workspace_jail_resolves_per_context(tmp_path, monkeypatch):
    """A concurrent chat on /repoB must NOT get /repoA's jail."""
    from aiforge_core.runtime import chat_agent
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)

    repoA = tmp_path / "repoA"; repoA.mkdir()
    repoB = tmp_path / "repoB"; repoB.mkdir()
    barrier = threading.Barrier(2)
    roots: dict[str, str] = {}

    def worker(name: str, path):
        rc.set_workspace_dir(str(path))
        barrier.wait()
        roots[name] = str(chat_agent._workspace_root())

    tA = threading.Thread(target=worker, args=("A", repoA))
    tB = threading.Thread(target=worker, args=("B", repoB))
    tA.start(); tB.start()
    tA.join(); tB.join()

    assert roots["A"] == str(repoA.resolve())
    assert roots["B"] == str(repoB.resolve())

    # And the jail actually rejects an escape for the per-context root.
    tok = rc.set_workspace_dir(str(repoA))
    try:
        # in-jail path is fine
        chat_agent._resolve(str(repoA), "sub/file.txt")
        with pytest.raises(PermissionError):
            chat_agent._resolve(str(repoA), "../repoB/secret.txt")
    finally:
        rc.reset_workspace_dir(tok)


# ───────────────────────── delegation depth ───────────────────────

def test_delegation_depth_nests_within_one_context():
    assert rc.get_delegation_depth() == 0
    t1 = rc.enter_delegation()
    assert rc.get_delegation_depth() == 1
    t2 = rc.enter_delegation()
    assert rc.get_delegation_depth() == 2
    rc.reset_delegation(t2)
    assert rc.get_delegation_depth() == 1
    rc.reset_delegation(t1)
    assert rc.get_delegation_depth() == 0


def test_delegation_depth_independent_across_contexts():
    barrier = threading.Barrier(2)
    depths: dict[str, int] = {}

    def worker(name: str, times: int):
        toks = [rc.enter_delegation() for _ in range(times)]
        barrier.wait()
        depths[name] = rc.get_delegation_depth()
        for t in reversed(toks):
            rc.reset_delegation(t)

    tA = threading.Thread(target=worker, args=("A", 3))
    tB = threading.Thread(target=worker, args=("B", 1))
    tA.start(); tB.start()
    tA.join(); tB.join()

    assert depths["A"] == 3
    assert depths["B"] == 1


def test_delegation_cap_fires_within_a_chain(monkeypatch):
    """The depth cap still short-circuits (no ADK spawn) when depth >= max."""
    from aiforge_core.runtime.tools import delegation
    monkeypatch.setenv("AIFORGE_DELEGATION_MAX_DEPTH", "1")
    tok = rc.enter_delegation()  # depth now 1 == max
    try:
        out = delegation.delegate_to_agent("researcher", "do a thing")
        assert out["ok"] is False
        assert out["error"] == "delegation_depth_exceeded"
        assert out["depth"] == 1
    finally:
        rc.reset_delegation(tok)
