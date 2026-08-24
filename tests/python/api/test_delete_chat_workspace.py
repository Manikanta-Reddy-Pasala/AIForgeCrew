"""_delete_chat_workspace must rm -rf a managed session workspace but REFUSE
anything else — a user-pinned repo, the root itself, or a path outside the
managed tree — so clearing a chat can never nuke a real project."""
from __future__ import annotations

import os

import pytest

from aiforge_core.api import api


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    root = tmp_path / "chat-workspaces"
    root.mkdir()
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(root))
    return root


def test_deletes_managed_session_dir(ws_root):
    d = ws_root / "session-5"
    d.mkdir()
    (d / "old.py").write_text("x")
    assert api._delete_chat_workspace(str(d)) is True
    assert not d.exists()


def test_refuses_pinned_user_repo(ws_root, tmp_path):
    repo = tmp_path / "my-project"
    repo.mkdir()
    (repo / "important.py").write_text("keep me")
    assert api._delete_chat_workspace(str(repo)) is False
    assert repo.exists()
    assert (repo / "important.py").exists()


def test_refuses_the_root_itself(ws_root):
    assert api._delete_chat_workspace(str(ws_root)) is False
    assert ws_root.exists()


def test_refuses_non_session_dir_inside_root(ws_root):
    other = ws_root / "notasession"
    other.mkdir()
    assert api._delete_chat_workspace(str(other)) is False
    assert other.exists()


def test_refuses_traversal_escape(ws_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("y")
    sneaky = str(ws_root / "session-1" / ".." / ".." / "outside")
    assert api._delete_chat_workspace(sneaky) is False
    assert outside.exists()


def test_empty_or_none_is_noop(ws_root):
    assert api._delete_chat_workspace(None) is False
    assert api._delete_chat_workspace("") is False
    assert api._delete_chat_workspace("   ") is False
