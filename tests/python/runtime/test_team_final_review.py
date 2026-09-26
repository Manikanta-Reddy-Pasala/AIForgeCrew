"""Final-review findings on team runs in the user's own repo.

Each test reproduces one finding (several exactly as the reviewer ran them):
subtask slugs with a hyphen, repo paths vs. the run worktree, an over-eager
"apply to my branch", a cached "not a repo", a consent that became a write
grant, cleanup on failure, resumable runs, over-broad read-only rules and a
truncated SPEC review.
"""
from __future__ import annotations

import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import team_run_life as life
from aiforge_core.runtime import team_target as tt
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot
from aiforge_core.runtime.parallel_subtasks._spec_align import (
    align_to_spec,
    spec_subtasks,
)


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


@pytest.fixture
def session_ws(tmp_path):
    ws = tmp_path / "chat-workspaces" / "session-1"
    ws.mkdir(parents=True)
    return str(ws)


def _branches(repo):
    return _git(repo, "branch", "--format=%(refname:short)").stdout.split()


# ─── 1: a hyphen inside a subtask name is not the separator ────────────────


def test_a_hyphenated_path_is_one_subtask_not_two():
    spec = "## Subtasks\n\n1. `src/user-service.ts` — add login\n"
    items = spec_subtasks(spec)
    assert items == [{"slug": "src/user-service.ts", "goal": "add login",
                      "paths": ["src/user-service.ts"]}]
    subs = [{"slug": "user-service", "path": "src/user-service.ts", "goal": "x"}]
    out, dropped, added = align_to_spec(subs, spec)
    assert [s["path"] for s in out] == ["src/user-service.ts"]
    assert dropped == [] and added == []


@pytest.mark.parametrize("line,slug,goal", [
    ("1. src/user-service.ts — add login", "src/user-service.ts", "add login"),
    ("2. **money-fix**: do x", "money-fix", "do x"),
    ("3. money-fix - do x", "money-fix", "do x"),
    ("4. money—fix fmt", "money", "fix fmt"),
])
def test_subtask_separators(line, slug, goal):
    it = spec_subtasks("## Subtasks\n" + line)[0]
    assert (it["slug"], it["goal"]) == (slug, goal)


# ─── 2: repo paths map onto the run worktree ───────────────────────────────


def test_planned_repo_paths_map_onto_the_run_worktree(tmp_path):
    repo = _repo(tmp_path / "proj")
    ws = tw.open_run(repo, "fix money.py")
    subs, dropped = tt.anchor_subtask_paths(
        [{"slug": "money", "path": f"{repo}/money.py"},
         {"slug": "t", "path": repo.lstrip("/") + "/tests/test_money.py"}],
        ws.cwd)
    assert [s["path"] for s in subs] == ["money.py", "tests/test_money.py"]
    assert dropped == []
    from aiforge_core.runtime.parallel_subtasks._stream import _anchor_paths
    gen = _anchor_paths([{"slug": "money", "path": f"{repo}/money.py"}], ws.cwd)
    evs = []
    try:
        while True:
            evs.append(next(gen))
    except StopIteration as stop:
        kept = stop.value
    assert kept == [{"slug": "money", "path": "money.py"}]
    assert not any("outside the workspace" in (e.get("text") or "") for e in evs)
    list(tw.close(ws))


def test_prompt_and_spec_paths_are_rewritten_to_the_worktree(tmp_path):
    repo = _repo(tmp_path / "proj")
    text = f"In {repo} fix {repo}/money.py so every test in {repo}/tests/ passes."
    assert tt.localize_paths(text, repo) == (
        "In ./ fix money.py so every test in tests/ passes.")
    ws = tw.open_run(repo, "fix money.py")
    p = tw.write_spec(ws.cwd, f"# Spec\n\nEdit `{repo}/money.py`.\n")
    assert repo not in open(p).read() and "`money.py`" in open(p).read()
    list(tw.close(ws))


def test_the_pipeline_gets_worktree_relative_paths(tmp_path, session_ws,
                                                   monkeypatch):
    from aiforge_core.api.routes._chat import _stages
    repo = _repo(tmp_path / "proj")
    seen = {}

    def _fake(_pp, prompt, cwd, session_id, history, *a):
        seen.update(prompt=prompt, cwd=cwd, history=history)
        a[-1]["done"] = True
        yield {"type": "message", "text": "built"}
    monkeypatch.setattr(_stages, "_pipeline_route", _fake)
    prompt = f"In {repo} fix money.py so every test in tests/ passes."
    _run_dispatch(prompt, session_ws)
    assert repo not in seen["prompt"] and "fix money.py" in seen["prompt"]
    assert repo not in seen["history"][-1]["content"]


# ─── 3: only an explicit imperative in the current message applies ────────


@pytest.mark.parametrize("text", [
    "the test fails on my branch, fix it",
    "fix the crash we see on this branch",
    "I merged it into main yesterday and now it fails",
    "the commit to main broke the tests",
    "don't merge it into main",
    "do not fast-forward anything",
])
def test_descriptive_branch_mentions_do_not_apply(text):
    assert not tw.wants_apply([text])


@pytest.mark.parametrize("text", [
    "fix it and apply it to my branch", "then merge into main",
    "fix money.py and commit to my branch", "fast-forward when done",
])
def test_explicit_imperatives_apply(text):
    assert tw.wants_apply([text])


def test_an_old_message_never_applies_forever():
    assert not tw.wants_apply(["fix the tests", "commit it to main"])


def test_the_route_decides_apply_from_the_current_message(tmp_path,
                                                          session_ws,
                                                          monkeypatch):
    repo = _repo(tmp_path / "proj")
    hist = [{"role": "user", "content": f"In {repo} fix x and commit it to main"}]
    rctx = _run_dispatch(f"In {repo}: the test fails on my branch, fix it",
                         session_ws, history=hist, monkeypatch=monkeypatch)
    ws = rctx["team_ws"]
    assert ws.apply is False


# ─── 4: "not a repo" is never cached ───────────────────────────────────────


def test_a_folder_initialised_after_turn_one_is_a_repo_on_turn_two(tmp_path,
                                                                  session_ws):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    first = tt.resolve_team_target([f"In {d} fix a.py"], session_ws)
    assert first.init_needed
    assert tw.init_repo(str(d))
    second = tt.resolve_team_target([f"In {d} fix a.py"], session_ws)
    assert not second.init_needed and second.cwd == os.path.realpath(str(d))


# ─── 5: consent is not a write grant; the jail guards the real checkout ────


def test_team_consent_does_not_grant_writes_to_the_real_repo(tmp_path,
                                                            monkeypatch):
    from aiforge_core.api.routes._chat import _team_route
    from aiforge_core.runtime import chat_approve, chat_write_grants
    repo = _repo(tmp_path / "proj")
    granted = []
    monkeypatch.setattr(chat_approve, "request", lambda sid: 1)
    monkeypatch.setattr(chat_approve, "wait", lambda sid: {"decision": "approve"})
    monkeypatch.setattr(chat_write_grants, "grant",
                        lambda sid, roots: granted.append(roots))
    gen = _team_route._ask_consent(9, repo, "ok?")
    evs = []
    try:
        while True:
            evs.append(next(gen))
    except StopIteration as stop:
        assert stop.value is True
    assert granted == [] and evs[0]["grant_roots"] == []
    assert _team_route._granted(9, repo)          # asked once per chat


def test_the_jail_refuses_the_real_repo_during_a_team_run(tmp_path,
                                                          monkeypatch):
    from aiforge_core.runtime import chat_write_grants, scope_guard
    from aiforge_core.runtime.chat_agent._turn._state import _writable_roots
    repo = _repo(tmp_path / "proj")
    monkeypatch.delenv("AIFORGE_CHAT_WORKSPACE_JAIL", raising=False)
    monkeypatch.setattr(chat_write_grants, "granted", lambda sid: [repo])
    ws = tw.open_run(repo, "fix money.py")
    msgs = [{"role": "user", "content": f"In {repo} fix money.py"}]
    roots = _writable_roots(msgs, 3, ws.cwd)
    assert repo not in roots
    args = {"path": f"{repo}/money.py", "content": "x"}
    assert scope_guard.outside_workspace("file_write", args, ws.cwd, roots)
    ok = {"path": os.path.join(ws.cwd, "money.py"), "content": "x"}
    assert not scope_guard.outside_workspace("file_write", ok, ws.cwd, roots)
    # outside a team run, naming the folder still allows it (chat mode)
    assert repo in _writable_roots(msgs, 3, str(tmp_path))
    list(tw.close(ws))


# ─── 6: cleanup on failure, disconnect and after a crash ───────────────────


def _run_dispatch(prompt, cwd, *, history=None, session_id=None,
                  monkeypatch=None, pipeline=None):
    from aiforge_core.api.routes._chat import _stages
    if pipeline is not None:
        monkeypatch.setattr(_stages, "_pipeline_route", pipeline)
    elif monkeypatch is not None:
        def _ok(_pp, prompt, cwd, *a):
            a[-1]["done"] = True
            yield {"type": "message", "text": "built"}
        monkeypatch.setattr(_stages, "_pipeline_route", _ok)
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    hist = list(history or []) + [{"role": "user", "content": prompt}]
    list(_stages._dispatch_agent_route(rd, None, prompt, cwd, session_id, hist,
                                       lambda t: t, {}, 0.0, False, "", rctx))
    return rctx


def test_a_failing_run_leaves_no_worktree_branch_or_registry(tmp_path,
                                                             session_ws,
                                                             monkeypatch):
    repo = _repo(tmp_path / "proj")
    made = {}

    def _boom(_pp, prompt, cwd, *a):
        made["ws"] = tw.for_cwd(cwd)
        yield {"type": "thought", "text": "planning"}
        raise RuntimeError("planner crashed")
    with pytest.raises(RuntimeError):
        _run_dispatch(f"In {repo} fix money.py", session_ws,
                      monkeypatch=monkeypatch, pipeline=_boom)
    ws = made["ws"]
    assert not os.path.exists(ws.run_dir) and tw._RUNS == {}
    assert prot.rules_for(ws.cwd) == {}
    assert not any(b.startswith("aiforge") for b in _branches(repo))
    assert ws.cwd not in _git(repo, "worktree", "list").stdout


def test_a_client_disconnect_cleans_up(tmp_path, session_ws, monkeypatch):
    from aiforge_core.api.routes._chat import _stages
    repo = _repo(tmp_path / "proj")

    def _slow(_pp, prompt, cwd, *a):
        yield {"type": "thought", "text": "one"}
        yield {"type": "thought", "text": "two"}
    monkeypatch.setattr(_stages, "_pipeline_route", _slow)
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    gen = _stages._dispatch_agent_route(
        rd, None, f"In {repo} fix money.py", session_ws, None,
        [{"role": "user", "content": f"In {repo} fix money.py"}],
        lambda t: t, {}, 0.0, False, "", rctx)
    while next(gen).get("text") != "one":
        pass
    gen.close()
    ws = rctx["team_ws"]
    assert ws.closed and not os.path.exists(ws.run_dir) and tw._RUNS == {}


def test_a_run_that_committed_nothing_leaves_no_branch(tmp_path):
    repo = _repo(tmp_path / "proj")
    ws = tw.open_run(repo, "fix money.py")
    notes = list(tw.close(ws))
    assert ws.branch not in _branches(repo)
    assert "no commits" in notes[-1]["text"]


def test_stale_run_worktrees_are_swept(tmp_path):
    repo = _repo(tmp_path / "proj")
    ws = tw.open_run(repo, "fix money.py")
    tw._RUNS.clear()                                  # the server "crashed"
    old = time.time() - 48 * 3600
    os.utime(ws.run_dir, (old, old))
    assert life.sweep_stale(12) == [os.path.join(tw._runs_root(),
                                                 os.path.basename(ws.run_dir))]
    assert not os.path.exists(ws.run_dir)
    assert ws.cwd not in _git(repo, "worktree", "list").stdout
    assert ws.branch not in _branches(repo)


def test_a_live_run_is_never_swept(tmp_path):
    repo = _repo(tmp_path / "proj")
    ws = tw.open_run(repo, "fix money.py")
    old = time.time() - 48 * 3600
    os.utime(ws.run_dir, (old, old))
    assert life.sweep_stale(12) == [] and os.path.isdir(ws.cwd)
    list(tw.close(ws))
