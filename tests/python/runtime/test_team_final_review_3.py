"""Final-review findings, round 2: a refused commit must never lose work,
a parked run resumes only on a continue/answer, idioms are not prohibitions,
bold/backtick subtask forms, shell commands naming the real repo in a team
run, request-framed "apply", and the shared exclude with overlapping runs."""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import team_run_life as life
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot
from aiforge_core.runtime.parallel_subtasks._spec_align import spec_subtasks


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True)


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "money.py").write_text("def fmt(x):\n    return str(x)\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_money.py").write_text("def test_x():\n    pass\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return os.path.realpath(str(path))


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
    life._PARKED.clear()
    life._CONSENT.clear()
    life._EXCL.clear()


@pytest.fixture
def session_ws(tmp_path):
    ws = tmp_path / "chat-workspaces" / "session-1"
    ws.mkdir(parents=True)
    return str(ws)


def _branches(repo):
    return _git(repo, "branch", "--format=%(refname:short)").stdout.split()


# ─── 1: a refused commit never loses the run's work ────────────────────────


def test_a_rejecting_pre_commit_hook_is_bypassed_on_the_run_branch(tmp_path):
    repo = _repo(tmp_path / "r")
    hook = os.path.join(repo, ".git", "hooks", "pre-commit")
    os.makedirs(os.path.dirname(hook), exist_ok=True)
    with open(hook, "w") as fh:
        fh.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    ws = tw.open_run(repo, "fix")
    with open(os.path.join(ws.cwd, "money.py"), "a") as fh:
        fh.write("# fixed\n")
    notes = list(tw.close(ws))
    assert "# fixed" in _git(repo, "show", f"{ws.branch}:money.py").stdout
    assert ws.branch in _branches(repo) and "Committed 1" in notes[-1]["text"]


def test_signing_without_an_agent_is_retried_unsigned(tmp_path):
    repo = _repo(tmp_path / "r")
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "gpg.program", "false")
    ws = tw.open_run(repo, "fix")
    with open(os.path.join(ws.cwd, "money.py"), "a") as fh:
        fh.write("# fixed\n")
    assert tw.seal(ws.cwd) == ["money.py"]
    assert "# fixed" in _git(repo, "show", f"{ws.branch}:money.py").stdout
    list(tw.close(ws))


def test_when_nothing_can_commit_the_worktree_and_branch_are_kept(
        tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    ws = tw.open_run(repo, "fix")
    with open(os.path.join(ws.cwd, "money.py"), "a") as fh:
        fh.write("# precious\n")
    monkeypatch.setattr(tw, "_commit", lambda cwd, msg: False)
    with pytest.raises(tw.SealError):
        tw.seal(ws.cwd)
    notes = list(tw.close(ws))
    text = notes[-1]["text"]
    assert ws.cwd in text and ws.branch in text and "nothing was removed" in text
    assert "# precious" in open(os.path.join(ws.cwd, "money.py")).read()
    assert ws.branch in _branches(repo)
    # ... and the stale sweep leaves it alone too
    old = 0
    os.utime(ws.run_dir, (old, old))
    assert life.sweep_stale(0) == [] and os.path.isdir(ws.cwd)


# ─── 2: a parked run resumes only on a continue / an answer ────────────────


def _turn(monkeypatch, prompt, cwd, history, pipeline, sid=5):
    from aiforge_core.api.routes._chat import _stages
    from aiforge_core.runtime import chat_approve, chat_write_grants
    monkeypatch.setattr(chat_approve, "approvals_required", lambda s: False)
    monkeypatch.setattr(chat_write_grants, "granted", lambda s: [])
    monkeypatch.setattr(_stages, "_pipeline_route", pipeline)
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    hist = list(history) + [{"role": "user", "content": prompt}]
    list(_stages._dispatch_agent_route(rd, None, prompt, cwd, sid, hist,
                                       lambda t: t, {}, 0.0, False, "", rctx))
    return rctx, hist


def _asks(_pp, prompt, cwd, *a):
    a[-1]["done"] = True
    yield {"type": "message", "awaiting_input": True, "text": "Which format?"}


def _done(_pp, prompt, cwd, *a):
    a[-1]["done"] = True
    yield {"type": "message", "text": "ok"}


def test_a_new_request_after_a_question_starts_fresh(tmp_path, session_ws,
                                                     monkeypatch):
    repo = _repo(tmp_path / "proj")
    rctx, hist = _turn(monkeypatch, f"In {repo} fix money.py", session_ws, [],
                       _asks)
    first = rctx["team_ws"]
    assert first.parked
    rctx2, _ = _turn(monkeypatch, "actually, build a CLI instead", session_ws,
                     hist, _done)
    assert first.closed and rctx2["team_ws"] is not first


def test_continue_and_merge_applies_on_resume(tmp_path, session_ws,
                                              monkeypatch):
    repo = _repo(tmp_path / "proj")
    rctx, hist = _turn(monkeypatch, f"In {repo} fix money.py", session_ws, [],
                       _asks)
    first = rctx["team_ws"]
    assert not first.apply

    def _commits(_pp, prompt, cwd, *a):
        with open(os.path.join(cwd, "money.py"), "a") as fh:
            fh.write("# done\n")
        _git(cwd, "commit", "-qam", "done")
        a[-1]["done"] = True
        yield {"type": "message", "text": "ok"}
    rctx2, _ = _turn(monkeypatch, "continue and merge it into main",
                     session_ws, hist, _commits)
    assert rctx2["team_ws"] is first and first.apply
    assert "# done" in (tmp_path / "proj" / "money.py").read_text()


@pytest.mark.parametrize("text,reason,ok", [
    ("continue", "stopped", True), ("go on", "stopped", True),
    ("yes", "question", True), ("euros, two decimals", "question", True),
    ("actually, build X instead", "question", False),
    ("build a login page", "question", False),
    ("add a README section", "stopped", False),
])
def test_what_continues_a_parked_run(text, reason, ok):
    assert life.continues(text, reason) is ok


# ─── 3: idioms are not prohibitions ────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "don't forget to update the tests",
    "don't just change money.py, also add fmt.py",
    "don't hesitate to modify `money.py`",
])
def test_idioms_are_permissions_not_prohibitions(tmp_path, text):
    root = str(tmp_path)
    prot.register(root, **prot.rules_from_texts([text]))
    assert prot.from_texts([text]) == []
    assert not prot.is_protected(root, "tests/test_money.py")
    assert not prot.is_protected(root, "money.py")


def test_a_direct_prohibition_still_protects():
    assert prot.from_texts(["do not ever edit the tests"]) == [prot.TESTS]
    assert prot.from_texts(["Do not edit the tests."]) == [prot.TESTS]


# ─── 4: every common LLM subtask form parses ───────────────────────────────


@pytest.mark.parametrize("line,slug", [
    ("1. **src/app.py:** add login", "src/app.py"),
    ("1. **x.py**: add login", "x.py"),
    ("1. `x.py` — add login", "x.py"),
    ("1. `x.py:` add login", "x.py"),
    ("1. x.py - add login", "x.py"),
    ("1. x-y.py: add login", "x-y.py"),
    ("- **x.py —** add login", "x.py"),
])
def test_subtask_forms(line, slug):
    items = spec_subtasks("## Subtasks\n" + line)
    assert [(i["slug"], i["goal"]) for i in items] == [(slug, "add login")]


# ─── 5: shell commands in a team run stay in the worktree ──────────────────


def _jail(name, args, cwd):
    from aiforge_core.runtime.chat_agent._turn._action import _workspace_jail
    st = SimpleNamespace(session_id=None, user_roots=[], convo=[])
    gen = _workspace_jail(st, name, args, cwd)
    evs = []
    try:
        while True:
            evs.append(next(gen))
    except StopIteration as stop:
        return stop.value, evs


def test_a_subtask_shell_command_naming_the_real_repo_is_rewritten(
        tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_WORKSPACE_JAIL", raising=False)
    repo = _repo(tmp_path / "proj")
    ws = tw.open_run(repo, "fix")
    for cmd in (f"cd {repo} && echo x >> money.py",
                f"git -C {repo} commit -am x",
                f"sed -i '' s/a/b/ {repo}/money.py"):
        args = {"command": cmd}
        sig, _evs = _jail("bash", args, ws.cwd)
        assert sig is None and repo not in args["command"]
        assert ws.cwd in args["command"]
    list(tw.close(ws))


def test_a_shell_write_outside_the_worktree_is_refused_unattended(
        tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_WORKSPACE_JAIL", raising=False)
    repo = _repo(tmp_path / "proj")
    # (temp dirs never count as a shell write target — use one under ~)
    other = os.path.expanduser("~/aiforge-jail-probe-never-created")
    ws = tw.open_run(repo, "fix")
    sig, evs = _jail("bash", {"command": f"echo x > {other}/f.txt"}, ws.cwd)
    assert sig == "continue" and evs[-1]["result"]["error"] == "outside_workspace"
    list(tw.close(ws))


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive FS")
def test_repo_paths_match_case_insensitively_on_macos(tmp_path):
    from aiforge_core.runtime.team_target import localize_paths
    repo = _repo(tmp_path / "Proj")
    upper = repo[:-4] + "PROJ"
    assert life.fold(upper) == life.fold(repo)
    assert localize_paths(f"fix {upper}/money.py", repo) == "fix money.py"
    assert life.rewrite_repo_paths(f"cd {upper}", repo, "/wt") == "cd /wt"
    ws = tw.open_run(repo, "fix")
    assert life.jail_roots(ws.cwd, [upper]) == []
    list(tw.close(ws))


# ─── 6: "apply" needs request framing ──────────────────────────────────────


@pytest.mark.parametrize("text,ok", [
    ("Fix this error: fast-forward fails", False),
    ("Fast-forward fails on CI", False),
    ('the log says "merge it into main" failed', False),
    ("can you merge it into main?", True),
    ("I want you to commit it to main", True),
    ("fix x, then merge into main", True),
])
def test_apply_needs_request_framing(text, ok):
    assert tw.wants_apply([text]) is ok


# ─── 7: overlapping runs share one exclude block ───────────────────────────


def test_overlapping_runs_do_not_leak_exclude_lines(tmp_path):
    repo = _repo(tmp_path / "r")
    excl = os.path.join(repo, ".git", "info", "exclude")
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    with open(excl, "w") as fh:
        fh.write("mine.txt\n")
    a = tw.open_run(repo, "fix a")
    b = tw.open_run(repo, "fix b")
    list(tw.close(a))
    assert ".aiforge-baseline" in open(excl).read()       # b is still running
    list(tw.close(b))
    assert open(excl).read() == "mine.txt\n"
