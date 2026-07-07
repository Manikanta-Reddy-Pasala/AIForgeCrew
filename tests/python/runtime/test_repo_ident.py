"""One canonical repo-name resolver — the chat/skills resolvers delegate to it,
so a repo resolves to the SAME key everywhere (no more written-here-read-there).
"""
from __future__ import annotations

import os
import subprocess
import tempfile


def _git_repo() -> str:
    d = tempfile.mkdtemp(prefix="MyProj-")
    subprocess.run(["git", "init", "-q", d], check=True)
    os.makedirs(os.path.join(d, "sub", "deep"), exist_ok=True)
    return d


def test_subdir_resolves_to_git_root_name():
    from aiforge_core.runtime import repo_ident as ri
    root = _git_repo()
    name = os.path.basename(root)
    assert ri.repo_name(root) == name
    assert ri.repo_name(os.path.join(root, "sub", "deep")) == name   # subdir == root


def test_all_resolvers_agree():
    from aiforge_core.runtime import repo_ident as ri
    from aiforge_core.runtime import chat_agent, skills
    root = _git_repo()
    sub = os.path.join(root, "sub")
    canon = ri.repo_name(root)
    # chat_agent._chat_repo_key, _repo_name, skills._repo_name all resolve the
    # same git-toplevel key for a subdir (only the sentinel differs on no-match).
    assert chat_agent._chat_repo_key(sub) == canon
    assert chat_agent._repo_name(sub) == canon
    assert skills._repo_name(sub) == canon


def test_non_git_falls_back_to_sentinel(monkeypatch):
    from aiforge_core.runtime import repo_ident as ri
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    assert ri.repo_name(None, sentinel="repo") == "repo"
    assert ri.repo_name(None, sentinel="skills") == "skills"
