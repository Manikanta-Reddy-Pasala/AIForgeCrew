"""Repo → local folder resolution: persist a global base + per-repo paths and
resolve a project name to a git dir (exact / case-insensitive / explicit map /
registry / text scan). Backs the 'take this folder for this repo' chat tools.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture
def base(monkeypatch):
    root = tempfile.mkdtemp()
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", root)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    os.makedirs(os.path.join(root, "MyRepo", ".git"))
    return root


def _mkgit() -> str:
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".git"))
    return d


def test_exact_and_fuzzy_under_base(base):
    from aiforge_core.runtime import workspace as ws
    assert ws.resolve_repo_dir("MyRepo") == os.path.join(base, "MyRepo")
    assert ws.resolve_repo_dir("my-repo") == os.path.join(base, "MyRepo")   # slug/ci
    assert ws.resolve_repo_dir("nope") is None


def test_explicit_path_map_wins(base):
    from aiforge_core.config import repo_map
    from aiforge_core.runtime import workspace as ws
    ext = _mkgit()
    repo_map.set_path("special", ext)
    assert ws.resolve_repo_dir("special") == ext


def test_global_root_override(base):
    from aiforge_core.config import repo_map
    from aiforge_core.runtime import workspace as ws
    newbase = tempfile.mkdtemp()
    os.makedirs(os.path.join(newbase, "Alpha", ".git"))
    repo_map.set_default_root(newbase)
    assert ws.resolve_repo_dir("Alpha") == os.path.join(newbase, "Alpha")


def test_forbidden_repo_never_resolves(base):
    from aiforge_core.runtime import workspace as ws
    os.makedirs(os.path.join(base, "AIForgeCrew", ".git"))
    assert ws.resolve_repo_dir("AIForgeCrew") is None


def test_text_scan_fallback(base):
    from aiforge_core.runtime import workspace as ws
    assert ws.resolve_repo_dir("", "please fix the MyRepo service") == \
        os.path.join(base, "MyRepo")


def test_chat_tools_persist(base):
    from aiforge_core.runtime import chat_agent as ca
    ext = _mkgit()
    assert ca._t_set_repo_folder({"repo": "foo", "path": ext}, ".")["ok"]
    from aiforge_core.runtime import workspace as ws
    assert ws.resolve_repo_dir("foo") == ext
    # non-dir path rejected
    assert not ca._t_set_repo_folder({"repo": "x", "path": "/no/such/dir"}, ".")["ok"]
    assert ca._t_set_repo_root({"path": base}, ".")["ok"]
