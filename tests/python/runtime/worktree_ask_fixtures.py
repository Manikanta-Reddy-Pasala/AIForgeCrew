"""Shared set-up for the worktree-ask tests: a real git project under a
projects root, a private config dir and chat store."""
from __future__ import annotations

import os
import subprocess
import time

import pytest

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV})


def age(path, days):
    """Make a folder (and what is directly in it) look ``days`` old."""
    t = time.time() - days * 86400
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                os.utime(os.path.join(root, n), (t, t), follow_symlinks=False)
            except OSError:
                pass
    os.utime(path, (t, t))
    for wt in (path, os.path.join(path, "work")):          # and git's own record of it
        try:
            first = open(os.path.join(wt, ".git"), encoding="utf-8").readline()
        except OSError:
            continue
        if first.startswith("gitdir:"):
            gd = first[len("gitdir:"):].strip()
            for n in ("index", "HEAD", os.path.join("logs", "HEAD")):
                if os.path.exists(os.path.join(gd, n)):
                    os.utime(os.path.join(gd, n), (t, t))


@pytest.fixture
def wenv(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(cfg / "chat.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(cfg / "memory"))
    monkeypatch.setenv("AIFORGE_PROJECT_INGEST", "0")
    for k in ("AIFORGE_CHAT_WORKTREE_PLACE", "AIFORGE_CHAT_WORKTREES",
              "AIFORGE_CHAT_WORKTREE_ASK"):
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "repos"
    repo = root / "shop"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "cart.py").write_text("def total(x):\n    return sum(x)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv("AIFORGE_PROJECTS_ROOT", str(root))
    from aiforge_core.config import repo_map
    monkeypatch.setattr(repo_map, "_load", lambda: {})
    from aiforge_core.memory import projects
    monkeypatch.setattr(projects, "_BOOT_REPO_ROOT", "")
    projects.forget_scan()
    from aiforge_core.runtime import chat_store, repo_ident
    chat_store.reset_backend_for_tests()
    repo_ident._GIT_TOPLEVEL_CACHE.clear()

    class E:
        pass

    e = E()
    e.repo, e.cfg, e.store = repo, cfg, chat_store
    e.real = os.path.realpath(str(repo))
    e.new_chat = lambda: chat_store.create_session("chat", str(repo))["id"]
    yield e
    chat_store.reset_backend_for_tests()
