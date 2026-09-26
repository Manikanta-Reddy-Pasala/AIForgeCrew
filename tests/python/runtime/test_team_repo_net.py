"""The safety net under a team run's shell commands (runtime/team_repo_net).

A text check cannot catch every way a command reaches the user's real repo
(the reviewer's bypasses below). Whatever a command changed there is put
back: the user's own uncommitted edit exactly, new files quarantined (never
deleted), HEAD reset after a commit — and a change the user made outside the
command's run window is left alone.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from aiforge_core.runtime import cmd_jobs
from aiforge_core.runtime import team_repo_net as net
from aiforge_core.runtime import team_run_life as life
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot

USER_EDIT = "def fmt(x):\n    return str(x)\n# the user's own edit\n"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    monkeypatch.setitem(life._SWEPT, "done", True)
    yield
    prot._REG.clear()
    tw._RUNS.clear()
    life._EXCL.clear()
    net._PENDING.clear()


@pytest.fixture
def run(tmp_path):
    """A user repo with an uncommitted edit and an untracked file, and a
    team run on it."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "money.py").write_text("def fmt(x):\n    return str(x)\n")
    (repo / "README.md").write_text("readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "money.py").write_text(USER_EDIT)
    (repo / "scratch.txt").write_text("mine\n")
    ws = tw.open_run(str(repo), "fix money.py")
    yield os.path.realpath(str(repo)), ws
    list(tw.close(ws))


def _state(repo):
    return (_git(repo, "status", "--porcelain").stdout,
            _git(repo, "rev-parse", "HEAD").stdout,
            open(os.path.join(repo, "money.py")).read(),
            open(os.path.join(repo, "scratch.txt")).read())


def _shell(cmd, cwd, name="run_command"):
    h = net.begin(cwd, name)
    assert h is not None
    p = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True,
                       text=True)
    return net.end(h, {"ok": p.returncode == 0, "output": p.stdout + p.stderr})


# ─── the reviewer's bypasses ───────────────────────────────────────────────


BYPASSES = [
    "env -C {repo} sh -c 'echo x >> money.py'",
    'cd "$(printf %s {repo})" && echo x >> money.py',
    "D={repo}; cd $D && echo new > made.txt",
    "cd -P {repo} && rm README.md",
    'echo "$(printf x >> {repo}/money.py)"',
    "python3 - <<'PY'\nopen('{repo}/made.py','w').write('1')\n"
    "open('{repo}/money.py','a').write('#x')\nPY",
    "find {repo} -name money.py | xargs perl -pi -e 's/str/repr/'",
    "cp -t {repo} {wt}/money.py",                 # GNU cp only
    "install -m 644 {wt}/money.py {repo}/money.py",
    "cp {wt}/money.py {repo}/README.md",
    "rsync --remove-source-files {repo}/README.md {wt}/r.md",
    "git -C {repo} commit -qam 'agent commit'",
    "cd {repo} && git add scratch.txt && git commit -qm x",
    "cd {repo} && git checkout -q -b other",
    "mkdir -p {repo}/newdir/sub && echo 1 > {repo}/newdir/sub/f.txt",
]


@pytest.mark.parametrize("tmpl", BYPASSES)
def test_every_bypass_is_put_back_exactly(run, tmpl):
    repo, ws = run
    before = _state(repo)
    result, note = _shell(tmpl.format(repo=repo, wt=ws.cwd), ws.cwd)
    if result.get("error") != "changed_users_checkout":
        # the flag is not supported on this platform (env -C, cp -t, …):
        # nothing changed, and nothing may have
        assert _state(repo) == before
        pytest.skip("command had no effect here")
    assert _state(repo) == before
    assert result["ok"] is False and ws.cwd in result["hint"]
    assert "put back" in note
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() in (
        "master", "main")


def test_a_new_file_is_quarantined_not_deleted(run):
    repo, ws = run
    result, note = _shell(f"cd $(echo {repo}) && echo data > made.txt", ws.cwd)
    assert not os.path.exists(os.path.join(repo, "made.txt"))
    q = [x for x in result["quarantined"] if x.startswith("made.txt")]
    dst = q[0].split(" → ")[1]
    assert open(dst).read() == "data\n" and "quarantine" in dst
    assert not dst.startswith(ws.run_dir)          # survives close()
    assert "moved to" in note


def test_head_is_reset_after_a_commit_and_the_commit_kept_in_the_reflog(run):
    repo, ws = run
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result, _ = _shell(f"R={repo}; git -C $R commit -qam agent", ws.cwd)
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert any("refs/heads" in r for r in result["reverted"])
    assert "agent" in _git(repo, "reflog", "--format=%gs").stdout
    assert open(os.path.join(repo, "money.py")).read() == USER_EDIT
    assert " M money.py" in _git(repo, "status", "--porcelain").stdout


def test_a_user_edit_outside_the_command_window_is_left_alone(run):
    repo, ws = run
    h = net.begin(ws.cwd, "run_command")
    path = os.path.join(repo, "README.md")
    with open(path, "w") as fh:
        fh.write("the user typed this meanwhile\n")
    old = time.time() - 120
    os.utime(path, (old, old))                  # not during the command
    h["t0"] = time.time()
    result, note = net.end(h, {"ok": True})
    assert result == {"ok": True}
    assert open(path).read() == "the user typed this meanwhile\n"
    assert "left alone" in note


def test_nothing_changed_costs_nothing_and_says_nothing(run):
    repo, ws = run
    result, note = _shell(f"cat {repo}/money.py; ls {repo}", ws.cwd)
    assert result["ok"] is True and note == ""


def test_outside_a_team_run_or_with_a_grant_the_net_is_off(run, monkeypatch,
                                                          tmp_path):
    from aiforge_core.runtime import chat_write_grants
    repo, ws = run
    assert net.begin(str(tmp_path), "run_command") is None
    assert net.begin(ws.cwd, "file_read") is None
    monkeypatch.setattr(chat_write_grants, "granted", lambda sid: [repo])
    assert net.begin(ws.cwd, "run_command", session_id=3) is None


# ─── a job the command left running ────────────────────────────────────────


def _job(cmd, cwd, tmp_path):
    out = tmp_path / f"job-{time.time_ns()}.log"
    fh = open(out, "w")
    proc = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, stdout=fh,
                            stderr=subprocess.STDOUT, start_new_session=True)
    job = cmd_jobs.Job(f"t-{proc.pid}", proc, cmd, [str(out)],
                       kill=lambda: proc.kill(), pgid=proc.pid)
    cmd_jobs._register(job)
    return job


def test_a_handed_off_job_that_writes_later_is_killed_and_undone(run,
                                                                 tmp_path):
    repo, ws = run
    before = _state(repo)
    h = net.begin(ws.cwd, "run_command")
    job = _job(f"sleep 1; echo late >> {repo}/money.py; "
               f"echo n > {repo}/late.txt; sleep 30", ws.cwd, tmp_path)
    result, note = net.end(h, {"ok": True, "id": job.key, "running": True})
    assert result["ok"] is True and note == ""         # nothing yet
    deadline = time.time() + 10
    while not os.path.exists(os.path.join(repo, "late.txt")) \
            and time.time() < deadline:
        time.sleep(0.1)
    note = net.poll(ws.cwd)                             # the next tool call
    assert "put back" in note
    assert not job.alive()
    assert _state(repo) == before


def test_a_job_still_pending_when_the_run_closes_is_settled(run, tmp_path):
    repo, ws = run
    before = _state(repo)
    h = net.begin(ws.cwd, "run_command")
    job = _job(f"sleep 1; echo x >> {repo}/money.py; touch {repo}/done.flag; "
               "sleep 30", ws.cwd, tmp_path)
    assert net.end(h, {"ok": True})[1] == ""
    deadline = time.time() + 10
    while not os.path.exists(os.path.join(repo, "done.flag")) \
            and time.time() < deadline:
        time.sleep(0.1)
    note = net.settle(ws)                    # what close() does first
    assert "put back" in note and not job.alive()
    assert _state(repo) == before
    assert net._PENDING == {}


# ─── the loop runs every shell command through the net ─────────────────────


def test_the_chat_loop_puts_the_repo_back(run):
    from aiforge_core.runtime import chat_agent as ca
    repo, ws = run
    before = _state(repo)
    steps = ['ACTION: run_command\nARGS_JSON: {"cmd": "cd \\"$(printf %s '
             + repo + ')\\" && echo x >> money.py && echo y > made.txt"}',
             "FINAL: done"]
    seq = list(steps)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "fix"}], cwd=ws.cwd,
        complete_fn=lambda _r, _c: seq.pop(0) if seq else "FINAL: x"))
    tools = [e for e in evs if e.get("type") == "tool"
             and e.get("name") == "run_command"]
    assert tools and tools[0]["result"]["error"] == "changed_users_checkout"
    assert any("put back" in str(e.get("text", "")) for e in evs)
    assert _state(repo) == before


# ─── the text pre-check stays lenient for readers ──────────────────────────


@pytest.mark.parametrize("tmpl", [
    "sed -n 1p {r}/money.py", "awk 1 {r}/money.py", "sort {r}/money.py",
    "uniq {r}/money.py", "nl {r}/money.py", "cut -c1-3 {r}/money.py",
    "tr a b < {r}/money.py", "od -c {r}/money.py", "strings {r}/money.py",
    "xxd {r}/money.py", "jq . {r}/a.json", "yq . {r}/a.yml",
    "find {r} -name '*.py' | xargs grep fmt", "column -t {r}/money.py",
])
def test_readers_pass_the_pre_check(run, tmpl):
    repo, ws = run
    assert life.repo_writes(ws.cwd, "run_command",
                            {"cmd": tmpl.format(r=repo)}) == []


@pytest.mark.parametrize("tmpl", [
    "sed -i s/a/b/ {r}/money.py", "sort -o {r}/money.py {r}/money.py",
    "yq -i . {r}/a.yml", "uniq {r}/money.py {r}/out.txt",
])
def test_in_place_forms_are_still_caught(run, tmpl):
    repo, ws = run
    assert life.repo_writes(ws.cwd, "run_command", {"cmd": tmpl.format(r=repo)})


# ─── a repo that holds the config dir is refused up front ─────────────────


def test_a_repo_holding_the_config_dir_is_refused(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from aiforge_core.api.routes._chat import _stages
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q")
    (home / "x.py").write_text("x = 1\n")
    _git(home, "add", "-A")
    _git(home, "commit", "-qm", "dotfiles")
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(home / ".aiforge"))
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    sess = tmp_path / "chat-workspaces" / "session-1"
    sess.mkdir(parents=True)
    from aiforge_core.runtime import team_target as tt
    monkeypatch.setattr(tt, "_broad", lambda p: False)
    evs = list(_stages._dispatch_agent_route(
        rd, None, f"In {home} fix x.py", str(sess), None,
        [{"role": "user", "content": f"In {home} fix x.py"}], lambda t: t, {},
        0.0, False, "", rctx))
    assert rctx["done"] and "config folder" in evs[-1]["text"]
    assert "team_ws" not in rctx
