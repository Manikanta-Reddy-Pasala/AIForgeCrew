"""Checkpoint restore granularity (Cline-parity) + edit-resend store helpers."""
from __future__ import annotations

import subprocess

import pytest


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True,
                          check=False)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a0\n")
    (tmp_path / "b.txt").write_text("b0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return str(tmp_path)


def test_subset_restore_only_touches_given_paths(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHECKPOINT_DIR", str(tmp_path / "ck"))
    from aiforge_core.runtime import checkpoints
    snap = checkpoints.snapshot(repo, label="s1")
    assert snap["ok"]
    (tmp_path / "a.txt").write_text("a1\n")
    (tmp_path / "b.txt").write_text("b1\n")
    res = checkpoints.restore(repo, snap["sha"], paths=["a.txt"])
    assert res["ok"]
    assert (tmp_path / "a.txt").read_text() == "a0\n"   # restored
    assert (tmp_path / "b.txt").read_text() == "b1\n"   # untouched


def test_default_restore_leaves_orphans(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHECKPOINT_DIR", str(tmp_path / "ck"))
    from aiforge_core.runtime import checkpoints
    snap = checkpoints.snapshot(repo, label="s1")
    (tmp_path / "new.txt").write_text("new\n")
    res = checkpoints.restore(repo, snap["sha"])
    assert "new.txt" in res["left_in_place"]
    assert res["deleted"] == []
    assert (tmp_path / "new.txt").exists()


def test_delete_orphans_full_state_restore(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHECKPOINT_DIR", str(tmp_path / "ck"))
    from aiforge_core.runtime import checkpoints
    snap = checkpoints.snapshot(repo, label="s1")
    (tmp_path / "new.txt").write_text("new\n")
    res = checkpoints.restore(repo, snap["sha"], delete_orphans=True)
    assert "new.txt" in res["deleted"]
    assert not (tmp_path / "new.txt").exists()
    assert res["left_in_place"] == []


def test_chat_store_edit_resend_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    from aiforge_core.runtime import chat_store
    s = chat_store.create_session("t")
    sid = s["id"]
    m1 = chat_store.add_message(sid, "user", "first")
    chat_store.add_message(sid, "assistant", "reply1")
    m3 = chat_store.add_message(sid, "user", "second")
    chat_store.add_message(sid, "assistant", "reply2")
    # stamp + read back a checkpoint
    chat_store.set_message_checkpoint(m1, "deadbeef")
    assert chat_store.message_checkpoint(sid, m1) == "deadbeef"
    assert chat_store.message_checkpoint(sid, m3) is None
    msgs = chat_store.get_messages(sid)
    assert msgs[0]["checkpoint_sha"] == "deadbeef"
    # truncate from the 2nd user turn → removes m3 + its reply (2 rows)
    removed = chat_store.delete_messages_from(sid, m3)
    assert removed == 2
    remaining = [m["content"] for m in chat_store.get_messages(sid)]
    assert remaining == ["first", "reply1"]


# ── a turn's Changes: since ITS checkpoint, without touching the user's index ──

def _changes(cwd, since):
    from aiforge_core.runtime.parallel_subtasks import _emit_changes
    evs = list(_emit_changes(cwd, since, include_worktree=True))
    return {f["path"]: f for f in evs[0]["files"]} if evs else {}


def test_changes_are_this_turns_only_and_the_index_is_untouched(repo, tmp_path, monkeypatch):
    # sidecar OUTSIDE the repo, or it shows up as a change itself
    monkeypatch.setenv("AIFORGE_CHECKPOINT_DIR", str(tmp_path.parent / f"{tmp_path.name}-ck"))
    from aiforge_core.runtime import checkpoints
    (tmp_path / "a.txt").write_text("user wip\n")          # the user's own edit
    snap = checkpoints.snapshot(repo, label="before turn 2")
    (tmp_path / "b.txt").write_text("b1\n")                # this turn
    (tmp_path / "new.txt").write_text("n\n")               # this turn, untracked
    files = _changes(repo, snap["sha"])
    assert set(files) == {"b.txt", "new.txt"}              # not a.txt
    assert files["new.txt"]["status"] == "added"
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""
    assert "?? new.txt" in _git(repo, "status", "--porcelain").stdout


def test_changes_in_a_subfolder_of_a_repo_are_relative_to_it(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHECKPOINT_DIR", str(tmp_path / "ck"))
    from aiforge_core.runtime import checkpoints
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "x.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "pkg")
    snap = checkpoints.snapshot(str(sub), label="before")
    (sub / "x.py").write_text("x = 2\n")
    (tmp_path / "a.txt").write_text("outside\n")           # outside the project
    files = _changes(str(sub), snap["sha"])
    assert set(files) == {"x.py"}
    assert "+x = 2" in files["x.py"]["diff"]


def test_undo_of_a_file_created_after_the_checkpoint_removes_it(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHECKPOINT_DIR", str(tmp_path / "ck"))
    from aiforge_core.runtime import checkpoints
    snap = checkpoints.snapshot(repo, label="s")
    (tmp_path / "made.txt").write_text("m\n")
    (tmp_path / "a.txt").write_text("a1\n")
    res = checkpoints.restore(repo, snap["sha"], paths=["made.txt"], delete_orphans=True)
    assert res["ok"], res
    assert not (tmp_path / "made.txt").exists()
    assert (tmp_path / "a.txt").read_text() == "a1\n"      # not asked for
