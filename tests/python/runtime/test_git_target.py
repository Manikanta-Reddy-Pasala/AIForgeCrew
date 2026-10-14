"""git_target: a chat's git command is checked against the repository and the
branch its work is in, before it runs."""
from __future__ import annotations

import os
import subprocess

import pytest

from aiforge_core.runtime import git_target as gt

SID = 7


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path, branch="main"):
    os.makedirs(path)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    with open(os.path.join(path, "a.txt"), "w") as fh:
        fh.write("a\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return os.path.realpath(path)


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_CHAT_GIT_TARGET_GUARD", raising=False)
    gt._TOPS.clear()
    a = _repo(str(tmp_path / "alpha"))
    b = _repo(str(tmp_path / "beta"))
    return a, b


def _edit(repo, name="x.py"):
    """The chat wrote a file in ``repo`` with a file tool."""
    path = os.path.join(repo, name)
    with open(path, "w") as fh:
        fh.write("x\n")
    gt.after(SID, "file_write", {"path": path, "content": "x\n"}, {"ok": True}, repo)


# ── reading a command ────────────────────────────────────────────────────────

def test_calls_follow_cd_dash_c_and_bash_c(repos):
    a, b = repos
    got = gt.calls(f"cd {b} && git add x.py && git commit -m m", a)
    assert [(c.sub, c.folder) for c in got] == [("add", b), ("commit", b)]
    got = gt.calls(f"git -C {b} commit -m 'a; b'", a)
    assert [(c.sub, c.folder) for c in got] == [("commit", b)]
    got = gt.calls(f"bash -c 'cd {b} && git push'", a)
    assert [(c.sub, c.folder) for c in got] == [("push", b)]
    assert [c.folder for c in gt.calls("cd $NOPE_UNSET_X && git commit", a)] == [None]
    assert gt.calls("echo 'git commit'", a) == []


# ── wrong repository ─────────────────────────────────────────────────────────

def test_commit_in_chat_folder_refused_when_work_is_in_another_repo(repos):
    a, b = repos
    _edit(b)
    res = gt.check(SID, "git add x.py && git commit -m fix", a, "fix the bug")
    assert res and res["reason"] == "wrong_repo"
    assert b in res["error"] and "git -C" in res["error"]


def test_commit_in_the_repo_with_the_work_passes(repos):
    a, b = repos
    _edit(b)
    assert gt.check(SID, f"git -C {b} commit -m fix", a, "") is None
    assert gt.check(SID, f"cd {b} && git commit -m fix", a, "") is None
    assert gt.check(SID, "git commit -m fix", b, "") is None


def test_user_naming_the_repo_lets_it_through(repos):
    a, b = repos
    _edit(b)
    assert gt.check(SID, "git commit -m x", a, "also commit in alpha please") is None
    assert gt.check(SID, "git commit -m x", a, f"commit in {a}") is None


def test_nothing_recorded_means_nothing_refused(repos):
    a, _ = repos
    assert gt.check(SID, "git commit -m x", a, "") is None


def test_read_only_git_is_never_checked(repos):
    a, b = repos
    _edit(b)
    assert gt.check(SID, "git status && git log -1 && git diff", a, "") is None


def test_shell_write_records_the_repo(repos, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "_is_temp", lambda p: False)  # tmp_path is in /tmp
    a, b = repos
    gt.after(SID, "run_command", {"cmd": f"echo x > {b}/new.txt"}, {"ok": True, "code": 0}, a)
    assert gt.pins(SID) == {b: "main"}


# ── wrong branch ─────────────────────────────────────────────────────────────

def test_commit_refused_after_the_branch_moved(repos):
    a, _ = repos
    _edit(a)
    _git(a, "checkout", "-q", "-b", "other")
    res = gt.check(SID, "git commit -m x", a, "")
    assert res and res["reason"] == "wrong_branch"
    assert "`other`" in res["error"] and "`main`" in res["error"]
    assert gt.check(SID, "git commit -m x", a, "commit it on other") is None


def test_switch_to_existing_branch_refused_unless_named(repos):
    a, _ = repos
    _git(a, "branch", "develop")
    _edit(a)
    res = gt.check(SID, "git checkout develop && git commit -m x", a, "")
    assert res and res["reason"] == "branch_switch"
    assert gt.check(SID, "git switch develop", a, "move this to develop") is None


def test_new_branch_is_allowed_and_becomes_the_pin(repos):
    a, _ = repos
    _edit(a)
    cmd = "git checkout -b feat/y && git commit -m x"
    assert gt.check(SID, cmd, a, "") is None
    _git(a, "checkout", "-q", "-b", "feat/y")
    gt.after(SID, "run_command", {"cmd": cmd}, {"ok": True, "code": 0}, a)
    assert gt.pins(SID)[a] == "feat/y"
    assert gt.check(SID, "git commit -m more", a, "") is None


def test_file_checkout_is_not_a_branch_switch(repos):
    a, _ = repos
    _git(a, "branch", "develop")
    _edit(a)
    assert gt.check(SID, "git checkout -- a.txt", a, "") is None
    assert gt.check(SID, "git checkout HEAD a.txt", a, "") is None


# ── push ─────────────────────────────────────────────────────────────────────

def test_push_to_an_unnamed_branch_refused(repos):
    a, _ = repos
    _git(a, "checkout", "-q", "-b", "feat/z")
    _edit(a)
    res = gt.check(SID, "git push origin HEAD:main", a, "push it")
    assert res and res["reason"] == "push_target"
    assert gt.check(SID, "git push -u origin feat/z", a, "") is None
    assert gt.check(SID, "git push origin", a, "") is None
    assert gt.check(SID, "git push origin HEAD:main", a, "push to main") is None


# ── after a run ──────────────────────────────────────────────────────────────

def test_after_says_where_the_commit_ran(repos):
    a, b = repos
    line = gt.after(SID, "run_command", {"cmd": f"git -C {b} commit -m x"},
                    {"ok": True, "code": 0}, a)
    assert line.startswith("[git]") and b in line and "`main`" in line
    failed = gt.after(SID, "run_command", {"cmd": "git commit"},
                      {"ok": False, "code": 1}, a)
    assert "FAILED" in failed and "may not all have run" in failed and a in failed
    assert gt.after(SID, "run_command", {"cmd": "git commit"},
                    {"ok": False, "blocked": "git_target"}, a) == ""


def test_prompt_note_only_for_two_or_more_repos(repos):
    a, b = repos
    _edit(a)
    assert gt.prompt_note(SID) == ""
    _edit(b)
    note = gt.prompt_note(SID)
    assert a in note and b in note and "git -C" in note


def test_switches_and_forget(repos, monkeypatch):
    a, b = repos
    _edit(b)
    assert gt.check(None, "git commit", a, "") is None
    monkeypatch.setenv("AIFORGE_CHAT_GIT_TARGET_GUARD", "0")
    assert gt.check(SID, "git commit", a, "") is None
    monkeypatch.delenv("AIFORGE_CHAT_GIT_TARGET_GUARD")
    gt.forget(SID)
    assert gt.pins(SID) == {}


# ── the gate in the chat loop ────────────────────────────────────────────────

class _St:
    def __init__(self, goal=""):
        self.session_id = SID
        self.goal = goal
        self.steers = []
        self.convo = []


def _drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def test_gate_refuses_and_tells_the_model(repos, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn._action import _git_target_gate
    monkeypatch.setenv("AIFORGE_CHAT_REPO_ROLES", "0")   # this test: the git check alone
    a, b = repos
    _edit(b)
    st = _St("fix it")
    events, sig = _drain(_git_target_gate(st, "run_command", {"cmd": "git commit -m x"}, a))
    assert sig == "continue"
    assert events[0]["result"]["blocked"] == "git_target"
    assert "OBSERVATION" in st.convo[-1]["content"]
    events, sig = _drain(_git_target_gate(st, "run_command",
                                          {"cmd": f"git -C {b} commit -m x"}, a))
    assert sig is None and events == []


# ── review findings ──────────────────────────────────────────────────────────

def test_rebase_under_way_is_not_a_wrong_branch(repos):
    a, _ = repos
    _git(a, "checkout", "-q", "-b", "feat")
    _edit(a)
    _git(a, "add", "x.py")
    _git(a, "commit", "-q", "-m", "feat x")
    _git(a, "checkout", "-q", "main")
    with open(os.path.join(a, "x.py"), "w") as fh:
        fh.write("main\n")
    _git(a, "add", "x.py")
    _git(a, "commit", "-q", "-m", "main x")
    _git(a, "checkout", "-q", "feat")
    subprocess.run(["git", "rebase", "main"], cwd=a, capture_output=True)
    assert gt.busy(a) and gt.branch(a) == ""
    assert gt.check(SID, "git add x.py && git rebase --continue", a, "") is None
    assert gt.check(SID, "git rebase --abort", a, "") is None
    assert gt.check(SID, "git commit -m resolved", a, "") is None


def test_detached_checkout_never_becomes_the_pin(repos):
    a, _ = repos
    _edit(a)
    sha = _git(a, "rev-parse", "HEAD")
    assert gt.check(SID, f"git checkout {sha}", a, "") is None
    _git(a, "checkout", "-q", sha)
    gt.after(SID, "run_command", {"cmd": f"git checkout {sha}"}, {"ok": True, "code": 0}, a)
    assert gt.pins(SID)[a] == "main"
    assert gt.check(SID, "git checkout main", a, "") is None


def test_force_create_of_an_existing_branch_is_a_switch(repos):
    a, _ = repos
    _git(a, "checkout", "-q", "-b", "feat")
    _edit(a)
    res = gt.check(SID, "git checkout -B main && git commit -m x && git push origin main", a, "")
    assert res and res["reason"] == "branch_switch"


def test_dash_and_at_previous_are_switches(repos):
    a, _ = repos
    _git(a, "checkout", "-q", "-b", "feat")
    _edit(a)
    res = gt.check(SID, "git checkout -", a, "")
    assert res and res["reason"] == "branch_switch" and "main" in res["error"]
    assert gt.check(SID, "git switch @{-1}", a, "")["reason"] == "branch_switch"


def test_plain_word_needs_branch_context():
    assert not gt.branch_named("main", "the main issue is the login test")
    assert not gt.branch_named("test", "the main issue is the login test")
    assert gt.branch_named("main", "push to main")
    assert gt.branch_named("main", "merge it into the main branch")
    assert gt.branch_named("main", "use `main`")
    assert gt.branch_named("feat/x", "work in feat/x please")


def test_running_job_records_nothing(repos):
    a, _ = repos
    gt.after(SID, "run_command", {"cmd": "git rebase main"},
             {"ok": True, "running": True}, a)
    assert gt.pins(SID) == {}


def test_subshell_cd_does_not_leak(repos):
    a, b = repos
    got = gt.calls(f"(cd {b} && git commit -m x) && git push origin main", a)
    assert [(c.sub, c.folder) for c in got] == [("commit", b), ("push", a)]
    got = gt.calls(f"(cd {b} && git commit -m x); git push origin main", a)
    assert [(c.sub, c.folder) for c in got] == [("commit", b), ("push", a)]


def test_multiline_message_is_one_word(repos):
    a, _ = repos
    got = gt.calls('git commit -m "first line\ngit push origin main\n"', a)
    assert [c.sub for c in got] == ["commit"]
    got = gt.calls("git add x\ngit commit -m m  # git push origin main", a)
    assert [c.sub for c in got] == ["add", "commit"]


def test_tag_push_is_not_a_branch(repos):
    a, _ = repos
    _edit(a)
    _git(a, "tag", "v1.2.0")
    assert gt.check(SID, "git push origin v1.2.0", a, "") is None
    assert gt.check(SID, "git push origin tag v9", a, "") is None
    assert gt.check(SID, "git push --tags", a, "") is None


def test_signed_is_a_flag(repos):
    a, _ = repos
    _git(a, "checkout", "-q", "-b", "feat/q")
    _edit(a)
    res = gt.check(SID, "git push --signed origin main", a, "")
    assert res and res["reason"] == "push_target"


def test_same_command_edit_then_commit_in_new_repo(repos, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "_is_temp", lambda p: False)
    a, b = repos
    _edit(a)
    assert gt.check(SID, f"cd {b} && sed -i s/a/b/ a.txt && git commit -am x", a, "") is None


def test_absolute_cd_after_unknown_one(repos):
    a, b = repos
    got = gt.calls(f"cd \"$(mktemp -d)\" && cd {b} && git commit", a)
    assert got[-1].folder == b


# ── integration review findings ──────────────────────────────────────────────

def test_git_dash_c_into_the_wrong_repo_is_refused(repos):
    a, b = repos
    _edit(b)
    res = gt.check(SID, f"git -C {a} commit -m x", b, "")
    assert res and res["reason"] == "wrong_repo"
    gt.after(SID, "run_command", {"cmd": f"git -C {a} status"}, {"ok": True, "code": 0}, b)
    assert a not in gt.pins(SID)


def test_failed_chain_keeps_the_new_branch_as_pin(repos):
    a, _ = repos
    _edit(a)
    cmd = "git checkout -b feat && git commit -m x && git push -u origin feat"
    assert gt.check(SID, cmd, a, "") is None
    _git(a, "checkout", "-q", "-b", "feat")
    line = gt.after(SID, "run_command", {"cmd": cmd}, {"ok": False, "code": 128}, a)
    assert "failed" in line
    assert gt.pins(SID)[a] == "feat"
    assert gt.check(SID, "git push -u origin feat", a, "") is None


def test_scratch_folder_does_not_pin_the_project(repos):
    a, _ = repos
    scratch = os.path.join(a, ".aiforge-worktrees", "chat-1-ab", "scratch")
    os.makedirs(scratch)
    assert gt.is_scratch(os.path.join(scratch, "probe.py"))
    gt.after(SID, "file_write", {"path": os.path.join(scratch, "probe.py")}, {"ok": True}, a)
    assert gt.pins(SID) == {}


def test_a_worktree_is_not_named_by_the_word_work(tmp_path):
    assert not gt.repo_named("/x/proj/.aiforge-worktrees/chat-1/work", "keep up the work")
    assert gt.repo_named("/x/proj", "commit it in proj")


def test_side_task_preamble_is_not_the_user():
    from aiforge_core.runtime.chat_agent._turn._action import _said

    class St:
        goal = ("This is a SIDE TASK started from another chat ... assistant: pushed "
                "to main\n\nThe side task:\nwhich file is it on?")
        steers = []
    assert _said(St()) == ""      # the task text may be the parent agent's


# ── round-2 review findings ──────────────────────────────────────────────────

def test_ansi_c_quote_and_line_continuation(repos):
    a, _ = repos
    got = gt.calls("git commit -m $'don\\'t crash'\ngit push origin release", a)
    assert [c.sub for c in got] == ["commit", "push"]
    got = gt.calls("git push origin \\\nfeat-x", a)
    assert got[0].args == ["origin", "feat-x"]


def test_failed_force_create_keeps_pin(repos):
    a, _ = repos
    _edit(a)
    cmd = "git checkout -B feat && make nothing-here"
    _git(a, "checkout", "-q", "-b", "feat")
    gt.after(SID, "run_command", {"cmd": cmd}, {"ok": False, "code": 2}, a)
    assert gt.pins(SID)[a] == "feat"


def test_prose_does_not_name_a_branch():
    assert not gt.branch_named("main", "add a flag to main.py")
    assert not gt.branch_named("main", "build on main logic")
    assert not gt.branch_named("release-2", "bump to release-2.1")
    assert gt.branch_named("main", "push it to main, then tell me")
    assert gt.branch_named("main", "rebase main first")


def test_bash_lc_popd_herestring(repos):
    a, b = repos
    assert [(c.sub, c.folder) for c in gt.calls(f"bash -lc 'cd {b} && git commit -am x'", a)] \
        == [("commit", b)]
    got = gt.calls(f"pushd {b} && git commit -am x; popd; git push origin feat", a)
    assert [(c.sub, c.folder) for c in got] == [("commit", b), ("push", a)]
    got = gt.calls('git commit -F- <<< "fix"\ngit push origin x', a)
    assert [c.sub for c in got] == ["commit", "push"]
    assert gt.calls(f"git --git-dir {b}/.git commit -m x", a)[0].folder is None
    assert [c.sub for c in gt.calls("ls | xargs -I {} git -C {} push", a)] == ["push"]


def test_push_all_and_at(repos):
    a, _ = repos
    _edit(a)
    assert gt.check(SID, "git push --all origin", a, "push it")["reason"] == "push_target"
    assert gt.check(SID, "git push --all origin", a, "push all branches") is None
    assert gt.check(SID, "git push origin @", a, "") is None


def test_folder_named_is_strict():
    assert not gt.folder_named("/x/backend", "the backend is slow")
    assert gt.folder_named("/x/backend", "fix it in backend")
    assert gt.folder_named("/x/backend", "the backend repo")
    assert not gt.folder_named("/x/api", "see /x/api-gateway")
    assert gt.folder_named("/x/api", "see /x/api for it")
