"""OLD worktrees of a project: what is listed, and what "delete" does to it.
Real git throughout."""
from __future__ import annotations

import os
import types

import pytest

from tests.python.runtime.worktree_ask_fixtures import age, git, wenv  # noqa: F401


def _inv():
    from aiforge_core.runtime import worktree_inventory
    return worktree_inventory


@pytest.fixture
def tickets(monkeypatch):
    statuses: dict = {}
    from aiforge_core.tickets import store
    monkeypatch.setattr(store, "get", lambda key: types.SimpleNamespace(status=statuses[key])
                        if key in statuses else None)
    return statuses


def _ticket_wt(repo, name, branch):
    wt = os.path.join(str(repo), ".aiforge-worktrees", name)
    assert git(repo, "worktree", "add", "-q", "-b", branch, wt).returncode == 0
    return wt


def _by_name(entries):
    return {e["name"]: e for e in entries}


def test_classification(wenv, tickets):
    inv = _inv()
    from aiforge_core.runtime import chat_worktree as cw
    active = wenv.new_chat()
    a = cw.ensure(active)
    gone = wenv.new_chat()
    g = cw.ensure(gone)
    wenv.store.delete_session(gone)                       # session gone, worktree left
    done = _ticket_wt(wenv.repo, "TAL-48", "t48")
    open(os.path.join(done, "notes.txt"), "w").write("x\n")          # 1 uncommitted
    unk = _ticket_wt(wenv.repo, "TAL-51", "t51")
    open(os.path.join(unk, "f.py"), "w").write("y = 1\n")
    git(unk, "add", "-A")
    git(unk, "commit", "-q", "-m", "work")                          # 1 unmerged
    live = _ticket_wt(wenv.repo, "TAL-60", "t60")
    tickets.update({"TAL-48": "done", "TAL-60": "in_progress"})
    dead = os.path.join(str(wenv.repo), ".aiforge-worktrees", "TAL-50")
    os.makedirs(dead)
    open(os.path.join(dead, "left.txt"), "w").write("z\n")
    branchwt = os.path.join(str(wenv.repo), ".aiforge-worktrees", "branch-rel", "work")
    git(wenv.repo, "worktree", "add", "-q", "-b", "rel", branchwt)
    holder = os.path.join(str(wenv.repo), ".aiforge-worktrees")
    for name in os.listdir(holder):
        age(os.path.join(holder, name), 30)

    got = _by_name(inv.inventory(str(wenv.repo)))
    chat_a = os.path.basename(os.path.dirname(a["path"]))
    chat_g = os.path.basename(os.path.dirname(g["path"]))
    assert got[chat_a]["kind"] == "chat" and got[chat_a]["owner"] == "active" and not got[chat_a]["old"]
    assert got[chat_g]["owner"] == "gone" and got[chat_g]["old"] and got[chat_g]["registered"]
    assert got["TAL-48"]["owner"] == "terminal" and got["TAL-48"]["old"]
    assert got["TAL-48"]["uncommitted"] == 1 and got["TAL-48"]["unmerged"] == 0
    assert got["TAL-51"]["owner"] == "unknown" and got["TAL-51"]["old"]
    assert got["TAL-51"]["unmerged"] == 1
    assert got["TAL-60"]["owner"] == "active" and not got["TAL-60"]["old"]
    assert got["TAL-50"]["dead"] and got["TAL-50"]["old"] and got["TAL-50"]["size"] > 0
    assert got["branch-rel"]["kind"] == "other" and got["branch-rel"]["old"]   # 30 days idle
    assert got["TAL-48"]["age_days"] >= 29

    old = inv.old_entries(str(wenv.repo))
    assert {e["name"] for e in old} == {chat_g, "TAL-48", "TAL-51", "TAL-50", "branch-rel"}
    text = inv.format_list(old)
    assert "TAL-48" in text and "1 uncommitted" in text and "⚠ unsaved work" in text
    assert "dead folder (git no longer registers it)" in text
    assert 'Reply "delete worktrees 1,3", "delete worktrees all" or "keep worktrees".' in text
    assert os.path.join(str(wenv.repo), ".aiforge-worktrees", "TAL-50") in text


def test_recent_and_branch_folders_in_use_are_not_listed(wenv, tickets):
    inv = _inv()
    dead = os.path.join(str(wenv.repo), ".aiforge-worktrees", "chat-9-abcd")
    os.makedirs(dead)                       # being made right now (no age yet)
    git(wenv.repo, "worktree", "add", "-q", "-b", "rel",
        os.path.join(str(wenv.repo), ".aiforge-worktrees", "branch-rel", "work"))
    assert inv.old_entries(str(wenv.repo)) == []


def test_an_empty_holder_folder_is_removed_silently(wenv):
    inv = _inv()
    holder = os.path.join(str(wenv.repo), ".aiforge-worktrees")
    os.makedirs(holder)
    assert inv.inventory(str(wenv.repo)) == [] and not os.path.exists(holder)
    os.makedirs(os.path.join(holder, "x"))
    inv.remove_empty_holder(str(wenv.repo))
    assert os.path.isdir(holder)                          # not empty: left alone


def test_delete_commits_then_removes_and_keeps_the_branch_and_trashes_dead(wenv, tickets):
    inv = _inv()
    done = _ticket_wt(wenv.repo, "TAL-48", "t48")
    open(os.path.join(done, "notes.txt"), "w").write("keep me\n")
    dead = os.path.join(str(wenv.repo), ".aiforge-worktrees", "TAL-50")
    os.makedirs(dead)
    open(os.path.join(dead, "left.txt"), "w").write("z\n")
    keep = _ticket_wt(wenv.repo, "TAL-52", "t52")
    tickets.update({"TAL-48": "done", "TAL-52": "cancelled"})
    holder = os.path.join(str(wenv.repo), ".aiforge-worktrees")
    for name in os.listdir(holder):
        age(os.path.join(holder, name), 20)
    old = inv.old_entries(str(wenv.repo))
    paths = [e["path"] for e in old]
    names = [e["name"] for e in old]
    picks = [names.index("TAL-48") + 1, names.index("TAL-50") + 1]
    lines = inv.delete(str(wenv.repo), paths, picks)
    text = "\n".join(lines)
    assert "TAL-48: removed (branch `t48` kept, 1 uncommitted file committed first)" in text
    assert not os.path.exists(done)
    assert git(wenv.repo, "show", "t48:notes.txt").stdout == "keep me\n"   # nothing lost
    assert "notes.txt" not in git(wenv.repo, "status", "--porcelain").stdout
    assert done not in git(wenv.repo, "worktree", "list").stdout
    assert "TAL-50: dead folder moved to " in text and not os.path.exists(dead)
    trash = str(wenv.cfg / "worktree-trash")
    moved = [os.path.join(r, f) for r, _d, fs in os.walk(trash) for f in fs]
    assert any(p.endswith(os.path.join("TAL-50", "left.txt")) for p in moved)
    assert os.path.isdir(keep)                            # not picked: untouched


def test_delete_skips_an_entry_that_is_in_use_again(wenv, tickets):
    inv = _inv()
    wt = _ticket_wt(wenv.repo, "TAL-48", "t48")
    tickets["TAL-48"] = "done"
    age(wt, 20)
    paths = [e["path"] for e in inv.old_entries(str(wenv.repo))]
    tickets["TAL-48"] = "in_progress"                     # reopened meanwhile
    assert inv.delete(str(wenv.repo), paths, "all") == ["TAL-48: kept — it is in use again"]
    assert os.path.isdir(wt)


def test_trash_older_than_two_weeks_is_pruned(wenv):
    inv = _inv()
    old = os.path.join(inv.trash_root(), "20200101-000000")
    new = os.path.join(inv.trash_root(), "20990101-000000")
    os.makedirs(os.path.join(old, "x"))
    os.makedirs(new)
    age(old, 15)
    assert inv.prune_trash() == 1
    assert not os.path.exists(old) and os.path.isdir(new)


@pytest.mark.parametrize("text,cmd,rest", [
    ("delete worktrees 1,3", ("delete", [1, 3]), ""),
    ("Delete worktrees all", ("delete", "all"), ""),
    ("remove worktree 2 and 4", ("delete", [2, 4]), ""),
    ("keep worktrees", ("keep", None), ""),
    ("2, delete worktrees all", ("delete", "all"), "2"),
    ("fix the bug", None, "fix the bug"),
])
def test_commands_are_read(text, cmd, rest):
    got, r = _inv().parse_command(text)
    assert got == cmd and r == rest


def test_keep_snoozes_the_same_set_for_a_week(wenv):
    inv = _inv()
    key = inv.set_key([{"path": "/a"}, {"path": "/b"}])
    assert not inv.snoozed(str(wenv.repo), key)
    inv.snooze(str(wenv.repo), key)
    assert inv.snoozed(str(wenv.repo), key)
    assert not inv.snoozed(str(wenv.repo), inv.set_key([{"path": "/a"}]))   # a new set shows
    inv.snooze(str(wenv.repo), key, days=-1)
    assert not inv.snoozed(str(wenv.repo), key)
