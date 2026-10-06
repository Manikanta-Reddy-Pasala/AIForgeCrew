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
    # inside the project, in the folder git is told to ignore
    assert wt.startswith(os.path.join(str(env.repo), ".aiforge-worktrees") + os.sep)
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
    # one worktree per chat, any mode: team no longer opts out (it reuses the chat's)
    assert cw.eligible(sess, "team") == os.path.realpath(env.repo)
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


def test_the_worktree_can_live_in_the_config_folder(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREE_PLACE", "config")
    cw = _cw()
    wt = cw.ensure(env.new_chat())["path"]
    assert os.sep + "chat-worktrees" + os.sep in wt and not wt.startswith(str(env.repo))


def test_indexes_and_scratch_files_never_reach_the_branch(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    os.makedirs(os.path.join(wt, ".codegraph"))
    open(os.path.join(wt, ".codegraph", "graph.db"), "w").write("x")
    os.makedirs(os.path.join(wt, "graphify-out"))
    open(os.path.join(wt, "graphify-out", "GRAPH.md"), "w").write("x")
    scratch = cw.scratch_dir(wt)
    assert scratch and os.path.isdir(scratch) and not scratch.startswith(wt + os.sep)
    open(os.path.join(scratch, "probe.py"), "w").write("print(1)\n")
    open(os.path.join(wt, "src", "tax.py"), "w").write("def vat(x):\n    return x * 0.2\n")
    assert cw.seal_for_session(sid, "tax") == ["src/tax.py"]
    assert cw.info(env.store.get_session(sid))["uncommitted"] == []
    assert cw.scratch_dir(str(env.repo)) is None            # the user's folder has none
    # the scratch folder is writable for the agent, and the agent is told of it
    from aiforge_core.runtime.chat_agent._turn._state import _writable_roots
    assert scratch in _writable_roots([], sid, wt)
    assert scratch in cw.prompt_note(wt) and "never committed" in cw.prompt_note(wt)


def test_an_index_committed_by_an_earlier_turn_is_taken_out_again(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    os.makedirs(os.path.join(wt, ".codegraph"))
    open(os.path.join(wt, ".codegraph", "graph.db"), "w").write("x")
    _git(wt, "add", "-f", ".codegraph")
    _git(wt, "commit", "-q", "-m", "old turn")
    assert cw.seal_for_session(sid, "tidy") == [".codegraph/graph.db"]
    assert _git(wt, "ls-files", "--", ".codegraph").stdout.strip() == ""
    assert os.path.exists(os.path.join(wt, ".codegraph", "graph.db"))   # still on disk


def test_the_users_status_does_not_show_the_worktree_folder(env):
    cw = _cw()
    cw.ensure(env.new_chat())
    assert _git(env.repo, "status", "--porcelain").stdout == ""


def test_no_gitignore_or_baseline_commit_of_ours_lands_on_the_chat_branch(env):
    from aiforge_core.runtime.parallel_subtasks import _planning
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert not _planning._is_managed_workspace(wt)
    assert _planning._commit_turn_baseline(wt) == head
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head
    assert not os.path.exists(os.path.join(wt, ".gitignore"))
    assert _git(wt, "status", "--porcelain").stdout == ""


# ── a file the chat only ran is not part of its commit ───────────────────────

def _ran(cmd):
    return {"type": "tool", "name": "run_command", "args": {"command": cmd},
            "result": {"ok": True}}


def _helper_turn(env, cmd="python3 check_vat.py", name="check_vat.py"):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "src", "cart.py"), "a").write("def vat(x):\n    return x * 0.2\n")
    open(os.path.join(wt, name), "w").write("from src.tax import vat\nassert vat(10) == 2\n")
    return cw, sid, wt, [_ran(cmd)]


def test_a_script_the_turn_only_ran_stays_out_of_the_commit_and_in_the_worktree(env):
    cw, sid, wt, steps = _helper_turn(env)
    assert cw.seal_for_session(sid, "add vat", steps=steps, final_text="Added vat().") == ["src/cart.py"]
    assert os.path.isfile(os.path.join(wt, "check_vat.py"))          # not moved, not deleted
    assert "check_vat.py" not in _git(wt, "ls-files").stdout
    data = cw.info(env.store.get_session(sid))
    assert data["uncommitted"] == [] and data["held_out"] == ["check_vat.py"]
    # later seals (next turn, merge, delete) keep it out without being told again
    open(os.path.join(wt, "README.md"), "a").write("vat\n")
    assert cw.seal_for_session(sid) == ["README.md"]
    assert cw.merge(sid)["ok"] is True
    assert not os.path.exists(os.path.join(str(env.repo), "check_vat.py"))


@pytest.mark.parametrize("cmd", [
    "./check_vat.py", "FOO=1 timeout 30 python3 ./check_vat.py --fast",
    "python3 -m pytest check_vat.py::test_x -q", "bash -c true && python3 check_vat.py > out.txt",
])
def test_the_forms_a_run_takes(env, cmd):
    cw, sid, _wt, steps = _helper_turn(env, cmd)
    assert "check_vat.py" not in cw.seal_for_session(sid, "add vat", steps=steps)


def test_a_new_file_nobody_ran_is_committed(env):
    cw, sid, _wt, _steps = _helper_turn(env)
    assert sorted(cw.seal_for_session(sid, "add vat", steps=[_ran("ls")])) == ["check_vat.py", "src/cart.py"]


def test_a_script_the_user_or_the_answer_names_is_committed(env):
    cw, sid, _wt, steps = _helper_turn(env)
    assert "check_vat.py" in cw.seal_for_session(sid, "add vat and a check_vat.py script", steps=steps)
    cw, sid, _wt, steps = _helper_turn(env)
    assert "check_vat.py" in cw.seal_for_session(
        sid, "add vat", steps=steps, final_text="Added `check_vat.py` to run the checks.")


def test_the_facts_block_naming_the_file_does_not_count_as_the_answer_naming_it(env):
    from aiforge_core.runtime import turn_facts_line
    cw, sid, _wt, steps = _helper_turn(env)
    final = f"Done.\n\n---\n_{turn_facts_line.HEAD}:_ files changed in this turn: `check_vat.py`, `src/cart.py`."
    assert cw.seal_for_session(sid, "add vat", steps=steps, final_text=final) == ["src/cart.py"]


def test_a_script_another_file_refers_to_is_committed(env):
    cw, sid, wt, steps = _helper_turn(env)
    open(os.path.join(wt, "README.md"), "a").write("Run `python3 check_vat.py`.\n")
    assert "check_vat.py" in cw.seal_for_session(sid, "add vat", steps=steps)


def test_a_test_in_the_projects_own_test_folder_is_committed(env):
    os.makedirs(env.repo / "tests")
    (env.repo / "tests" / "test_cart.py").write_text("def test_a():\n    pass\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-q", "-m", "tests")
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "src", "cart.py"), "a").write("def vat(x):\n    return x * 0.2\n")
    open(os.path.join(wt, "tests", "test_tax.py"), "w").write("def test_v():\n    pass\n")
    names = cw.seal_for_session(sid, "add vat", steps=[_ran("pytest tests/test_tax.py -q")])
    assert sorted(names) == ["src/cart.py", "tests/test_tax.py"]


def test_a_turn_whose_only_product_is_the_script_commits_it(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "primes.py"), "w").write("print(2, 3, 5)\n")
    assert cw.seal_for_session(sid, "print some primes", steps=[_ran("python3 primes.py")]) == ["primes.py"]
    assert cw.info(env.store.get_session(sid))["held_out"] == []


def test_a_held_script_is_committed_once_something_refers_to_it_or_the_user_names_it(env):
    cw, sid, wt, steps = _helper_turn(env)
    cw.seal_for_session(sid, "add vat", steps=steps)
    assert cw.seal_for_session(sid, "commit check_vat.py too") == ["check_vat.py"]
    assert cw.info(env.store.get_session(sid))["held_out"] == []


def test_a_run_after_cd_or_from_another_folder_holds_nothing(env):
    cw, sid, _wt, _ = _helper_turn(env)
    steps = [_ran("cd src && python3 check_vat.py"),
             {"type": "tool", "name": "run_command",
              "args": {"command": "python3 check_vat.py", "cwd": "/tmp"}, "result": {}}]
    assert "check_vat.py" in cw.seal_for_session(sid, "add vat", steps=steps)


def test_the_switch_turns_it_off_and_a_team_worktree_is_sealed_whole(env, monkeypatch):
    cw, sid, _wt, steps = _helper_turn(env)
    monkeypatch.setenv("AIFORGE_CHAT_HOLD_HELPERS", "0")
    assert "check_vat.py" in cw.seal_for_session(sid, "add vat", steps=steps)


@pytest.mark.parametrize("cmd", [
    "python3 -m py_compile check_vat.py", "bash -n check_vat.py", "node --check check_vat.py",
    "npx eslint check_vat.py", "uv run ruff check check_vat.py", "python3 -c 'print(1)' check_vat.py",
    "cat check_vat.py", "python3 src/cart.py check_vat.py",
])
def test_a_file_that_was_only_checked_or_read_is_committed(env, cmd):
    cw, sid, _wt, steps = _helper_turn(env, cmd)
    assert "check_vat.py" in cw.seal_for_session(sid, "add vat", steps=steps)


def test_a_turn_that_made_only_new_files_commits_the_script_with_its_output(env):
    cw = _cw()
    sid = env.new_chat()
    wt = cw.ensure(sid)["path"]
    open(os.path.join(wt, "convert.py"), "w").write("open('out.json', 'w').write('[]')\n")
    open(os.path.join(wt, "out.json"), "w").write("[]")
    names = cw.seal_for_session(sid, "convert the data", steps=[_ran("python3 convert.py")])
    assert sorted(names) == ["convert.py", "out.json"]


def test_a_new_test_in_a_new_subfolder_is_committed(env):
    cw, sid, wt, _ = _helper_turn(env)
    os.makedirs(os.path.join(wt, "src", "billing"))
    open(os.path.join(wt, "src", "billing", "test_vat.py"), "w").write("def test_v():\n    pass\n")
    names = cw.seal_for_session(sid, "add vat", steps=[_ran("pytest src/billing/test_vat.py")])
    assert "src/billing/test_vat.py" in names


def test_a_run_file_placed_in_a_folder_is_committed(env):
    cw, sid, wt, _ = _helper_turn(env)
    os.remove(os.path.join(wt, "check_vat.py"))
    os.makedirs(os.path.join(wt, "scripts"))
    open(os.path.join(wt, "scripts", "probe.py"), "w").write("print(1)\n")
    names = cw.seal_for_session(sid, "add vat", steps=[_ran("python3 scripts/probe.py")])
    assert sorted(names) == ["scripts/probe.py", "src/cart.py"]


@pytest.mark.parametrize("prompt", [
    "add vat and a script to check it", "write a migration for vat and run it",
    "add vat, plus a small CLI tool",
])
def test_when_the_user_asked_for_something_to_run_nothing_is_held(env, prompt):
    cw, sid, _wt, steps = _helper_turn(env)
    assert "check_vat.py" in cw.seal_for_session(sid, prompt, steps=steps, final_text="Done.")


def test_a_top_level_test_is_committed_when_asked_for_or_when_the_project_keeps_them_there(env):
    cw, sid, _wt, steps = _helper_turn(env, "pytest test_vat.py", "test_vat.py")
    assert "test_vat.py" in cw.seal_for_session(sid, "add vat and a test for it", steps=steps)
    cw, sid, _wt, steps = _helper_turn(env, "pytest test_vat.py", "test_vat.py")
    assert "test_vat.py" not in cw.seal_for_session(sid, "add vat", steps=steps)
    (env.repo / "test_cart.py").write_text("def test_a():\n    pass\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-q", "-m", "flat tests")
    cw, sid, _wt, steps = _helper_turn(env, "pytest test_vat.py", "test_vat.py")
    assert "test_vat.py" in cw.seal_for_session(sid, "add vat", steps=steps)


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "denied by the user"}, {"ok": True, "denied": True}, None,
])
def test_a_command_that_was_refused_or_never_started_is_not_a_run(env, result):
    cw, sid, _wt, _ = _helper_turn(env)
    step = {"type": "tool", "name": "run_command",
            "args": {"command": "python3 check_vat.py"}, "result": result}
    assert "check_vat.py" in cw.seal_for_session(sid, "add vat", steps=[step])


def test_a_check_that_ran_and_failed_is_still_a_run(env):
    cw, sid, _wt, _ = _helper_turn(env)
    step = {"type": "tool", "name": "run_command", "args": {"command": "python3 check_vat.py"},
            "result": {"ok": False, "exit_code": 1, "output": "AssertionError"}}
    assert "check_vat.py" not in cw.seal_for_session(sid, "add vat", steps=[step])


@pytest.mark.parametrize("step", [
    {"type": "tool", "name": "file_write", "args": {"path": "./check_vat.py", "content": "x"},
     "result": {"ok": True}},
    {"type": "tool", "name": "run_command", "args": {"command": "echo 'print(2)' >> check_vat.py"},
     "result": {"ok": True}},
])
def test_a_held_file_written_again_by_any_route_is_committed(env, step, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "_is_temp", lambda p: False)   # the test repo is under /tmp
    cw, sid, wt, steps = _helper_turn(env)
    cw.seal_for_session(sid, "add vat", steps=steps)
    open(os.path.join(wt, "check_vat.py"), "a").write("print('ok')\n")
    assert cw.seal_for_session(sid, "improve that check", steps=[step]) == ["check_vat.py"]


def test_a_held_file_another_writer_committed_is_no_longer_listed(env):
    cw, sid, wt, steps = _helper_turn(env)
    cw.seal_for_session(sid, "add vat", steps=steps)
    _git(wt, "add", "check_vat.py")
    _git(wt, "commit", "-q", "-m", "by a team turn")
    assert cw.info(env.store.get_session(sid))["held_out"] == []


def test_a_held_file_the_agent_writes_again_is_committed(env):
    cw, sid, wt, steps = _helper_turn(env)
    cw.seal_for_session(sid, "add vat", steps=steps)
    open(os.path.join(wt, "check_vat.py"), "a").write("print('ok')\n")
    wrote = {"type": "tool", "name": "file_write",
             "args": {"path": "check_vat.py", "content": "x"}, "result": {"ok": True}}
    assert cw.seal_for_session(sid, "improve that check", steps=[wrote]) == ["check_vat.py"]


def test_deleting_the_chat_commits_what_was_held(env):
    cw, sid, _wt, steps = _helper_turn(env)
    cw.seal_for_session(sid, "add vat", steps=steps)
    branch = cw.remove(sid)["branch"]
    assert "check_vat.py" in _git(env.repo, "ls-tree", "-r", "--name-only", branch).stdout


def test_git_not_answering_holds_nothing(env, monkeypatch):
    from aiforge_core.runtime import chat_run_only
    cw, sid, _wt, steps = _helper_turn(env)
    real = chat_run_only._git

    def flaky(args, cwd, timeout=30, ok=(0,)):
        if args[0] == "grep":
            raise chat_run_only._GitFailed("timeout")
        return real(args, cwd, timeout, ok)
    monkeypatch.setattr(chat_run_only, "_git", flaky)
    assert "check_vat.py" in cw.seal_for_session(sid, "add vat", steps=steps)
