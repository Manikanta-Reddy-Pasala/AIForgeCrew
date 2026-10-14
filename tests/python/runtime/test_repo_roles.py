"""repo_roles: which repositories a chat works in and which it only reads,
recorded once and enforced on every write."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from aiforge_core.runtime import git_target as gt
from aiforge_core.runtime import repo_roles as rr

SID = 11


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path):
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
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
    monkeypatch.delenv("AIFORGE_CHAT_REPO_ROLES", raising=False)
    # tmp_path sits in /tmp, which shell write detection skips as scratch.
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "_is_temp", lambda p: False)
    gt._TOPS.clear()
    src = _repo(str(tmp_path / "java-src"))
    dst = _repo(str(tmp_path / "rust-dst"))
    other = _repo(str(tmp_path / "other"))
    return src, dst, other


def _write(path):
    return "file_write", {"path": path, "content": "x"}


def test_record_and_read(repos):
    src, dst, _ = repos
    res = rr.record(SID, {"slug": "repos", "work_in": [dst], "read_only": [src + "/a.txt"]},
                    dst)
    assert res["ok"]
    assert rr.roles(SID) == {"work": [dst], "read": [src]}   # a file → its repo
    assert "work in" in res["note"] and "read only" in res["note"]


def test_write_in_source_refused_destination_allowed(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, *_write(os.path.join(src, "Main.java")), dst)
    assert res and res["reason"] == "read_only" and dst in res["error"]
    assert rr.check(SID, *_write(os.path.join(dst, "main.rs")), dst) is None
    assert rr.check(SID, *_write("src/lib.rs"), dst) is None


def test_shell_and_git_writes_into_source_refused(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, "run_command", {"cmd": f"sed -i s/a/b/ {src}/a.txt"}, dst)
    assert res and res["reason"] == "read_only"
    res = rr.check(SID, "run_command", {"cmd": f"cd {src} && git commit -m x"}, dst)
    assert res and res["reason"] == "read_only"
    # reading the source is the point
    assert rr.check(SID, "run_command", {"cmd": f"cat {src}/a.txt && git -C {src} log -1"},
                    dst) is None
    assert rr.check(SID, "run_command", {"cmd": f"cp {src}/a.txt {dst}/a.txt"}, dst) is None


def test_unlisted_repo_refused_non_repo_folder_allowed(repos, tmp_path):
    src, dst, other = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, *_write(os.path.join(other, "x")), dst)
    assert res and res["reason"] == "not_a_work_repo"
    plain = tmp_path / "notes"
    plain.mkdir()
    assert rr.check(SID, *_write(str(plain / "n.md")), dst) is None


def test_roles_missing_with_two_repos_in_play(repos):
    src, dst, _ = repos
    res = rr.check(SID, *_write(os.path.join(dst, "x.rs")), dst, user_roots=[src])
    assert res and res["reason"] == "roles_missing" and "plan_progress" in res["error"]
    # one repository: nothing to confuse
    assert rr.check(SID, *_write(os.path.join(dst, "x.rs")), dst) is None


def test_changing_a_role_needs_the_user(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.record(SID, {"work_in": [src]}, dst, said="carry on")
    assert not res["ok"] and "NOT changed" in res["error"]
    assert rr.roles(SID)["read"] == [src]
    res = rr.record(SID, {"work_in": [src]}, dst, said="you may now edit the java-src repo too")
    assert res["ok"] and src in rr.roles(SID)["work"] and rr.roles(SID)["read"] == []


def test_both_roles_at_once_refused(repos):
    src, dst, _ = repos
    assert not rr.record(SID, {"work_in": [dst], "read_only": [dst]}, dst)["ok"]


def test_prompt_note(repos):
    src, dst, _ = repos
    assert "SEVERAL REPOSITORIES" in rr.prompt_note(SID, dst, [src])
    assert rr.prompt_note(SID, dst, []) == ""
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    note = rr.prompt_note(SID, dst)
    assert "WORK IN" in note and dst in note and "READ ONLY" in note and src in note


def test_off_switch_and_forget(repos, monkeypatch):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    monkeypatch.setenv("AIFORGE_CHAT_REPO_ROLES", "0")
    assert rr.check(SID, *_write(os.path.join(src, "x")), dst) is None
    monkeypatch.delenv("AIFORGE_CHAT_REPO_ROLES")
    rr.forget(SID)
    assert not rr.declared(SID)


def test_git_commit_in_work_repo_not_refused_by_pins(repos):
    """Edits made by a command the harness could not read leave no pin; a
    recorded work repository is still a valid place to commit."""
    src, dst, other = repos
    gt.after(SID, "file_write", {"path": os.path.join(other, "x")}, {"ok": True}, other)
    rr.record(SID, {"work_in": [dst, other]}, dst)
    assert gt.check(SID, f"git -C {dst} commit -m x", other, "") is None


# ── in the chat loop ─────────────────────────────────────────────────────────

class _St:
    def __init__(self, cwd, goal=""):
        self.session_id = SID
        self.goal = goal
        self.steers = []
        self.convo = []
        self.cwd = cwd
        self.user_roots = []
        self.board = {}
        self.board_used = False
        self.board_touched = False


def _drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def test_plan_progress_records_roles(repos):
    from aiforge_core.runtime.chat_agent._turn._action import _pre_dispatch_gates
    src, dst, _ = repos
    st = _St(dst, "port java-src to rust in rust-dst")
    events, sig = _drain(_pre_dispatch_gates(
        st, "plan_progress", {"slug": "repos", "work_in": [dst], "read_only": [src]},
        False, False))
    assert sig == "handled"
    assert any(e.get("role") == "system" and "📌" in e.get("text", "") for e in events)
    obs = json.loads(st.convo[-1]["content"][len("OBSERVATION: "):])
    assert obs["ok"] and obs["repos"]["read"] == [src]


def test_gate_refuses_write_in_source(repos):
    from aiforge_core.runtime.chat_agent._turn._action import _git_target_gate
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    st = _St(dst)
    events, sig = _drain(_git_target_gate(st, "file_write",
                                          {"path": os.path.join(src, "A.java"),
                                           "content": "x"}, dst))
    assert sig == "continue" and events[0]["result"]["blocked"] == "repo_roles"


# ── round-2 review findings ──────────────────────────────────────────────────

def test_a_bare_name_never_resolves_to_the_chat_repo(repos):
    src, dst, _ = repos
    # chat sits in dst; "java-src" is its sibling, not dst/java-src
    res = rr.record(SID, {"work_in": ["rust-dst"], "read_only": ["java-src"]}, dst)
    assert res["ok"] and rr.roles(SID) == {"work": [dst], "read": [src]}
    rr.forget(SID)
    res = rr.record(SID, {"read_only": ["no-such-thing"]}, dst)
    assert not res["ok"] and "FULL path" in res["error"]


def test_new_destination_folder_forces_roles_and_is_writable(repos, tmp_path):
    src, _, _ = repos
    new = str(tmp_path / "new-rs")          # does not exist yet
    res = rr.check(SID, *_write(os.path.join(src, "A.java")), src,
                   user_roots=[str(tmp_path)], said=f"port this to {new}")
    assert res and res["reason"] == "roles_missing"
    os.makedirs(new)
    rr.record(SID, {"work_in": [new], "read_only": [src]}, src)
    assert rr.check(SID, *_write(os.path.join(new, "main.rs")), src) is None
    assert rr.check(SID, *_write(os.path.join(src, "A.java")), src)["reason"] == "read_only"


def test_git_working_tree_writes_in_source_refused(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    for cmd in (f"git -C {src} clean -fdx", f"cd {src} && git stash",
                f"git -C {src} restore a.txt", f"git -C {src} rm a.txt"):
        res = rr.check(SID, "run_command", {"cmd": cmd}, dst)
        assert res and res["reason"] == "read_only", cmd
    for cmd in (f"git -C {src} log -3", f"git -C {src} branch -a", f"git -C {src} fetch"):
        assert rr.check(SID, "run_command", {"cmd": cmd}, dst) is None, cmd


def test_default_path_tools_count_as_writes_here(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, "rename_symbol", {"old": "a", "new": "b"}, src)
    assert res and res["reason"] == "read_only"


def test_roles_inside_one_repo_and_its_worktree(repos):
    _, dst, _ = repos
    os.makedirs(os.path.join(dst, "java"))
    os.makedirs(os.path.join(dst, "rust"))
    res = rr.record(SID, {"work_in": [dst + "/rust"], "read_only": [dst + "/java"]}, dst)
    assert res["ok"]
    assert rr.check(SID, *_write(os.path.join(dst, "rust", "m.rs")), dst) is None
    assert rr.check(SID, *_write(os.path.join(dst, "java", "M.java")), dst)["reason"] \
        == "read_only"
    # git in that repo is work (a work folder is inside it)
    assert rr.check(SID, "run_command", {"cmd": "git commit -m x"}, dst) is None
    # a chat worktree of the same repo maps onto the project's folders
    wt = os.path.join(dst, ".aiforge-worktrees", "chat-1-ab", "work")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "dirs", "--allow-empty")
    _git(dst, "worktree", "add", "-q", "-b", "aiforge/chat-1-ab", wt)
    os.makedirs(os.path.join(wt, "java"), exist_ok=True)
    gt._TOPS.clear()
    res = rr.check(SID, *_write(os.path.join(wt, "java", "M.java")), wt)
    assert res and res["reason"] == "read_only"
    assert rr.check(SID, *_write(os.path.join(wt, "rust", "m.rs")), wt) is None


def test_side_task_cannot_move_a_role(repos, monkeypatch):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    monkeypatch.setitem(rr._OWNERS, "99", SID)
    res = rr.record(99, {"work_in": [src]}, dst, said=f"edit {src}")
    assert not res["ok"]


def test_the_suggested_reply_unlocks_the_folder(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, *_write(os.path.join(src, "A.java")), dst)
    assert "allow edits in java-src" in res["error"]
    assert rr.record(SID, {"work_in": [src]}, dst, said="allow edits in java-src")["ok"]
    assert rr.check(SID, *_write(os.path.join(src, "A.java")), dst) is None


# ── round-3 review findings ──────────────────────────────────────────────────

def test_work_folder_inside_a_read_folder_needs_the_user(repos):
    src, dst, _ = repos
    os.makedirs(os.path.join(src, "sub"))
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.record(SID, {"work_in": [src + "/sub"]}, dst, said="carry on")
    assert not res["ok"]
    assert rr.record(SID, {"work_in": [src + "/sub"]}, dst,
                     said=f"you may edit {src}/sub")["ok"]


def test_pasted_files_do_not_force_roles(repos, tmp_path):
    _, dst, _ = repos
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.log").write_text("x")
    res = rr.check(SID, *_write(os.path.join(dst, "x.rs")), dst,
                   user_roots=[str(logs)], said=f"see {logs}/app.log and fix it")
    assert res is None


def test_split_repo_git_tree_writes(repos):
    _, dst, _ = repos
    os.makedirs(os.path.join(dst, "java"))
    os.makedirs(os.path.join(dst, "rust"))
    rr.record(SID, {"work_in": [dst], "read_only": [dst + "/java"]}, dst)
    for cmd in ("git checkout -- java/", "git rm java/X.java", "git clean -fdx",
                "git reset --hard", "git stash"):
        res = rr.check(SID, "run_command", {"cmd": cmd}, dst)
        assert res and res["reason"] == "read_only", cmd
    assert rr.check(SID, "run_command", {"cmd": "git checkout -- rust/"}, dst) is None
    assert rr.check(SID, "run_command", {"cmd": "git commit -m x"}, dst) is None
    rr.forget(SID)
    rr.record(SID, {"read_only": [dst], "work_in": [dst + "/rust"]}, dst)
    assert rr.check(SID, "run_command", {"cmd": "git commit -m x"}, dst) is None


def test_git_reads_are_really_reads():
    from aiforge_core.runtime.git_target import GitCall
    assert not rr._git_reads(GitCall("tag", ["-a", "v1", "-m", "x"], "/"))
    assert not rr._git_reads(GitCall("branch", ["-r", "-D", "x"], "/"))
    assert not rr._git_reads(GitCall("symbolic-ref", ["HEAD", "refs/heads/x"], "/"))
    assert rr._git_reads(GitCall("branch", ["-a"], "/"))
    assert rr._git_reads(GitCall("tag", ["-l"], "/"))


def test_unknown_repo_refused_once_roles_exist(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, "run_command", {"cmd": 'cd "$(pick)" && git commit -m x'}, dst)
    assert res and res["reason"] == "unknown_repo"
    res = rr.check(SID, "run_command", {"cmd": f"git --work-tree={src} commit -m x"}, dst)
    assert res and res["reason"] == "read_only"


def test_owner_cache_cleared_on_delete_all(monkeypatch):
    monkeypatch.setitem(rr._OWNERS, "2", 1)
    rr.forget(None)
    assert "2" not in rr._OWNERS


def test_python_cell_writing_into_source(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, "execute_ipython_cell",
                   {"code": f"open('{src}/a.txt', 'w').write('x')"}, dst)
    assert res and res["reason"] == "read_only"
    assert rr.check(SID, "execute_ipython_cell",
                    {"code": f"print(open('{src}/a.txt').read())"}, dst) is None


def test_ambiguous_name_is_an_error(repos, tmp_path):
    _, dst, _ = repos
    a = tmp_path / "x1" / "api"
    b = tmp_path / "x2" / "api"
    for d in (a, b):
        d.mkdir(parents=True)
        _git(str(d), "init", "-q")
    res = rr.record(SID, {"read_only": ["api"]}, dst, user_roots=[str(a), str(b)])
    assert not res["ok"] and "could mean" in res["error"]


# ── live-run findings ────────────────────────────────────────────────────────

def _run(cmd, cwd):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)


def test_net_catches_writes_the_precheck_cannot_read(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    for cmd in (f"cd {src} && python3 -c \"open('a.txt','w').write('x')\"",
                f"S={src}; cd $S && python3 -c \"open('a.txt','a').write('y')\"",
                f"find {src} -name a.txt | xargs sed -i s/a/b/",
                f"env -C {src} sh -c 'echo z >> a.txt'",
                f"cd {dst} && python3 -c \"open('../java-src/new.txt','w')\""):
        h = rr.net_begin(SID, "run_command")
        _run(cmd, dst)
        res, note = rr.net_end(h, {"ok": True, "code": 0})
        assert res["error"] == "changed_read_only" and res["stop"], cmd
        assert "READ-ONLY" in note and "allow edits in java-src" in note
        _git(src, "checkout", "-q", "--", ".")
        _run("rm -f new.txt", src)


def test_net_quiet_for_reads_and_work_folder_writes(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    for cmd in (f"cat {src}/a.txt", f"cp {src}/a.txt {dst}/b.txt",
                f"python3 -c \"print(open('{src}/a.txt').read())\" > {dst}/c.txt"):
        h = rr.net_begin(SID, "run_command")
        _run(cmd, dst)
        res, note = rr.net_end(h, {"ok": True, "code": 0})
        assert res == {"ok": True, "code": 0} and note == "", cmd


def test_net_split_repo_counts_only_the_read_subfolder(repos):
    _, dst, _ = repos
    for d in ("java", "rust"):
        os.makedirs(os.path.join(dst, d))
        open(os.path.join(dst, d, "f"), "w").write("1\n")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "dirs")
    rr.record(SID, {"work_in": [dst + "/rust"], "read_only": [dst + "/java"]}, dst)
    h = rr.net_begin(SID, "run_command")
    _run("echo 2 >> rust/f", dst)
    assert rr.net_end(h, {"ok": True})[1] == ""
    h = rr.net_begin(SID, "run_command")
    _run("echo 2 >> java/f", dst)
    assert rr.net_end(h, {"ok": True})[0]["error"] == "changed_read_only"


def test_precheck_still_refuses_definite_writes_but_not_inputs(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, "run_command", {"cmd": f"cd {src} && sed -i s/a/b/ a.txt"}, dst)
    assert res and res["reason"] == "read_only"
    for cmd in (f"python3 port.py --input {src} --out {dst}",
                f"java -cp {src}/target/classes Main", f"cd {src} && mvn -q test"):
        assert rr.check(SID, "run_command", {"cmd": cmd}, dst) is None, cmd


def test_allow_must_name_the_folder_in_the_same_clause():
    f = "/code/calc-java"
    assert rr.user_allows(f, "allow edits in calc-java")
    assert rr.user_allows(f, "ok, you may change /code/calc-java now")
    assert not rr.user_allows(f, "port Parser from calc-java to calc-rust; you can update Cargo.toml")
    assert not rr.user_allows(f, "In calc-java the parser handles ^; you may modify the tests")
    assert not rr.user_allows(f, "allow edits in /code/calc-java/docs")
    assert not rr.user_allows(f, "don't allow edits in calc-java")
    assert rr.user_allows(f, "no worries, allow edits in calc-java")


def test_user_allow_is_applied_by_the_harness(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    assert rr.allow_from_user(SID, "don't allow edits in java-src") == []
    assert rr.allow_from_user(SID, "carry on") == []
    assert rr.allow_from_user(SID, "allow edits in java-src") == [src]
    assert src in rr.roles(SID)["work"]
    assert rr.check(SID, *_write(os.path.join(src, "A.java")), dst) is None


def test_partial_record_applies_the_allowed_part(repos):
    src, dst, other = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.record(SID, {"work_in": [other], "read_only": [dst]}, dst, said="go on")
    assert res["changed"] and other in rr.roles(SID)["work"]
    assert dst in rr.roles(SID)["work"] and "NOT changed" in res["warning"]


def test_gate_applies_user_allow_once(repos):
    from aiforge_core.runtime.chat_agent._turn._action import _git_target_gate
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    st = _St(dst, "allow edits in java-src")
    events, sig = _drain(_git_target_gate(st, "file_write",
                                          {"path": os.path.join(src, "A.java"),
                                           "content": "x"}, dst))
    assert sig is None and any("📌" in e.get("text", "") for e in events)


def test_final_note_asks_the_user(repos):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst], "read_only": [src]}, dst)
    res = rr.check(SID, *_write(os.path.join(src, "A.java")), dst)
    assert rr.refused_folder(res) == src
    note = rr.final_note([src], SID, "Completed the Rust part.")
    assert "🔒" in note and "'allow edits in java-src'" in note
    assert rr.final_note([src], SID, "reply 'allow edits in java-src' to go on") == ""
    rr.allow_from_user(SID, "allow edits in java-src")
    assert rr.final_note([src], SID, "done") == ""


def test_clone_reads_its_source(repos, tmp_path):
    src, dst, _ = repos
    rr.record(SID, {"work_in": [dst, str(tmp_path / "copy")], "read_only": [src]}, dst)
    assert rr.check(SID, "run_command",
                    {"cmd": f"git clone {src} {tmp_path}/copy"}, src) is None
    res = rr.check(SID, "run_command", {"cmd": f"git clone {dst} {src}/inner"}, dst)
    assert res and res["reason"] == "read_only"
