"""The safety net under a team run's shell commands (runtime/team_repo_net).

A text check cannot catch every way a command reaches the user's real repo
(the reviewer's bypasses below). The net DETECTS a change to the checkout,
reports it, and pauses the run for the user — it never writes: an automatic
undo cannot tell the command's change from the user's own edit.
"""
from __future__ import annotations

import hashlib
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
    net._ALERTS.clear()


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


def _digest(repo):
    """Every byte of the checkout (files, .git refs/HEAD/index) — the net
    must never change any of it."""
    h = hashlib.sha1(usedforsecurity=False)
    for root, dirs, files in os.walk(repo):
        dirs.sort()
        rel_root = os.path.relpath(root, repo)
        if rel_root.startswith(".git") and rel_root not in (".git", ".git/refs", ".git/refs/heads"):
            continue
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, repo).encode())
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            else:
                with open(p, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


def _shell(cmd, cwd, name="run_command"):
    """Run ``cmd`` between begin and end; returns (result, note, digest
    right after the command, digest after the net)."""
    h = net.begin(cwd, name)
    assert h is not None
    p = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True,
                       text=True)
    repo = h["ws"].repo
    after_cmd = _digest(repo)
    result, note = net.end(h, {"ok": p.returncode == 0,
                               "output": p.stdout + p.stderr})
    return result, note, after_cmd, _digest(repo)


# ─── the reviewer's bypasses: detected, reported, nothing written ─────────


BYPASSES = [
    "env -C {repo} sh -c 'echo x >> money.py'",
    'cd "$(printf %s {repo})" && echo x >> money.py',
    "D={repo}; cd $D && echo new > made.txt",
    "cd -P {repo} && rm README.md",
    'echo "$(printf x >> {repo}/money.py)"',
    "python3 - <<'PY'\nopen('{repo}/made.py','w').write('1')\nPY",
    "find {repo} -name money.py | xargs perl -pi -e 's/str/repr/'",
    "cp -t {repo} {wt}/money.py",                 # GNU cp only
    "install -m 644 {wt}/money.py {repo}/money.py",
    "rsync --remove-source-files {repo}/README.md {wt}/r.md",
    "git -C {repo} commit -qam 'agent commit'",
    "cd {repo} && git checkout -q -b other",
    "mkdir -p {repo}/newdir/sub && echo 1 > {repo}/newdir/sub/f.txt",
]


@pytest.mark.parametrize("tmpl", BYPASSES)
def test_every_bypass_is_reported_and_nothing_is_written(run, tmpl):
    repo, ws = run
    before = _digest(repo)
    result, note, after_cmd, after_net = _shell(
        tmpl.format(repo=repo, wt=ws.cwd), ws.cwd)
    assert after_net == after_cmd                  # the net wrote nothing
    if after_cmd == before:
        assert result.get("error") != "changed_users_checkout"
        pytest.skip("the command had no effect on this platform")
    assert result["ok"] is False
    assert result["error"] == "changed_users_checkout"
    assert result["changed"] or result["ref_moved"]
    assert "Nothing was reverted" in note
    alert = net.alert_for(ws.cwd)
    assert alert and "Reply **continue**" in net.pause_text(ws, alert)


def test_the_users_uncommitted_edit_is_never_touched(run):
    repo, ws = run
    result, _note, after_cmd, after_net = _shell(
        f"cd $(echo {repo}) && echo data > made.txt", ws.cwd)
    assert "made.txt" in result["changed"] and after_net == after_cmd
    assert open(os.path.join(repo, "money.py")).read() == USER_EDIT
    assert open(os.path.join(repo, "made.txt")).read() == "data\n"


def test_a_commit_is_reported_as_a_ref_move_and_left_in_place(run):
    repo, ws = run
    result, _n, after_cmd, after_net = _shell(
        f"R={repo}; git -C $R commit -qam agent", ws.cwd)
    assert result["ref_moved"] and after_net == after_cmd
    assert "agent" in _git(repo, "log", "-1", "--format=%s").stdout


def test_nothing_changed_says_nothing(run):
    repo, ws = run
    result, note, _a, _b = _shell(f"cat {repo}/money.py; ls {repo}", ws.cwd)
    assert result["ok"] is True and note == ""
    assert net.alert_for(ws.cwd) is None


def test_outside_a_team_run_with_a_grant_or_off_the_net_is_off(
        run, monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_write_grants
    repo, ws = run
    assert net.begin(str(tmp_path), "run_command") is None
    assert net.begin(ws.cwd, "file_read") is None
    monkeypatch.setenv("AIFORGE_TEAM_REPO_NET", "off")
    assert net.begin(ws.cwd, "run_command") is None
    monkeypatch.delenv("AIFORGE_TEAM_REPO_NET")
    monkeypatch.setattr(chat_write_grants, "granted", lambda sid: [repo])
    assert net.begin(ws.cwd, "run_command", session_id=3) is None


def test_a_snapshot_failure_warns_and_the_command_still_runs(run):
    repo, ws = run
    lock = os.path.join(repo, ".git", "index.lock")
    open(lock, "w").close()
    try:
        h = net.begin(ws.cwd, "run_command")
        subprocess.run(["bash", "-c", f"echo ran > {ws.cwd}/ran.txt"])
        result, note = net.end(h, {"ok": True})
    finally:
        os.remove(lock)
    assert result == {"ok": True} and "not watched" in note
    assert os.path.exists(os.path.join(ws.cwd, "ran.txt"))
    assert net.alert_for(ws.cwd) is None


def test_a_git_failure_is_never_read_as_an_empty_checkout(run, monkeypatch):
    repo, ws = run
    real = subprocess.run

    def _flaky(argv, *a, **k):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"boom")
        return real(argv, *a, **k)
    h = net.begin(ws.cwd, "run_command")
    monkeypatch.setattr(net.subprocess, "run", _flaky)
    result, note = net.end(h, {"ok": True})
    assert result == {"ok": True} and "not watched" in note


def test_submodule_big_file_and_case_rename_only_reported(run, tmp_path,
                                                         monkeypatch):
    repo, ws = run
    for k, v in (("GIT_CONFIG_COUNT", "1"),
                 ("GIT_CONFIG_KEY_0", "protocol.file.allow"),
                 ("GIT_CONFIG_VALUE_0", "always")):
        monkeypatch.setenv(k, v)
    lib = tmp_path / "lib"
    lib.mkdir()
    _git(lib, "init", "-q")
    (lib / "l.txt").write_text("l\n")
    _git(lib, "add", "-A")
    _git(lib, "commit", "-qm", "l")
    _git(repo, "submodule", "add", "-q", str(lib), "lib")
    big = os.path.join(repo, "big.bin")
    with open(big, "wb") as fh:
        fh.write(b"\0" * (17 * 1024 * 1024))       # a dirty LFS-size file
    for cmd, must_see in ((f"echo x >> {repo}/lib/l.txt", True),
                          (f"printf y >> {big}", True),
                          # case-only: git (ignorecase) may not see it at all
                          (f"cd {repo} && mv README.md readme.md", False)):
        net._ALERTS.clear()
        result, _n, after_cmd, after_net = _shell(cmd, ws.cwd)
        assert after_net == after_cmd, cmd              # nothing written
        if must_see:
            assert result["error"] == "changed_users_checkout", cmd
    assert os.path.getsize(big) == 17 * 1024 * 1024 + 1
    assert open(os.path.join(repo, "lib", "l.txt")).read() == "l\nx\n"


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


def test_a_change_during_a_long_job_is_reported_not_reverted_not_killed(
        run, tmp_path):
    repo, ws = run
    h = net.begin(ws.cwd, "run_command")
    job = _job("sleep 30", ws.cwd, tmp_path)
    result, note = net.end(h, {"ok": True, "id": job.key, "running": True})
    assert result["ok"] is True and note == ""
    with open(os.path.join(repo, "money.py"), "a") as fh:
        fh.write("# the user, meanwhile\n")         # could be anyone
    try:
        note = net.poll(ws.cwd)                         # the next tool call
        assert "money.py" in note and "Nothing was reverted" in note
        assert job.alive()                              # not killed
        assert "# the user, meanwhile" in open(
            os.path.join(repo, "money.py")).read()
        assert net.alert_for(ws.cwd)["changed"] == ["money.py"]
    finally:
        job.proc.kill()


def test_a_pending_job_is_looked_at_when_the_run_closes(run, tmp_path):
    repo, ws = run
    h = net.begin(ws.cwd, "run_command")
    job = _job(f"sleep 1; echo x > {repo}/late.txt; sleep 30", ws.cwd,
               tmp_path)
    net.end(h, {"ok": True})
    deadline = time.time() + 10
    while not os.path.exists(os.path.join(repo, "late.txt")) \
            and time.time() < deadline:
        time.sleep(0.1)
    try:
        assert "late.txt" in net.settle(ws)
        assert os.path.exists(os.path.join(repo, "late.txt"))
        assert net._PENDING == {} and net._ALERTS == {}
    finally:
        job.proc.kill()


# ─── the run pauses and asks; "continue" goes on ───────────────────────────


def test_the_team_run_pauses_and_continue_resumes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from aiforge_core.api.routes._chat import _stages
    from aiforge_core.runtime import chat_approve, chat_write_grants
    repo = tmp_path / "proj2"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "money.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    repo = os.path.realpath(str(repo))
    sess = tmp_path / "chat-workspaces" / "session-1"
    sess.mkdir(parents=True)
    monkeypatch.setattr(chat_approve, "approvals_required", lambda s: False)
    monkeypatch.setattr(chat_write_grants, "granted", lambda s: [])
    after = []

    def _pipeline(_pp, prompt, cwd, *a):
        h = net.begin(cwd, "run_command")
        subprocess.run(["bash", "-c", f"cd $(echo {repo}) && echo y > x.txt"])
        res, note = net.end(h, {"ok": True})
        yield {"type": "tool", "name": "run_command", "result": res}
        yield {"type": "message", "role": "system", "text": note}
        after.append("kept going")              # must never be reached
        yield {"type": "message", "text": "built"}
        a[-1]["done"] = True

    def _turn(prompt, hist, pipe):
        monkeypatch.setattr(_stages, "_pipeline_route", pipe)
        rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
        rctx = {"done": False}
        h2 = list(hist) + [{"role": "user", "content": prompt}]
        evs = list(_stages._dispatch_agent_route(
            rd, None, prompt, str(sess), 5, h2, lambda t: t, {}, 0.0, False,
            "", rctx))
        return rctx, evs, h2
    rctx, evs, hist = _turn(f"In {repo} fix money.py", [], _pipeline)
    pause = [e for e in evs if e.get("awaiting_input")]
    assert pause and "Reply **continue**" in pause[-1]["text"]
    assert "x.txt" in pause[-1]["text"] and after == []
    ws = rctx["team_ws"]
    assert ws.parked and open(os.path.join(repo, "x.txt")).read() == "y\n"

    def _goes_on(_pp, prompt, cwd, *a):
        a[-1]["done"] = True
        yield {"type": "message", "text": "ok"}
    rctx2, _e, _h = _turn("continue", hist, _goes_on)
    assert rctx2["team_ws"] is ws and ws.closed


def test_the_chat_loop_stops_at_a_changed_checkout(run):
    from aiforge_core.runtime import chat_agent as ca
    repo, ws = run
    steps = ['ACTION: run_command\nARGS_JSON: {"cmd": "cd \\"$(printf %s '
             + repo + ')\\" && echo y > made.txt"}',
             'ACTION: run_command\nARGS_JSON: {"cmd": "echo second"}',
             "FINAL: done"]
    seq = list(steps)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "fix"}], cwd=ws.cwd,
        complete_fn=lambda _r, _c: seq.pop(0) if seq else "FINAL: x"))
    tools = [e for e in evs if e.get("type") == "tool"
             and e.get("name") == "run_command"]
    assert tools[0]["result"]["error"] == "changed_users_checkout"
    assert not any("second" in str(e.get("args")) for e in tools)
    assert os.path.exists(os.path.join(repo, "made.txt"))    # not reverted


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
    "sed -Ei s/a/b/ {r}/money.py", "sed -ni p {r}/money.py",
    "sed 's/a/b/w out.txt' {r}/money.py", "sed '1e ls' {r}/money.py",
    "awk '{{print > \"f\"}}' {r}/money.py",
    "awk '{{system(\"rm x\")}}' {r}/money.py",
    "awk '{{print | \"sh\"}}' {r}/money.py", "sort -uo {r}/a {r}/money.py",
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
