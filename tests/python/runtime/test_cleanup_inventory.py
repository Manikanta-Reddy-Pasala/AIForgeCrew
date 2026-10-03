"""The cleanup inventory: what a chat created or started and has not undone.

Every entry comes from a fact (a tool call that succeeded, a process that is
alive, a file git reports), never from what the model said. Each kind is
detected, checked against the machine, and resolved again when a later call
undoes it. The list is saved with the chat.

An undo is offered only for what a call PROVES the chat made: the list is read
by a model that may be told "clean up", so ``rm -rf`` on a directory that was
already there is not a cosmetic mistake.
"""
import os
import subprocess
import sys

import pytest

from aiforge_core.runtime import chat_store, cleanup_detect as D, cleanup_inventory as I


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    for k in ("AIFORGE_CHAT_ACTION_LOG", "AIFORGE_CHAT_CLEANUP_INVENTORY",
              "AIFORGE_SANDBOX_REQUIRED", "AIFORGE_DOCKER_SANDBOX"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(I, "_docker_ps", lambda: None)      # no real docker in tests
    I._DOCKER_CACHE.update(at=0.0, rows=None)
    chat_store.reset_backend_for_tests()
    yield tmp_path
    chat_store.reset_backend_for_tests()


def _cmd(cmd, ok=True, tool="run_command", **res):
    return {"type": "tool", "name": tool, "args": {"cmd": cmd},
            "result": {"ok": ok, "code": 0 if ok else 1, **res}}


PIP_RICH = "Successfully installed rich-13.7.0\n"


def _write(path, tool="file_write", created=False):
    return {"type": "tool", "name": tool, "args": {"path": path, "content": "x"},
            "result": {"ok": True, "path": path, **({"created": True} if created else {})}}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _session(cwd):
    return chat_store.create_session("t", cwd=str(cwd))["id"]


def _kinds(entries):
    return [e["kind"] for e in entries]


def _by(entries, kind):
    return [e for e in entries if e["kind"] == kind]


def _undo(found):
    return {e["what"]: e["undo"] for e in found.values()}


# ── packages: only what the installer says it newly installed ────────────

def test_packages_the_installer_says_it_installed():
    found = D.replay([
        _cmd("pip install requests==2.31 rich",
             stdout="Collecting requests\nSuccessfully installed requests-2.31.0 rich-13.7.0\n"),
        _cmd("npm install left-pad", stdout="\nadded 1 package in 2s\n"),
        _cmd("sudo apt-get install -y jq", stdout=(
            "The following NEW packages will be installed:\n  jq libjq1 libonig5\n"
            "0 upgraded, 3 newly installed\n")),
        _cmd("pip install nope", ok=False, stderr="ERROR: No matching distribution"),
    ], "/w")
    assert _undo(found) == {
        "pip package `requests==2.31`": "pip uninstall -y requests",
        "pip package `rich`": "pip uninstall -y rich",
        "npm package `left-pad`": "npm uninstall left-pad",
        "apt package `jq`": "sudo apt-get remove -y jq",
    }
    assert all(e["kind"] == "package" for e in found.values())


def test_a_package_that_was_already_there_is_not_listed():
    """``pip uninstall`` of something the chat did not install would break the
    user's environment; the installer's output is the proof."""
    found = D.replay([
        _cmd("pip install requests", stdout="Requirement already satisfied: requests in /x\n"),
        _cmd("pip install -U pip", stdout=(
            "Found existing installation: pip 23.0\nUninstalling pip-23.0:\n"
            "Successfully installed pip-24.0\n")),
        _cmd("pip install -q rich"),                      # quiet: nothing to go on
        _cmd("npm install left-pad", stdout="\nup to date in 1s\n"),
        _cmd("sudo apt-get install -y git",
             stdout="git is already the newest version (1:2.43.0).\n"),
        _cmd("npm install", stdout="\nup to date, audited 300 packages\n"),
    ], "/w")
    assert found == {}


def test_a_requirements_file_lists_what_was_newly_installed_not_the_file():
    found = D.replay([_cmd("pip install -r requirements.txt", stdout=(
        "Requirement already satisfied: requests in /x\n"
        "Successfully installed flask-3.0.0 itsdangerous-2.1.2\n"))], "/w")
    assert _undo(found) == {
        "pip package `flask` (from requirements.txt)": "pip uninstall -y flask",
        "pip package `itsdangerous` (from requirements.txt)": "pip uninstall -y itsdangerous"}
    assert not any("-r" in e["undo"] for e in found.values())


def test_an_uninstall_takes_the_package_off_the_list():
    done = "Successfully installed requests-2.31.0 rich-13.7.0\n"
    found = D.replay([_cmd("pip install requests rich", stdout=done),
                      _cmd("pip uninstall -y requests"),
                      _cmd("npm install left-pad", stdout="added 1 package"),
                      _cmd("npm uninstall left-pad")], "/w")
    assert list(_undo(found)) == ["pip package `rich`"]


def test_node_modules_is_listed_but_never_offered_for_deletion():
    ci = D.replay([_cmd("cd web && npm ci", stdout="added 300 packages in 9s")], "/w")
    (e,) = ci.values()
    assert e["where"] == "/w/web/node_modules" and e["undo"] == ""     # it may predate the chat
    plain = D.replay([_cmd("npm install", stdout="added 4 packages in 1s")], "/w")
    (e,) = plain.values()
    assert e["what"] == "node_modules (npm install added 4 packages)" and e["undo"] == ""


def test_several_npm_packages_at_once_get_no_uninstall():
    """``added 3 packages`` counts dependencies: with two names asked for, either
    may have been in package.json already."""
    found = D.replay([_cmd("npm install react axios", stdout="added 3 packages in 2s")], "/w")
    assert {e["what"]: e["undo"] for e in found.values()} == {
        "npm package `react`": "", "npm package `axios`": ""}
    assert all("only if it was not a dependency before" in e["hint"] for e in found.values())
    changed = D.replay([_cmd("npm install react",
                             stdout="added 1 package, changed 2 packages in 2s")], "/w")
    assert [e["undo"] for e in changed.values()] == [""]


def test_the_undo_names_the_interpreter_and_the_folder():
    found = D.replay([
        _cmd(".venv/bin/pip install rich", stdout=PIP_RICH),
        _cmd("cd svc && npm install left-pad", stdout="added 1 package"),
        _cmd("cd infra && git checkout -b ops/x"),
    ], "/w")
    assert _undo(found) == {
        "pip package `rich`": "/w/.venv/bin/pip uninstall -y rich",
        "npm package `left-pad`": "cd /w/svc && npm uninstall left-pad",
        "git branch `ops/x`": "cd /w/infra && git branch -D ops/x"}
    module = D.replay([_cmd("/opt/py/bin/python3.11 -m pip install rich", stdout=PIP_RICH)], "/w")
    assert list(_undo(module).values()) == ["/opt/py/bin/python3.11 -m pip uninstall -y rich"]


def test_pip_in_an_activated_or_unknown_environment_gets_no_uninstall():
    """The undo would run in another environment, where the package may have
    been installed long before the chat."""
    for step in (_cmd(". .venv/bin/activate && pip install rich", stdout=PIP_RICH),
                 _cmd("sudo pip install rich", stdout=PIP_RICH),
                 _cmd("pip install rich", tool="bash", stdout=PIP_RICH)):   # the persistent shell
        (e,) = D.replay([step], "/w").values()
        assert e["what"] == "pip package `rich`" and e["undo"] == ""
        assert "environment it went into" in e["hint"]


def test_only_a_plain_command_is_read(tmp_path):
    """Text in a heredoc or in quotes did not run; ``a || b`` and ``a; b``
    succeeding says nothing about ``a``; ``sh -c`` and ``docker exec`` run
    somewhere else."""
    t = tmp_path / "data"
    found = D.replay([
        _cmd(f"cat > setup.sh <<'EOF'\ngit checkout -b main\nmkdir {t}\nEOF"),
        _cmd(f"echo 'run: git checkout -b main && mkdir {t}'"),
        _cmd("git checkout -b feat || git checkout feat"),
        _cmd(f"mkdir {t}; ls"),
        _cmd(f"mkdir {t} || true"),
        _cmd(f"d=$(mktemp -d) && mkdir {t}"),
        _cmd(f"mkdir {t} &"),
        _cmd('docker exec app sh -c "cd /app && pip install rich"', stdout=PIP_RICH),
        _cmd('sh -c "pip install rich"', stdout=PIP_RICH),
        _cmd("ssh box 'docker run -d --name pg postgres'"),
        _cmd(f"(cd /x && mkdir {t})"),
    ], str(tmp_path / "repo"))
    assert found == {}


def test_a_filter_keeps_the_installers_proof_but_not_the_exit_status(tmp_path):
    t = tmp_path / "data"
    found = D.replay([
        _cmd("pip install rich 2>&1 | tail -5", stdout=PIP_RICH),   # pip's own words
        _cmd(f"mkdir {t} | cat"),                                   # the status is cat's
        _cmd("git checkout -b feat | cat"),
    ], str(tmp_path / "repo"))
    assert list(_undo(found)) == ["pip package `rich`"]


def test_a_clone_never_lists_its_source(tmp_path):
    src, dest = tmp_path / "origin.git", tmp_path / "copy"
    found = D.replay([_cmd(f"git clone --depth 1 {src}"),
                      _cmd(f"git clone --depth 1 -b main {src} {dest}")], str(tmp_path / "repo"))
    assert [e["path"] for e in found.values()] == [str(dest)]


# ── temp paths: rm only for what the command provably made ───────────────

def test_temp_paths_a_command_provably_made(tmp_path):
    scratch = tmp_path / "scratch"
    made = str(scratch / "tmp.Ab12")
    found = D.replay([
        _cmd("mktemp -d", stdout=made + "\n"),
        _cmd(f"mkdir {scratch}/build"),                 # no -p: fails if it exists
        _cmd(f"mkdir {tmp_path}/repo/build"),           # inside the chat's own folder
    ], str(tmp_path / "repo"))
    assert {e["path"]: e["undo"] for e in found.values()} == {
        made: f"rm -rf {made}", f"{scratch}/build": f"rm -rf {scratch}/build"}
    assert all(e["kind"] == "temp" and e["proven"] for e in found.values())


def test_a_path_that_may_have_been_there_gets_no_rm(tmp_path):
    """The shared-/tmp hazard: writing INTO a directory does not make it ours."""
    shared = tmp_path / "shared"
    found = D.replay([
        _cmd(f"mkdir -p {shared}/cache"),
        _cmd(f"echo hi > {shared}/out.log"),
        _cmd(f"touch {shared}/stamp"),
        _cmd(f"echo more >> {shared}/existing.log"),    # an append: not listed at all
        _cmd(f"cp build.zip {shared}/"),                # a copy INTO it: not listed
        _cmd(f"unzip a.zip -d {shared}/unzipped"),
        _cmd(f"mktemp -d; find {tmp_path} -maxdepth 1",
             stdout=f"{shared}/tmp.X1\n{shared}\n{tmp_path}/other\n"),
    ], str(tmp_path / "repo"))
    assert sorted(e["path"] for e in found.values()) == sorted(
        [f"{shared}/cache", f"{shared}/out.log", f"{shared}/stamp"])
    assert all(e["undo"] == "" and not e["proven"] for e in found.values())
    assert all("may predate this chat" in e["what"] for e in found.values())
    assert not any(" → " in I.line(e) for e in found.values())      # no undo offered


def test_removing_a_temp_dir_takes_it_and_what_is_inside_off(tmp_path):
    scratch = tmp_path / "scratch"
    found = D.replay([_cmd(f"mkdir {scratch}"), _cmd(f"touch {scratch}/b.txt"),
                      _cmd(f"rm -rf {scratch}")], str(tmp_path / "repo"))
    assert found == {}


# ── docker ───────────────────────────────────────────────────────────────

def test_docker_containers_and_compose_stacks():
    found = D.replay([
        _cmd("docker run -d --name pg -p 5432:5432 postgres:16"),
        _cmd("docker run -d redis:7", stdout="3f2a9c1d8e7b" + "0" * 52 + "\n"),
        _cmd("docker run --rm alpine echo hi"),        # ran and removed itself
        _cmd("docker run alpine echo hi"),             # no name, no id: cannot be named
        _cmd("docker compose -f dev.yml up -d",
             stderr=" Container shop-db-1  Created\n Container shop-db-1  Started\n"),
    ], "/w")
    assert _undo(found) == {
        "docker container `pg`": "docker rm -f pg",
        "docker container `3f2a9c1d8e7b`": "docker rm -f 3f2a9c1d8e7b",
        "docker compose stack `dev.yml`": "docker compose -f dev.yml down"}
    gone = D.replay([_cmd("docker run -d --name pg postgres:16"), _cmd("docker rm -f pg"),
                     _cmd("docker compose up -d", stderr="Container x  Created"),
                     _cmd("docker compose down")], "/w")
    assert gone == {}


def test_a_stack_that_was_already_up_is_not_ours_to_take_down():
    found = D.replay([_cmd("docker compose up -d", stderr=" Container shop-db-1  Running\n")],
                     "/w")
    assert found == {}
    # one service added to a stack whose db was already running: ``down`` would kill db
    part = D.replay([_cmd("docker compose up -d web", stderr=(
        " Container shop-db-1  Running\n Container shop-web-1  Created\n"
        " Container shop-web-1  Started\n"))], "/w")
    (e,) = part.values()
    assert e["undo"] == "" and e["hint"].endswith("docker compose stop web")
    # containers that existed and were only started again
    again = D.replay([_cmd("docker compose up -d", stderr=" Container shop-db-1  Started\n")],
                     "/w")
    (e,) = again.values()
    assert e["undo"] == "" and "may predate this chat" in e["hint"]


def test_containers_are_checked_against_docker(repo, monkeypatch):
    sid = _session(repo)
    steps = [_cmd("docker run -d --name pg postgres:16"),
             _cmd("docker run -d --name gone redis:7"),
             _cmd("docker compose up -d", stderr="Container shop-db-1  Started")]
    as_recorded = _by(I.collect(sid, steps, str(repo)), "docker")
    assert len(as_recorded) == 3 and not any(e["verified"] for e in as_recorded)
    monkeypatch.setattr(I, "_docker_ps", lambda: [
        ("pg", "aaa111", "running", "", ""),
        ("other", "gone99", "running", "", ""),        # an id that starts like a NAME
        ("shop-db-1", "bbb222", "exited", str(repo), "shop")])
    live = {e["what"]: e["state"] for e in _by(I.collect(sid, steps, str(repo)), "docker")}
    assert live == {"docker container `pg`": "running", "docker compose stack": "exited"}
    assert [e["what"] for e in I.running(sid)] == ["docker container `pg`"]


# ── git ──────────────────────────────────────────────────────────────────

def test_git_branches_worktrees_stashes_and_commits():
    found = D.replay([
        _cmd("git checkout -b feat/x"),
        _cmd("git checkout -B main"),                  # resets an existing branch
        _cmd("git switch -C develop"),
        _cmd("git worktree add ../wt -b side"),
        _cmd("git stash", stdout="Saved working directory and index state WIP on main: abc1 init"),
        _cmd("git stash", stdout="No local changes to save"),
        _cmd("git commit -m wip"),
    ], "/w/repo")
    undo = _undo(found)
    assert undo == {"git branch `feat/x`": "git branch -D feat/x",
                    "git worktree": "git worktree remove /w/wt",
                    "git branch `side`": "git branch -D side",
                    "git stash entry": "",             # worked out against the live list
                    "commits not pushed": ""}          # pushing is never a cleanup step
    assert not any("push" in u for u in undo.values())
    gone = D.replay([_cmd("git checkout -b feat/x"), _cmd("git branch -D feat/x"),
                     _cmd("git commit -m wip"), _cmd("git push origin feat/x")], "/w/repo")
    assert gone == {}


def test_a_command_checked_on_later_counts_when_it_finishes():
    """``pip install`` handed back still running installed nothing yet; the
    ``command_wait`` that sees it exit 0 is the fact."""
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "pip install torch"},
              "result": {"ok": True, "id": "bg-4", "running": True}}]
    assert D.replay(steps, "/w") == {}
    steps.append({"type": "tool", "name": "command_wait", "args": {"id": "bg-4"},
                  "result": {"ok": True, "id": "bg-4", "running": False, "code": 0,
                             "new_output": "Successfully installed torch-2.3.0\n"}})
    assert list(_undo(D.replay(steps, "/w"))) == ["pip package `torch`"]


# ── checked against the machine ──────────────────────────────────────────

def test_only_a_file_the_chat_created_can_be_removed(repo):
    sid = _session(repo)
    (repo / "new.py").write_text("y = 2\n")
    (repo / "local.yaml").write_text("mine: true\n")        # the user's, untracked
    (repo / "app.py").write_text("x = 3\n")
    steps = [_write("new.py", created=True), _write("local.yaml", "file_patch"),
             _write("app.py", "file_patch"), _write("ghost.py", created=True)]
    files = _by(I.collect(sid, steps, str(repo)), "file")
    assert {e["where"]: (e["what"], e["undo"]) for e in files} == {
        "new.py": ("new file (untracked)", "rm new.py"),
        "local.yaml": ("file written (untracked; it may predate this chat)", ""),
        "app.py": ("changed file (uncommitted)", "")}       # never ``git checkout --``
    lines = {e["where"]: I.line(e) for e in files}
    assert lines["app.py"] == ("changed file (uncommitted) — app.py "
                               "(review with: git diff -- app.py)")
    assert lines["local.yaml"].endswith("(no automatic undo: check it before removing)")
    assert I.leftover_line(files) == "Uncommitted: 3 files."
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work")
    assert _by(I.collect(sid, steps, str(repo)), "file") == []


def test_a_branch_that_no_longer_exists_is_dropped_and_commits_are_counted(repo):
    sid = _session(repo)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "app.py").write_text("x = 9\n")
    _git(repo, "commit", "-q", "-am", "wip")
    steps = [_cmd("git checkout -b feat/x"), _cmd("git checkout -b never-made"),
             _cmd("git commit -am wip")]
    git = {e["what"]: e for e in _by(I.collect(sid, steps, str(repo)), "git")}
    assert "git branch `feat/x`" in git and git["git branch `feat/x`"]["verified"]
    assert "git branch `never-made`" not in git
    commits = git["commits on `feat/x` (no upstream branch yet)"]
    assert commits["undo"] == "" and "push only when the user asks" in I.line(commits)


def test_a_stash_entry_names_its_own_stash(repo):
    """The stash stack is shared with the user and other chats: ``git stash
    pop`` with no ref takes whatever is on top."""
    sid = _session(repo)
    (repo / "app.py").write_text("users = 1\n")
    _git(repo, "stash", "push", "-m", "the user's own stash")
    (repo / "app.py").write_text("chat = 1\n")
    said = _git(repo, "stash").stdout
    (repo / "app.py").write_text("later = 1\n")
    _git(repo, "stash", "push", "-m", "another one on top")
    steps = [_cmd("git stash", stdout=said)]
    (e,) = _by(I.collect(sid, steps, str(repo)), "git")
    assert e["what"] == "git stash entry stash@{1}" and e["undo"] == "git stash pop 'stash@{1}'"
    _git(repo, "stash", "drop", "stash@{1}")                 # the user dropped the chat's
    assert _by(I.collect(sid, steps, str(repo)), "git") == []


def test_temp_paths_are_listed_only_while_they_exist(repo, tmp_path):
    sid = _session(repo)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    steps = [_cmd(f"mkdir {scratch}"), _cmd(f"mkdir {tmp_path}/never")]
    temps = _by(I.collect(sid, steps, str(repo)), "temp")
    assert [e["where"] for e in temps] == [str(scratch)]
    assert I.leftover_line(temps) == f"Temporary: {scratch}."
    scratch.rmdir()
    assert _by(I.collect(sid, steps, str(repo)), "temp") == []


# ── each step is replayed once ───────────────────────────────────────────

def test_an_entry_found_gone_does_not_come_back(repo):
    """The chat's branch was deleted. If the USER later makes a branch of the
    same name it is theirs: the old step is not read again."""
    sid = _session(repo)
    _git(repo, "branch", "feat/x")
    steps = [_cmd("git checkout -b feat/x")]
    assert [e["what"] for e in I.collect(sid, steps, str(repo))] == ["git branch `feat/x`"]
    _git(repo, "branch", "-D", "feat/x")
    assert I.collect(sid, steps, str(repo)) == []
    _git(repo, "branch", "feat/x")                           # the user's own, later
    assert I.collect(sid, steps, str(repo)) == []
    # a NEW step that makes it again is read
    steps.append(_cmd("git checkout -b feat/x"))
    assert [e["what"] for e in I.collect(sid, steps, str(repo))] == ["git branch `feat/x`"]


def test_old_steps_are_not_reread_under_a_new_folder(repo, tmp_path):
    """A chat can move (its own worktree, a ticket folder). ``npm uninstall`` in
    the new folder would hit a project the old command never touched."""
    sid = _session(repo)
    steps = [_cmd("npm install left-pad", stdout="added 1 package")]
    first = I.collect(sid, steps, str(repo))
    assert [e["undo"] for e in first] == ["npm uninstall left-pad"]
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (e,) = I.collect(sid, steps, str(moved))
    assert e["key"] == f"pkg:npm:{repo}:left-pad"            # still the folder it ran in


def test_a_rewind_replays_what_is_left(repo):
    sid = _session(repo)
    pip = _cmd("pip install rich", stdout=PIP_RICH)
    I.collect(sid, [_cmd("make"), pip, _cmd("make test")], str(repo))
    # the last two steps were deleted, then other work was done
    after = I.collect(sid, [_cmd("make"), _cmd("npm install left-pad", stdout="added 1 package")],
                      str(repo))
    assert sorted(e["what"] for e in after) == ["npm package `left-pad`", "pip package `rich`"]


# ── what is running ──────────────────────────────────────────────────────

@pytest.fixture
def sleeper():
    procs = []

    def start():
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
        procs.append(p)
        return p
    yield start
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _job(sid, proc, cmd, key, tmp_path):
    from aiforge_core.runtime import cmd_jobs
    log = tmp_path / f"{key}.log"
    log.write_text("")
    job = cmd_jobs.Job(key, proc, cmd, [str(log)], session_id=sid, explicit=True,
                       kill=proc.kill, pgid=proc.pid)
    return cmd_jobs._register(job)


def test_a_running_background_command_is_listed_with_its_id_and_port(repo, sleeper, tmp_path):
    from aiforge_core.runtime import cmd_jobs
    sid = _session(repo)
    proc = sleeper()
    job = _job(sid, proc, "npm run dev -- --port 5173", "bg-3", tmp_path)
    try:
        (e,) = _by(I.collect(sid, [], str(repo)), "job")
        assert e["what"] == "running: job bg-3 `npm run dev -- --port 5173`, port 5173"
        assert e["undo"] == 'command_kill {"id": "bg-3"}'
        assert I.leftover_line([e]) == ("Left running: job bg-3 "
                                        "(`npm run dev -- --port 5173`, port 5173).")
        assert I.running_job_ids(sid) == {"bg-3"}
        other = _session(repo)                      # another chat does not see it
        assert _by(I.collect(other, [], str(repo)), "job") == []
        proc.kill()
        proc.wait(timeout=5)
        assert _by(I.collect(sid, [], str(repo)), "job") == []
    finally:
        cmd_jobs._forget(job)


def test_the_inventory_survives_a_restart_of_the_process(repo, sleeper, tmp_path):
    """The job table is in memory and the messages may be gone (a rewind): what
    was saved with the chat is still there, and still checked against facts."""
    from aiforge_core.runtime import cmd_jobs
    sid = _session(repo)
    proc = sleeper()
    job = _job(sid, proc, "python -m http.server 8123", "bg-9", tmp_path)
    (repo / "new.py").write_text("y = 2\n")
    first = I.collect(sid, [_write("new.py", created=True), _cmd(
        "pip install rich", stdout="Successfully installed rich-13.7.0")], str(repo))
    assert sorted(_kinds(first)) == ["file", "job", "package"]
    assert chat_store.get_session_cleanup(sid)                 # saved with the chat

    cmd_jobs._forget(job)                       # the restart: memory is empty
    chat_store.reset_backend_for_tests()
    after = I.collect(sid, [], str(repo))       # no steps: only the saved list
    assert sorted(_kinds(after)) == ["file", "job", "package"]
    (j,) = _by(after, "job")
    assert "bg-9" in j["what"] and "port 8123" in j["what"]
    assert j["undo"].startswith(f"kill -TERM -- -{proc.pid}")   # command_kill cannot reach it
    assert _by(after, "file")[0]["undo"] == "rm new.py"
    assert _by(after, "package")[0]["undo"] == "pip uninstall -y rich"

    proc.kill()
    proc.wait(timeout=5)
    (repo / "new.py").unlink()
    assert _kinds(I.collect(sid, [], str(repo))) == ["package"]


def test_a_reused_pid_is_not_taken_for_the_saved_job(repo, sleeper):
    """After a reboot the saved pid belongs to something else; a ``kill`` aimed
    at it would hit the wrong process. The start time tells them apart."""
    sid = _session(repo)
    other = sleeper()                           # alive, but not the job that was saved
    saved = I._job_entry("bg-1", "npm run dev", pid=other.pid, pgid=other.pid,
                         reachable=False)
    assert saved["started"]
    I.save(sid, [{**saved, "started": "1"}])    # same pid, another process's start time
    assert I.collect(sid, [], str(repo)) == [] and I.running(sid) == []
    I.save(sid, [{**saved, "started": ""}])     # no start time on record: not trusted
    assert I.collect(sid, [], str(repo)) == []
    I.save(sid, [saved])                        # the real one is kept
    assert [e["job"] for e in I.collect(sid, [], str(repo))] == ["bg-1"]


# ── switches, failure, what is shown ─────────────────────────────────────

def test_off_switch_and_a_broken_store_give_an_empty_list(repo, monkeypatch):
    sid = _session(repo)
    (repo / "new.py").write_text("y\n")
    monkeypatch.setenv("AIFORGE_CHAT_CLEANUP_INVENTORY", "0")
    assert I.collect(sid, [_write("new.py")], str(repo)) == []
    monkeypatch.delenv("AIFORGE_CHAT_CLEANUP_INVENTORY")

    def boom(*_a, **_k):
        raise RuntimeError("db is gone")
    monkeypatch.setattr(chat_store, "get_session_cleanup", boom)
    monkeypatch.setattr(chat_store, "set_session_cleanup", boom)
    assert _kinds(I.collect(sid, [_write("new.py")], str(repo))) == ["file"]   # still built
    monkeypatch.setattr(I, "_verify", boom)
    assert I.collect(sid, [_write("new.py")], str(repo)) == []
    assert I.collect(None, [_write("new.py")], str(repo)) == []


def test_lines_are_bounded():
    entries = [{"kind": "package", "what": f"pip package `p{i}`", "where": "",
                "undo": f"pip uninstall -y p{i}"} for i in range(20)]
    out = I.lines(entries, 5)
    assert len(out) == 6 and out[0] == "pip package `p0` → pip uninstall -y p0"
    assert out[-1].startswith("(+15 more")
    long = I.lines([{"kind": "temp", "what": "w" * 400, "where": "", "undo": "u" * 400}], 5)
    assert len(long[0]) <= 260


def test_credentials_in_a_command_are_masked():
    assert D.redact("curl -H 'Authorization: Bearer abc.def.ghi' https://x") == \
        "curl -H 'Authorization: Bearer ***' https://x"
    assert D.redact("git clone https://bot:s3cr3t@github.com/a/b") == \
        "git clone https://bot:***@github.com/a/b"
    assert D.redact("API_KEY=sk-live-12345678 npm run dev") == "API_KEY=*** npm run dev"
    assert D.redact("curl -u admin:hunter2 https://x") == "curl -u admin:*** https://x"
    assert D.redact("docker run -u 1000:1000 img") == "docker run -u 1000:1000 img"
    e = I._job_entry("bg-2", "TOKEN=ghp_abcdefgh123 npm run dev", pid=None)
    assert "ghp_abcdefgh123" not in e["what"] and "ghp_abcdefgh123" not in e["cmd"]
