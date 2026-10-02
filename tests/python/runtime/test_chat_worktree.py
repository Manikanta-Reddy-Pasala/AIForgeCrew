"""Every chat on a git project works in its own worktree.

Real git throughout. What must hold: the user's folder is not touched while a
chat works; the chat's identity (memory key, project, lists) stays the project;
each turn is committed to the chat's branch; merging is fast-forward only and
refuses rather than risk the user's uncommitted work; deleting the chat keeps
its branch; two chats in one project do not see each other's edits.
"""
from __future__ import annotations

import os
import subprocess

import pytest


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "cfg" / "chat.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "cfg" / "memory"))
    monkeypatch.setenv("AIFORGE_PROJECT_INGEST", "0")
    root = tmp_path / "repos"
    repo = root / "shop"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "cart.py").write_text("def total(x):\n    return sum(x)\n")
    (repo / "README.md").write_text("# shop\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
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
    e.repo, e.root, e.store, e.projects = repo, root, chat_store, projects
    e.new_chat = lambda: chat_store.create_session("chat", str(repo))["id"]
    yield e
    chat_store.reset_backend_for_tests()


def _cw():
    from aiforge_core.runtime import chat_worktree
    return chat_worktree


def test_a_chat_gets_a_worktree_on_its_own_branch_and_the_users_folder_is_untouched(env):
    cw = _cw()
    sid = env.new_chat()
    before = _git(env.repo, "status", "--porcelain").stdout
    data = cw.ensure(sid)
    wt = data["path"]
    assert os.path.isdir(wt) and cw.is_worktree(wt)
    assert os.sep + "chat-worktrees" + os.sep in wt                               # under the config dir
    assert not wt.startswith(str(env.repo))                                       # not inside the repo
    assert data["branch"].startswith("aiforge/chat-") and data["base_branch"] == "main"
    assert (env.repo / "src" / "cart.py").read_text() == (open(os.path.join(wt, "src", "cart.py")).read())
    # the session keeps the project as its folder; the worktree is where it runs
    sess = env.store.get_session(sid)
    assert sess["cwd"] == str(env.repo) and sess["workdir"] == wt
    # nothing changed in the user's folder or branch
    assert _git(env.repo, "status", "--porcelain").stdout == before
    assert _git(env.repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert _git(env.repo, "branch", "--list", data["branch"]).stdout.strip()


def test_the_chats_identity_stays_the_project(env):
    from aiforge_core.runtime import repo_ident
    from aiforge_core.runtime.chat_agent import _chat_repo_key
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    # memory is filed under "shop", not under "work" (the worktree's folder name)
    assert repo_ident.repo_name(wt) == "shop"
    assert _chat_repo_key(wt) == "shop"
    assert cw.main_repo_of(wt) == os.path.realpath(env.repo) or cw.main_repo_of(wt) == str(env.repo)
    assert env.projects.project_path_of(wt) == str(env.repo)
    assert env.projects.project_of(wt) == "shop"


def test_the_state_folder_is_shared_through_a_link(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    link = os.path.join(wt, ".aiforge")
    assert os.path.islink(link) and os.path.realpath(link) == os.path.realpath(env.repo / ".aiforge")
    # a rule written through the link lands in the project, and git ignores the link
    os.makedirs(os.path.join(link, "rules"), exist_ok=True)
    open(os.path.join(link, "rules", "r.md"), "w").write("- be kind\n")
    assert (env.repo / ".aiforge" / "rules" / "r.md").read_text() == "- be kind\n"
    assert ".aiforge" not in _git(wt, "status", "--porcelain").stdout


def test_two_chats_do_not_see_each_others_edits(env):
    cw = _cw()
    a, b = env.new_chat(), env.new_chat()
    wa, wb = cw.ensure(a)["path"], cw.ensure(b)["path"]
    assert wa != wb
    open(os.path.join(wa, "src", "cart.py"), "w").write("def total(x):\n    return 111\n")
    open(os.path.join(wb, "src", "cart.py"), "w").write("def total(x):\n    return 222\n")
    assert "111" in open(os.path.join(wa, "src", "cart.py")).read()
    assert "222" in open(os.path.join(wb, "src", "cart.py")).read()
    assert "return sum(x)" in (env.repo / "src" / "cart.py").read_text()      # the user's file


def test_each_turn_is_committed_to_the_chats_branch(env):
    cw = _cw()
    sid = env.new_chat()
    data = cw.ensure(sid)
    wt = data["path"]
    assert cw.seal_for_session(sid, "add a tax function") == []                # nothing yet
    open(os.path.join(wt, "src", "tax.py"), "w").write("def vat(x):\n    return x * 0.2\n")
    open(os.path.join(wt, "junk.pyc"), "w").write("")
    # editing shows as uncommitted; a cache file is not an edit and is never committed
    assert cw.info(env.store.get_session(sid))["uncommitted"] == ["src/tax.py"]
    assert cw.seal_for_session(sid, "add a tax function") == ["src/tax.py"]
    log = _git(wt, "log", "--format=%s", "-n", "2").stdout.splitlines()
    assert log[0] == f"aiforge chat {sid}: add a tax function"
    info = cw.info(env.store.get_session(sid))
    assert info["ahead"] == 1 and info["uncommitted"] == []
    # the state link is never committed
    assert ".aiforge" not in _git(wt, "show", "--name-only", "--format=", "HEAD").stdout


def test_merge_fast_forwards_the_users_branch(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "src", "tax.py"), "w").write("def vat(x):\n    return x * 0.2\n")
    res = cw.merge(sid)
    assert res["ok"] is True and res["merged"] == 1 and res["into"] == "main"
    assert (env.repo / "src" / "tax.py").exists()
    assert _git(env.repo, "status", "--porcelain").stdout.strip() == ""
    assert cw.info(env.store.get_session(sid))["ahead"] == 0
    assert cw.merge(sid)["message"].startswith("Nothing to merge")


def test_merge_waits_while_the_users_folder_has_uncommitted_changes(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "src", "tax.py"), "w").write("x = 1\n")
    (env.repo / "README.md").write_text("# shop — my local edit\n")
    res = cw.merge(sid)
    assert res["ok"] is False and res["reason"] == "main_dirty" and "README.md" in res["message"]
    assert (env.repo / "README.md").read_text() == "# shop — my local edit\n"     # untouched
    assert not (env.repo / "src" / "tax.py").exists()


def test_merge_refuses_when_the_users_branch_changed(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "src", "tax.py"), "w").write("x = 1\n")
    _git(env.repo, "checkout", "-q", "-b", "other")
    res = cw.merge(sid)
    assert res["ok"] is False and res["reason"] == "branch_changed" and "other" in res["message"]


def test_merge_rebases_the_chat_when_the_users_branch_moved_on(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "src", "tax.py"), "w").write("x = 1\n")
    (env.repo / "NOTES.md").write_text("notes\n")                  # the user commits meanwhile
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-q", "-m", "user work")
    res = cw.merge(sid)
    assert res["ok"] is True, res
    assert (env.repo / "src" / "tax.py").exists() and (env.repo / "NOTES.md").exists()
    assert _git(env.repo, "log", "--format=%s", "-n", "3").stdout.splitlines()[1] == "user work"


def test_a_conflict_is_reported_and_nothing_is_left_half_done(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "src", "cart.py"), "w").write("def total(x):\n    return 111\n")
    (env.repo / "src" / "cart.py").write_text("def total(x):\n    return 999\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-q", "-m", "user edit of the same line")
    res = cw.merge(sid)
    assert res["ok"] is False and res["reason"] == "conflict"
    assert (env.repo / "src" / "cart.py").read_text() == "def total(x):\n    return 999\n"
    assert _git(env.repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(wt, "status", "--porcelain").stdout.strip() == ""         # rebase was aborted
    assert not os.path.exists(os.path.join(env.repo, ".git", "MERGE_HEAD"))


def test_deleting_the_chat_removes_the_worktree_and_keeps_the_branch(env):
    cw = _cw()
    sid = env.new_chat()
    data = cw.ensure(sid)
    open(os.path.join(data["path"], "src", "tax.py"), "w").write("x = 1\n")   # uncommitted
    out = cw.remove(sid)
    assert out["removed"] is True and not os.path.exists(data["path"])
    branches = _git(env.repo, "branch", "--list", "aiforge/*").stdout
    assert data["branch"] in branches                                          # branch kept
    # the uncommitted work was committed before the folder went
    assert "tax.py" in _git(env.repo, "show", "--name-only", "--format=", data["branch"]).stdout
    assert env.store.get_session(sid)["workdir"] is None
    assert len(_git(env.repo, "worktree", "list").stdout.strip().splitlines()) == 1   # only the repo


def test_who_does_not_get_one(env, monkeypatch, tmp_path):
    cw = _cw()
    sid = env.new_chat()
    sess = env.store.get_session(sid)
    assert cw.eligible(sess, "simple") == os.path.realpath(env.repo)
    assert cw.eligible(sess, "team") is None                                   # team makes one per run
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREES", "0")
    assert cw.eligible(sess, "simple") is None                                 # switched off
    monkeypatch.delenv("AIFORGE_CHAT_WORKTREES")
    child = env.store.create_session("side", str(env.repo))
    env.store.set_session_task(child["id"], sid, {"state": "queued"})
    assert cw.eligible(env.store.get_session(child["id"]), "simple") is None   # side task
    plain = env.root / "plain"
    plain.mkdir()
    assert cw.eligible({"id": 9, "cwd": str(plain)}, "simple") is None         # not a git repo
    elsewhere = tmp_path / "elsewhere"
    (elsewhere).mkdir()
    _git(elsewhere, "init", "-q")
    assert cw.eligible({"id": 9, "cwd": str(elsewhere)}, "simple") is None     # not in a project
    empty = env.root / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    assert cw.eligible({"id": 9, "cwd": str(empty)}, "simple") is None         # no commit to branch from


def test_a_side_task_shares_its_parents_worktree_and_removes_nothing(env):
    from aiforge_core.api.routes._chat import _side_tasks as st
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    child = env.store.create_session("side", str(env.repo))
    env.store.set_session_task(child["id"], sid, {"state": "queued"})
    env.store.set_session_workdir(child["id"], wt)
    assert cw.workdir_of(env.store.get_session(child["id"])) == wt
    assert cw.seal_for_session(child["id"], "x") == []                         # the parent commits
    assert cw.remove(child["id"]) == {"removed": False}
    assert os.path.isdir(wt)


def test_a_vanished_worktree_is_made_again(env):
    cw = _cw()
    sid = env.new_chat()
    first = cw.ensure(sid)["path"]
    import shutil
    shutil.rmtree(first)                       # the user cleaned the folder up
    _git(env.repo, "worktree", "prune")
    sess = env.store.get_session(sid)
    assert cw.workdir_of(sess) is None
    second = cw.ensure(sid)["path"]
    assert os.path.isdir(second) and second != first


def test_the_agent_is_told_where_it_works(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    note = cw.prompt_note(wt)
    assert "its own git worktree" in note and wt in note and str(env.repo) in note
    assert "belongs to the user" in note
    assert cw.prompt_note(str(env.repo)) == ""


def test_checkpoint_numbers_are_not_reused_across_worktrees(env):
    from aiforge_core.runtime import checkpoints
    cw = _cw()
    a, b = env.new_chat(), env.new_chat()
    wa, wb = cw.ensure(a)["path"], cw.ensure(b)["path"]
    open(os.path.join(wa, "a.txt"), "w").write("a")
    open(os.path.join(wb, "b.txt"), "w").write("b")
    ra = checkpoints.snapshot(wa, "a")
    rb = checkpoints.snapshot(wb, "b")
    assert ra["ok"] and rb["ok"] and ra["ref"] != rb["ref"]


def test_only_a_fresh_chat_gets_one(env):
    """A chat that has already answered has been working in the project folder,
    maybe with uncommitted edits: it keeps working there."""
    cw = _cw()
    sid = env.new_chat()
    env.store.add_message(sid, "user", "first")
    assert cw.eligible(env.store.get_session(sid), "simple")        # its first message: yes
    env.store.add_message(sid, "assistant", "done")
    assert cw.eligible(env.store.get_session(sid), "simple") is None
