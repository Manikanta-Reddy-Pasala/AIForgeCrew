"""When the repo net pauses a team run, the run really stops — no further
command runs, the agent thread winds down before the run is parked — and
"continue" re-baselines instead of pausing again on the same change."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import team_repo_net as net
from aiforge_core.runtime import team_run_life as life
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot

pytest.importorskip("google.adk")


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
    life._PARKED.clear()
    net._PENDING.clear()
    net._ALERTS.clear()
    net._HALTED.clear()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "money.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return os.path.realpath(str(r))


@pytest.fixture
def sess(tmp_path):
    s = tmp_path / "chat-workspaces" / "session-1"
    s.mkdir(parents=True)
    return str(s)


def _doer_run_shell():
    from aiforge_core.runtime import doer_tools
    return next(t for t in doer_tools.adk_function_tools()
                if (getattr(t, "name", None) or t.func.__name__) == "run_shell")


def _turn(monkeypatch, prompt, sess, hist, pipeline, sid=5):
    from aiforge_core.api.routes._chat import _stages
    from aiforge_core.runtime import chat_approve, chat_write_grants
    monkeypatch.setattr(chat_approve, "approvals_required", lambda s: False)
    monkeypatch.setattr(chat_write_grants, "granted", lambda s: [])
    monkeypatch.setattr(_stages, "_pipeline_route", pipeline)
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    h = list(hist) + [{"role": "user", "content": prompt}]
    evs = list(_stages._dispatch_agent_route(rd, None, prompt, sess, sid, h,
                                             lambda t: t, {}, 0.0, False, "",
                                             rctx))
    return rctx, evs, h


# ─── 1: the ADK-style agent thread stops at the pause ──────────────────────


def test_a_pause_stops_the_agent_thread_before_the_run_is_parked(
        repo, sess, monkeypatch):
    from aiforge_core.runtime import sandbox
    from aiforge_core.runtime.chat_pipeline import _repo_net_halted
    tool = _doer_run_shell()
    threads = []

    def _pipeline(_pp, prompt, cwd, session_id, *a):
        q: queue.Queue = queue.Queue()

        def _agent():                  # stands in for the ADK driver thread
            tok = sandbox.set_root_override(cwd)
            try:
                for i in range(40):
                    if _repo_net_halted(session_id):
                        break          # the driver's per-event stop check
                    cmd = f"echo {i} >> {cwd}/log.txt"
                    if i == 2:
                        cmd += f"; cd $(echo {repo}) && echo y > stray.txt"
                    r = tool.func(cmd=cmd)
                    q.put({"type": "tool", "name": "run_shell", "result": r})
                    time.sleep(0.05)
            finally:
                sandbox.reset_root_override(tok)
                q.put(None)
        t = threading.Thread(target=_agent, daemon=True)
        threads.append(t)
        t.start()
        while (ev := q.get()) is not None:
            yield ev
        a[-1]["done"] = True
    rctx, evs, _h = _turn(monkeypatch, f"In {repo} fix money.py", sess, [],
                          _pipeline)
    ws = rctx["team_ws"]
    assert not threads[0].is_alive()               # wound down before park
    assert ws.parked
    log = open(os.path.join(ws.cwd, "log.txt")).read().split()
    assert log == ["0", "1", "2"]                   # nothing ran after it
    assert os.path.exists(os.path.join(repo, "stray.txt"))   # not reverted
    pause = [e for e in evs if e.get("awaiting_input")]
    assert pause and "stray.txt" in pause[-1]["text"]
    assert net.alert_for(ws.cwd) is None          # the alert is cleared
    # the halt holds while parked: a driver still stuck in a model call
    # must find the run stopped when it returns
    assert net.halted(ws)


def test_a_halted_run_refuses_every_further_doer_call(repo, monkeypatch):
    from aiforge_core.runtime import doer_tools, sandbox
    ws = tw.open_run(repo, "fix")
    ws.session_id = 9
    tok = sandbox.set_root_override(ws.cwd)
    try:
        r = _doer_run_shell().func(cmd=f"cd $(echo {repo}) && echo y > s.txt")
        assert r["stop"] is True
        from aiforge_core.runtime.chat_pipeline import _repo_net_halted
        assert _repo_net_halted(9)
        for name in ("run_shell", "serve", "execute_ipython_cell"):
            t = next(t for t in doer_tools.adk_function_tools()
                     if (getattr(t, "name", None) or t.func.__name__) == name)
            assert t.func._team_net is True
        r2 = _doer_run_shell().func(cmd=f"echo later > {ws.cwd}/later.txt")
        assert r2["stop"] is True and "STOP" in r2["hint"]
        assert not os.path.exists(os.path.join(ws.cwd, "later.txt"))
    finally:
        sandbox.reset_root_override(tok)
        list(tw.close(ws))


# ─── 2: "continue" re-baselines ─────────────────────────────────────────────


def test_continue_does_not_repause_on_the_same_change_but_does_on_a_new_one(
        repo, sess, monkeypatch, tmp_path):
    from aiforge_core.runtime import cmd_jobs

    def _first(_pp, prompt, cwd, *a):
        h = net.begin(cwd, "run_command")
        out = open(tmp_path / "job.log", "w")
        proc = subprocess.Popen(
            ["bash", "-c", f"echo y > {repo}/stray.txt; sleep 30"], cwd=cwd,
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        cmd_jobs._register(cmd_jobs.Job(f"t-{proc.pid}", proc, "x",
                                        [str(tmp_path / "job.log")]))
        time.sleep(0.5)
        res, note = net.end(h, {"ok": True})
        yield {"type": "tool", "result": res}
        a[-1]["done"] = True
        proc.kill()
    rctx, evs, hist = _turn(monkeypatch, f"In {repo} fix money.py", sess, [],
                            _first)
    ws = rctx["team_ws"]
    assert ws.parked and any(e.get("awaiting_input") for e in evs)
    assert net._PENDING == {} and net.alert_for(ws.cwd) is None

    seen = {}

    def _second(_pp, prompt, cwd, *a):
        seen["poll"] = net.poll(cwd)
        h = net.begin(cwd, "run_command")
        subprocess.run(["bash", "-c", "echo fine > ok.txt"], cwd=cwd)
        seen["same"] = net.end(h, {"ok": True})
        yield {"type": "message", "text": "went on"}
        h = net.begin(cwd, "run_command")
        subprocess.run(["bash", "-c", f"echo z > {repo}/new.txt"], cwd=cwd)
        seen["new"] = net.end(h, {"ok": True})
        yield {"type": "tool", "result": seen["new"][0]}
        a[-1]["done"] = True
    rctx2, evs2, _ = _turn(monkeypatch, "continue", sess, hist, _second)
    assert rctx2["team_ws"] is ws
    assert seen["poll"] == "" and seen["same"] == ({"ok": True}, "")
    assert seen["new"][0]["changed"] == ["new.txt"]
    pause = [e for e in evs2 if e.get("awaiting_input")]
    assert pause and "new.txt" in pause[-1]["text"]
    assert "stray.txt" not in pause[-1]["text"]


# ─── 4: a snapshot error never blocks the command ─────────────────────────


def test_a_readlink_race_warns_and_the_command_runs(repo, monkeypatch):
    ws = tw.open_run(repo, "fix")
    os.symlink("money.py", os.path.join(repo, "link.py"))   # dirty symlink

    def _boom(p, *a, **k):
        raise OSError("vanished")
    monkeypatch.setattr(os, "readlink", _boom)
    try:
        h = net.begin(ws.cwd, "run_command")
        assert h["snap"] is None and "vanished" in h["warn"]
        subprocess.run(["bash", "-c", "echo ran > ran.txt"], cwd=ws.cwd)
        result, note = net.end(h, {"ok": True})
        assert result == {"ok": True} and "not watched" in note
        assert os.path.exists(os.path.join(ws.cwd, "ran.txt"))
    finally:
        monkeypatch.undo()
        list(tw.close(ws))


def test_begin_never_raises(monkeypatch, repo):
    ws = tw.open_run(repo, "fix")
    monkeypatch.setattr(net, "_ws_for", lambda *a: 1 / 0)
    try:
        assert net.begin(ws.cwd, "run_command") is None
        assert net.end(None, {"ok": 1}) == ({"ok": 1}, "")
    finally:
        monkeypatch.undo()
        list(tw.close(ws))


# ─── 5: hashing is capped; an untracked folder's contents are watched ─────


def test_hashing_budget_and_untracked_folders(repo, monkeypatch):
    monkeypatch.setattr(net, "_HASH_BUDGET", 10)
    with open(os.path.join(repo, "a.txt"), "w") as fh:
        fh.write("x" * 100)
    os.makedirs(os.path.join(repo, "newdir"))
    with open(os.path.join(repo, "newdir", "f.txt"), "w") as fh:
        fh.write("1")
    s1 = net.snapshot(repo)
    assert s1.marks["a.txt"].startswith("big:")
    assert s1.marks["newdir/"].startswith("dir:")
    with open(os.path.join(repo, "newdir", "f.txt"), "w") as fh:
        fh.write("22")
    changed, _moved = net.diff(s1, net.snapshot(repo))
    assert changed == ["newdir/"]


@pytest.mark.parametrize("cmd,writes", [
    ("awk '$1>5' {r}/money.py", False),
    ("awk '$2 >= 3 && $1 < 2' {r}/money.py", False),
    ("awk '{{print $1 | \"sort\"}}' {r}/money.py", True),
    ("awk '{{\"date\" | getline d}}' {r}/money.py", True),
    ("awk -f prog.awk {r}/money.py", True),
    ("sed -f prog.sed {r}/money.py", True),
])
def test_awk_comparisons_are_reads_script_files_are_not(repo, cmd, writes):
    from aiforge_core.runtime.team_repo_guard import touches
    assert bool(touches(cmd.format(r=repo), "/var/empty",
                        life.fold(repo))) is writes


def test_park_keeps_the_halt_and_resume_clears_it():
    ws = SimpleNamespace(cwd="/tmp/aiforge-test-run-x")
    net._HALTED.add(ws.cwd)
    net.reset(ws, keep_halt=True)
    assert ws.cwd in net._HALTED
    net.reset(ws)
    assert ws.cwd not in net._HALTED


def test_a_whole_delegation_is_not_one_net_window():
    from aiforge_core.runtime.doer_tools._net_wrap import SHELL_CAPABLE
    assert "delegate_to_agent" not in SHELL_CAPABLE
    assert "task" not in SHELL_CAPABLE
    assert "run_shell" in SHELL_CAPABLE
