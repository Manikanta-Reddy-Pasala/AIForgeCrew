"""Branch targets: a folder per (repository, branch), so changes for two
branches of one repo — or a branch per repo — never meet in one checkout."""
from __future__ import annotations

import os
import subprocess

import pytest

from aiforge_core.runtime import branch_targets as bt
from aiforge_core.runtime import git_target as gt
from aiforge_core.runtime import repo_roles as rr

_REAL_USER_READS = None


@pytest.fixture(autouse=True)
def _reads_on_record(monkeypatch):
    """These tests set roles directly; the "user said it is a source" rule
    has its own tests."""
    from aiforge_core.runtime import repo_roles
    global _REAL_USER_READS
    _REAL_USER_READS = repo_roles.user_reads
    monkeypatch.setattr(repo_roles, "user_reads", lambda *a, **k: True)
    monkeypatch.setattr(repo_roles, "_user_history", lambda *a, **k: "")
    yield


SID = 21


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path, branches=()):
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    with open(os.path.join(path, "a.txt"), "w") as fh:
        fh.write("a\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    for b in branches:
        _git(path, "branch", b)
    return os.path.realpath(path)


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREE_PLACE", "repo")
    monkeypatch.setattr(rr, "_user_history", lambda sid, limit=40: "")
    gt._TOPS.clear()
    api = _repo(str(tmp_path / "api"), branches=("release/2",))
    web = _repo(str(tmp_path / "web"))
    return api, web


def _write(path):
    return "file_write", {"path": path, "content": "x"}


def test_ensure_reuses_a_checkout_or_makes_a_worktree(repos):
    api, _ = repos
    got = bt.ensure(api, "main")
    assert got == {"path": api, "made": False, "new_branch": False}
    got = bt.ensure(api, "release/2")
    assert got["made"] and not got["new_branch"]
    assert gt.branch(got["path"]) == "release/2"
    assert ".aiforge-worktrees" in got["path"]
    assert _git(api, "status", "--porcelain") == ""      # excluded from status
    got = bt.ensure(api, "feat/new")
    assert got["new_branch"] and gt.branch(got["path"]) == "feat/new"
    with pytest.raises(ValueError):
        bt.ensure(api, "bad..name")


def test_two_branches_of_one_repo_get_two_folders(repos):
    api, _ = repos
    said = "fix it on main and backport the fix to release/2"
    res = rr.record(SID, {"work_in": [
        {"path": api, "branch": "main", "for": "the fix"},
        {"path": api, "branch": "release/2", "for": "the backport"}]}, api, said=said)
    assert res["ok"], res
    t = rr.targets(SID)
    rel = next(f for f, m in t.items() if m["branch"] == "release/2")
    assert t[api]["branch"] == "main" and rel != api
    assert "release/2" in res["note"] and "the backport" in res["note"]
    # each change in its branch's folder
    assert rr.check(SID, *_write(os.path.join(api, "fix.py")), api) is None
    assert rr.check(SID, *_write(os.path.join(rel, "fix.py")), api) is None
    # commits in each folder land on that folder's branch
    assert gt.check(SID, f"git -C {rel} commit -m backport", api, "") is None
    assert gt.check(SID, f"git -C {api} commit -m fix", api, "") is None
    assert gt.pins(SID)[gt.toplevel(rel)] == "release/2"


def test_branch_not_named_by_user_is_not_recorded(repos):
    api, _ = repos
    res = rr.record(SID, {"work_in": [{"path": api, "branch": "hotfix/x"}]}, api,
                    said="fix the bug")
    assert not res["ok"] and "not named" in res["error"]
    assert rr.targets(SID) == {}
    assert not bt.exists(api, "hotfix/x")


def test_branch_named_earlier_in_the_chat_counts(repos, monkeypatch):
    api, _ = repos
    monkeypatch.setattr(rr, "_user_history", lambda sid, limit=40: "use branch hotfix/x")
    res = rr.record(SID, {"work_in": [{"path": api, "branch": "hotfix/x"}]}, api,
                    said="go")
    assert res["ok"] and bt.exists(api, "hotfix/x")


def test_two_repos_two_branches(repos):
    api, web = repos
    said = "api change on release/2, web change on feat/ui"
    res = rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"},
                                      {"path": web, "branch": "feat/ui"}]}, api, said=said)
    assert res["ok"], res
    t = rr.targets(SID)
    api_f = next(f for f, m in t.items() if m["repo"] == api)
    web_f = next(f for f, m in t.items() if m["repo"] == web)
    assert gt.branch(api_f) == "release/2" and gt.branch(web_f) == "feat/ui"
    # the repos' own checkouts (other branches) are not where this work goes
    res = rr.check(SID, *_write(os.path.join(api, "x.py")), api)
    assert res and res["reason"] == "not_a_work_repo"
    res = rr.check(SID, "run_command", {"cmd": f"git -C {web} commit -m x"}, api)
    assert res and res["reason"] == "not_a_work_repo"
    assert rr.check(SID, "run_command", {"cmd": f"git -C {web_f} commit -m x"}, api) is None
    note = rr.prompt_note(SID, api)
    assert "branch `release/2`" in note and "branch `feat/ui`" in note


def test_write_refused_when_a_branch_folder_moved_branch(repos):
    api, _ = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"}]}, api,
              said="work on release/2")
    f = next(iter(rr.targets(SID)))
    _git(f, "checkout", "-q", "-b", "other")
    res = rr.check(SID, *_write(os.path.join(f, "x.py")), api)
    assert res and res["reason"] == "wrong_branch" and "release/2" in res["error"]


def test_read_only_takes_no_branch(repos):
    api, web = repos
    res = rr.record(SID, {"work_in": [web], "read_only": [{"path": api, "branch": "release/2"}]},
                    web, said="port release/2 of api into web")
    assert "work_in" in (res.get("warning") or res.get("error") or "")
    assert rr.roles(SID)["read"] == [] and rr.targets(SID) == {}


def test_switching_to_a_branch_with_a_folder_points_there(repos):
    api, _ = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "main"},
                                {"path": api, "branch": "release/2"}]}, api,
              said="main and release/2")
    rel = next(f for f, m in rr.targets(SID).items() if m["branch"] == "release/2")
    _git(api, "commit", "-q", "--allow-empty", "-m", "fix on main")
    res = gt.check(SID, "git checkout release/2", api, "backport to release/2")
    assert res and res["reason"] == "branch_switch" and rel in res["error"]
    assert "cherry-pick " + _git(api, "rev-parse", "--short", "HEAD") in res["error"]


def test_write_in_another_checkout_names_the_branch_folders(repos, tmp_path):
    api, _ = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"}]}, api,
              said="release/2")
    rel = next(iter(rr.targets(SID)))
    res = rr.check(SID, *_write(os.path.join(api, "api.py")), api)
    assert res and "`release/2` → " + os.path.join(rel, "api.py") in res["error"]


def test_whole_tree_git_in_main_not_blocked_by_branch_folders(repos):
    api, web = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "main"},
                                {"path": api, "branch": "release/2"}],
                    "read_only": [web]}, api, said="main and release/2")
    for cmd in ("git stash", "git reset HEAD", "git checkout -- ."):
        assert rr.check(SID, "run_command", {"cmd": cmd}, api) is None, cmd


def test_re_record_keeps_made_flag(repos):
    api, _ = repos
    args = {"work_in": [{"path": api, "branch": "release/2", "for": "backport"}]}
    rr.record(SID, args, api, said="release/2")
    rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"}]}, api, said="release/2")
    meta = next(iter(rr.targets(SID).values()))
    assert meta["made"] and meta["for"] == "backport"


def test_forget_removes_clean_worktrees_only(repos):
    api, _ = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"},
                                {"path": api, "branch": "feat/a"}]}, api,
              said="release/2 and feat/a")
    t = rr.targets(SID)
    clean = next(f for f, m in t.items() if m["branch"] == "release/2")
    dirty = next(f for f, m in t.items() if m["branch"] == "feat/a")
    with open(os.path.join(dirty, "wip.txt"), "w") as fh:
        fh.write("wip")
    rr.forget(SID)
    assert not os.path.exists(clean)
    assert os.path.exists(os.path.join(dirty, "wip.txt"))
    assert bt.exists(api, "release/2")                     # the branch stays


# ── second live run ──────────────────────────────────────────────────────────

def test_roles_on_a_board_item_are_ignored(repos):
    from aiforge_core.runtime.chat_agent._turn._action import _pre_dispatch_gates

    class St:
        session_id = SID
        goal = "x"
        steers = []
        convo = []
        cwd = None
        user_roots = []
        board = {}
        board_used = board_touched = False
    api, web = repos
    st = St()
    st.cwd = api
    gen = _pre_dispatch_gates(st, "plan_progress", {"slug": "part-1", "title": "do it",
                                                    "read_only": [web]}, False, False)
    try:
        while True:
            next(gen)
    except StopIteration:
        pass
    assert rr.roles(SID) == {"work": [], "read": []}


def test_role_changes_need_the_user_both_ways(repos):
    api, web = repos
    said = f"change {api} and {web}"
    rr.record(SID, {"work_in": [api, web]}, api, said=said)
    res = rr.record(SID, {"read_only": [web]}, api, said=said)
    assert web in rr.roles(SID)["work"] and "NOT changed" in res["error"]
    assert rr.record(SID, {"read_only": [web]}, api, said="leave web alone")["ok"]
    assert rr.roles(SID)["read"] == [web]


def test_second_branch_in_a_worked_repo_gets_a_folder(repos):
    api, _ = repos
    with open(os.path.join(api, "a.txt"), "a") as fh:
        fh.write("fix\n")
    gt.after(SID, "file_write", {"path": os.path.join(api, "a.txt")}, {"ok": True}, api)
    _git(api, "commit", "-qam", "fix")
    res = gt.check(SID, "git checkout release/2", api, "backport it to release/2")
    assert res and res["reason"] == "branch_switch"
    folder = next(f for f, m in rr.targets(SID).items() if m["branch"] == "release/2")
    assert folder in res["error"] and gt.branch(folder) == "release/2"
    assert gt.branch(api) == "main"                         # the checkout did not move
    assert api in rr.roles(SID)["work"]                      # the main work stays allowed
    assert rr.check(SID, *_write(os.path.join(folder, "a.txt")), api) is None
    assert gt.check(SID, f"git -C {folder} cherry-pick HEAD", api, "") is None


def test_edit_then_new_branch_still_switches(repos):
    api, _ = repos
    with open(os.path.join(api, "a.txt"), "a") as fh:
        fh.write("wip\n")
    gt.after(SID, "file_write", {"path": os.path.join(api, "a.txt")}, {"ok": True}, api)
    assert gt.check(SID, "git checkout -b feat/x && git commit -am x", api,
                    "put it on a new branch feat/x") is None
    assert rr.targets(SID) == {}


def test_new_branch_after_committed_work_gets_a_folder(repos):
    api, _ = repos
    with open(os.path.join(api, "a.txt"), "a") as fh:
        fh.write("fix\n")
    gt.after(SID, "file_write", {"path": os.path.join(api, "a.txt")}, {"ok": True}, api)
    _git(api, "commit", "-qam", "fix")
    res = gt.check(SID, "git checkout -b hotfix/y", api, "also make hotfix/y")
    assert res and res["reason"] == "branch_switch"
    folder = next(f for f, m in rr.targets(SID).items() if m["branch"] == "hotfix/y")
    assert gt.branch(folder) == "hotfix/y" and gt.branch(api) == "main"


# ── UX review ────────────────────────────────────────────────────────────────

def test_switch_refusal_says_done_when_branch_has_the_commit(repos):
    api, _ = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "main"},
                                {"path": api, "branch": "release/2"}]}, api,
              said="main and release/2")
    rel = next(f for f, m in rr.targets(SID).items() if m["branch"] == "release/2")
    _git(api, "commit", "-q", "--allow-empty", "-m", "fix on main")
    res = gt.check(SID, "git checkout release/2", api, "")
    assert "cherry-pick" in res["error"] and "<sha>" not in res["error"]
    # a commit on release/2 the chat did not make is not "done"
    _git(rel, "commit", "-q", "--allow-empty", "-m", "version bump")
    assert "DONE" not in gt.check(SID, "git checkout release/2", api, "")["error"]
    with open(os.path.join(rel, "a.txt"), "a") as fh:
        fh.write("backport\n")
    _git(rel, "commit", "-qam", "backport")
    gt.after(SID, "run_command", {"cmd": f"git -C {rel} commit -am backport"},
             {"ok": True, "code": 0}, api)
    res = gt.check(SID, "git checkout release/2", api, "")
    assert "DONE" in res["error"] and "backport" in res["error"]


def test_facts_line_lists_every_repo_and_branch_committed(repos):
    from aiforge_core.runtime import turn_facts_line as facts
    api, web = repos
    for d in (api, web):
        with open(os.path.join(d, "a.txt"), "a") as fh:
            fh.write("x\n")
        _git(d, "commit", "-qam", "x")
        gt.after(SID, "run_command", {"cmd": f"git -C {d} commit -m x"},
                 {"ok": True, "code": 0}, api)
    line = facts._landed(SID)
    assert "`api@main`" in line and "`web@main`" in line
    assert facts._landed(SID) == ""                     # taken once


def test_repeated_refusal_is_flagged_then_paused(repos):
    from aiforge_core.runtime.chat_agent._turn._action import _git_target_gate
    api, web = repos
    rr.record(SID, {"work_in": [api], "read_only": [web]}, api)

    class St:
        session_id = SID
        goal = ""
        steers = []
        cwd = api
        user_roots = []

    st = St()
    st.convo = []
    outs = []
    for i in range(4):
        gen = _git_target_gate(st, "file_write",
                               {"path": os.path.join(web, "x.js"), "content": "x"}, api)
        evs = []
        try:
            while True:
                evs.append(next(gen))
        except StopIteration as stop:
            outs.append((stop.value, evs))
    assert outs[0][0] == "continue" and "repeated" not in outs[0][1][0]["result"]
    assert outs[1][1][0]["result"]["repeated"] == 2
    assert outs[3][0] in ("return", "continue") and len(outs[3][1]) > 1


def test_branch_named_in_an_earlier_turn_counts_for_git(repos, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn._action import _git_target_gate
    api, _ = repos
    monkeypatch.setattr(rr, "_user_history", lambda sid, limit=40: "work on release/2 later")
    with open(os.path.join(api, "a.txt"), "a") as fh:
        fh.write("y\n")
    gt.after(SID, "file_write", {"path": os.path.join(api, "a.txt")}, {"ok": True}, api)
    _git(api, "commit", "-qam", "y")

    class St:
        session_id = SID
        goal = "go on"
        steers = []
        cwd = api
        user_roots = []
    st = St()
    st.convo = []
    gen = _git_target_gate(st, "run_command", {"cmd": "git checkout release/2"}, api)
    evs = list(gen)
    assert evs and "own folder" in evs[0]["result"]["error"]



# ── correctness review ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_memory():
    gt._MINE.clear()
    gt._WORKED.clear()
    gt._LANDED.clear()
    yield


def test_auto_folder_starts_from_the_chats_work(repos):
    api, _ = repos
    with open(os.path.join(api, "a.txt"), "a") as fh:
        fh.write("chat work\n")
    gt.after(SID, "file_write", {"path": os.path.join(api, "a.txt")}, {"ok": True}, api)
    _git(api, "commit", "-qam", "chat work")
    head = _git(api, "rev-parse", "HEAD")
    gt.check(SID, "git checkout -b feat/z", api, "also branch feat/z")
    folder = next(f for f, m in rr.targets(SID).items() if m["branch"] == "feat/z")
    assert _git(folder, "rev-parse", "HEAD") == head


def test_pull_alone_is_not_work(repos):
    api, _ = repos
    gt.after(SID, "run_command", {"cmd": "git pull"}, {"ok": True, "code": 0}, api)
    assert gt.check(SID, "git checkout -b feat/q", api, "make feat/q") is None
    assert rr.targets(SID) == {}


def test_target_on_main_checkout_does_not_cover_nested_worktrees(repos):
    api, _ = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "main"},
                                {"path": api, "branch": "release/2"}]}, api,
              said="main and release/2")
    other = os.path.join(api, ".aiforge-worktrees", "chat-9-zz", "work")
    _git(api, "worktree", "add", "-q", "-b", "aiforge/chat-9", other)
    gt._TOPS.clear()
    res = rr.check(SID, *_write(os.path.join(other, "a.txt")), api)
    assert res and res["reason"] == "not_a_work_repo"
    assert rr.check(SID, *_write(os.path.join(api, "a.txt")), api) is None


def test_backport_tools_work_in_a_branch_folder_of_a_read_only_repo(repos, monkeypatch):
    api, _ = repos
    monkeypatch.setattr(rr, "user_restricts", lambda f, s: True)
    rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"}]}, api, said="release/2")
    rr.record(SID, {"read_only": [api]}, api, said="leave api alone")
    rel = next(iter(rr.targets(SID)))
    for cmd in (f"git -C {rel} apply x.patch", f"git -C {rel} stash", f"git -C {rel} am p"):
        assert rr.check(SID, "run_command", {"cmd": cmd}, api) is None, cmd


def test_forget_keeps_a_worktree_another_chat_records(repos):
    api, _ = repos
    rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"}]}, api, said="release/2")
    rr.record(SID + 1, {"work_in": [{"path": api, "branch": "release/2"}]}, api, said="release/2")
    folder = next(iter(rr.targets(SID)))
    rr.forget(SID)
    assert os.path.isdir(folder)


def test_slugs_do_not_collide(repos):
    api, _ = repos
    a = bt.ensure(api, "feat/a")["path"]
    b = bt.ensure(api, "feat-a")["path"]
    assert a != b and gt.branch(a) == "feat/a" and gt.branch(b) == "feat-a"


def test_restrict_needs_a_real_restriction():
    f = "/code/api"
    assert not rr.user_restricts(f, "keep the api changes small")
    assert not rr.user_restricts(f, "leave a comment in api")
    assert rr.user_restricts(f, "leave api alone")
    assert rr.user_restricts(f, "api is read-only")



# ── fourth live run ──────────────────────────────────────────────────────────

def test_read_only_needs_the_users_words(repos, monkeypatch):
    api, web = repos
    monkeypatch.setattr(rr, "user_reads", _REAL_USER_READS)
    res = rr.record(SID, {"work_in": [api], "read_only": [web]}, api,
                    said=f"add ping to {api} and pingLabel to {web}")
    assert web not in rr.roles(SID)["read"] and "NOT recorded as read-only" in res["warning"]
    rr.forget(SID)
    res = rr.record(SID, {"work_in": [web], "read_only": [api]}, api,
                    said=f"port the python code from {api} into {web}")
    assert rr.roles(SID)["read"] == [api]
    assert _REAL_USER_READS(api, "convert the code in this repo to js", api)
    assert not _REAL_USER_READS(api, "add a ping function to this repo", api)


def test_users_checkout_is_never_switched_when_the_chat_has_a_worktree(repos, monkeypatch):
    api, _ = repos
    monkeypatch.setattr(gt, "_users_checkout", lambda sid, top: os.path.realpath(top) == api)
    res = gt.check(SID, "git checkout release/2", api, "backport to release/2")
    assert res and res["reason"] == "branch_switch" and "own folder" in res["error"]
    assert gt.branch(api) == "main"
    folder = next(f for f, m in rr.targets(SID).items() if m["branch"] == "release/2")
    assert gt.branch(folder) == "release/2"



# ── final review ─────────────────────────────────────────────────────────────

def test_user_reads_cases():
    f = "/home/me/code/java-app"
    yes = ("Source: ~/code/java-app", "migrate /home/me/code/java-app to /y",
           "rewrite java-app in Rust in /y", "java-app is the old one, write /y",
           "port the code from ../java-app into /y")
    no = ("port from /x to java-app", "from now on edit java-app",
          "copy the output to java-app", "change the port in java-app",
          "update the docs in java-app")
    for t in yes:
        assert _REAL_USER_READS(f, t), t
    for t in no:
        assert not _REAL_USER_READS(f, t), t
    assert not rr.user_restricts("/c/y", "don't change /c/x, edit /c/y")


def test_read_only_subfolder_stays_read_only_in_branch_folders(repos):
    api, _ = repos
    os.makedirs(os.path.join(api, "docs"))
    open(os.path.join(api, "docs", "d.md"), "w").write("d")
    _git(api, "add", "-A")
    _git(api, "commit", "-qm", "docs")
    _git(api, "branch", "-f", "release/2")
    rr.record(SID, {"work_in": [{"path": api, "branch": "release/2"}],
                    "read_only": [api + "/docs"]}, api, said="release/2; docs are reference")
    rel = next(iter(rr.targets(SID)))
    res = rr.check(SID, *_write(os.path.join(rel, "docs", "d.md")), api)
    assert res and res["reason"] == "read_only"
    assert rr.check(SID, *_write(os.path.join(rel, "a.txt")), api) is None
    res = rr.check(SID, "run_command", {"cmd": f"git -C {rel} stash"}, api)
    assert res and res["reason"] == "read_only"


def test_new_branch_is_reported(repos):
    api, _ = repos
    res = rr.record(SID, {"work_in": [{"path": api, "branch": "feat/new"}]}, api,
                    said="make feat/new")
    assert "did not exist" in (res.get("warning") or "")


def test_old_head_is_not_reported_as_landed(repos):
    from aiforge_core.runtime import turn_facts_line as facts
    api, _ = repos
    _git(api, "commit", "-q", "--allow-empty", "-m", "old",
         "--date=2001-01-01T00:00:00")
    subprocess.run(["git", "commit", "-q", "--amend", "--no-edit", "--allow-empty"],
                   cwd=api, env={**os.environ, "GIT_COMMITTER_DATE": "2001-01-01T00:00:00"},
                   check=True)
    gt.after(SID, "run_command", {"cmd": "git commit -m x; git push"},
             {"ok": True, "code": 0}, api)
    assert facts._landed(SID) == ""
