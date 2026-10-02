"""ONE git worktree per chat, and no concurrent writers inside it.

Owner rule: for any chat, ``git worktree list`` on the project shows the main
checkout plus exactly ONE worktree of that chat, from the first message to the
last, across mode switches (simple -> plan -> team) and across a multi-subtask
build. Real git throughout.
"""
from __future__ import annotations

import os
import subprocess

import pytest


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _trees(repo) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "cfg" / "chat.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "cfg" / "memory"))
    monkeypatch.setenv("AIFORGE_PROJECT_INGEST", "0")
    for k in ("AIFORGE_PARALLEL_SUBTASKS", "AIFORGE_PARALLEL_SUBTASKS_MAX",
              "AIFORGE_BEST_OF_N", "AIFORGE_SHARED_WORKTREE"):
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "repos"
    repo = root / "shop"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "cart.py").write_text("def total(x):\n    return sum(x)\n")
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
    e.repo, e.store = repo, chat_store
    e.new_chat = lambda: chat_store.create_session("chat", str(repo))["id"]
    yield e
    chat_store.reset_backend_for_tests()


def _cw():
    from aiforge_core.runtime import chat_worktree
    return chat_worktree


def _drive(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


# ── mode switches keep the chat's worktree ───────────────────────────────────

def test_team_is_eligible_for_a_fresh_chat_and_makes_the_one_worktree(env):
    cw = _cw()
    sid = env.new_chat()
    assert cw.eligible(env.store.get_session(sid), "team") == os.path.realpath(env.repo)
    data = cw.ensure(sid)
    assert data and len(_trees(env.repo)) == 2          # the checkout + the chat's


def test_simple_to_plan_to_team_never_adds_a_worktree(env, monkeypatch):
    from aiforge_core.api.routes._chat import _team_route as tr
    from aiforge_core.runtime import team_workspace as tw
    cw = _cw()
    sid = env.new_chat()
    wd = cw.ensure(sid)["path"]                          # first (simple) message
    before = _trees(env.repo)
    assert len(before) == 2
    # plan: same worktree, nothing new
    assert cw.ensure(sid)["path"] == wd
    # team: the user even pastes the project's own path in the message
    monkeypatch.setattr(tw, "open_run", lambda *a, **k: pytest.fail(
        "a second (per-run) worktree was opened for the chat's own repo"))
    prompt = f"fix the total in {env.repo}/src/cart.py"
    from aiforge_core.runtime import team_target as tt
    assert tt.resolve_team_target([prompt], wd).retargeted, \
        "premise: without the fold this retargets (and would open a run)"
    rctx: dict = {"done": False}
    sess = env.store.get_session(sid)
    got = _drive(tr._team_target_cwd(prompt, [], wd, rctx, sid))
    assert got == wd and "team_ws" not in rctx
    # the prompt is localised so no writer is sent into the user's checkout
    p2, _h = tr.localize(rctx, prompt, [])
    assert str(env.repo) not in p2 and "src/cart.py" in p2
    assert _trees(env.repo) == before
    assert sess["workdir"] == wd
    # and back to simple: still the same single worktree
    assert cw.ensure(sid)["path"] == wd and _trees(env.repo) == before


def test_a_chat_that_starts_in_team_mode_gets_exactly_one(env, monkeypatch):
    from aiforge_core.api.routes._chat import _producer as pr
    sid = env.new_chat()
    published: list = []

    class Run:
        def publish(self, ev):
            published.append(ev)

    class Body:
        mode = "team"

    pc = type("PC", (), {"session_id": sid, "body": Body(), "run": Run(), "cwd": str(env.repo)})()
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "set_phase", lambda *a, **k: None)
    pr._ensure_chat_worktree(pc)
    wd = env.store.get_session(sid)["workdir"]
    assert wd and pc.cwd == wd                           # team now runs IN the chat worktree
    pr._ensure_chat_worktree(pc)                         # next team message
    assert env.store.get_session(sid)["workdir"] == wd
    assert len(_trees(env.repo)) == 2


def test_covers_is_the_chats_repo_and_what_is_inside_it(env, tmp_path):
    cw = _cw()
    wd = cw.ensure(env.new_chat())["path"]
    assert cw.covers(wd, str(env.repo))
    assert cw.covers(wd, str(env.repo / "src"))
    assert not cw.covers(wd, str(tmp_path / "elsewhere"))
    assert not cw.covers(str(env.repo), str(env.repo))   # a plain checkout is not a chat worktree


def test_deleting_the_chat_leaves_no_worktree_behind(env):
    from aiforge_core.runtime import team_run_life
    cw = _cw()
    sid = env.new_chat()
    wd = cw.ensure(sid)["path"]
    assert len(_trees(env.repo)) == 2
    team_run_life.forget_session(sid)
    assert cw.remove(sid)["removed"] is True
    assert not os.path.isdir(wd) and len(_trees(env.repo)) == 1


# ── a multi-subtask run stays in the chat's worktree ─────────────────────────

def _writer(seen=None):
    def run_one(subtask, wt):
        if seen is not None:
            seen.append((subtask["slug"], os.path.realpath(wt), len(_trees(wt))))
        with open(os.path.join(wt, f"{subtask['slug']}.txt"), "w") as fh:
            fh.write(subtask["slug"] + "\n")
        return {"ok": True}
    return run_one


def test_a_multi_subtask_run_is_sequential_in_place_by_default(env):
    from aiforge_core.runtime import parallel_subtasks as pp
    cw = _cw()
    wd = cw.ensure(env.new_chat())["path"]
    base = _git(wd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    seen: list = []
    subs = [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}]
    assert pp.fan_out_enabled() is False and pp._max_workers() == 1
    agg = pp.run_parallel(wd, base, None, subs, _writer(seen),
                          in_place=not pp.fan_out_enabled())
    assert agg["ok"] and agg["done"] == 3 and agg["mode"] == "in_place"
    # every subtask ran in the chat's own worktree, and never saw another one
    assert {s for _slug, s, _n in seen} == {os.path.realpath(wd)}
    assert {n for _s, _w, n in seen} == {2}
    assert len(_trees(env.repo)) == 2
    assert not os.path.exists(os.path.join(wd, ".aiforge-worktrees"))
    # committed on the chat's own branch: no extra branch left, work in history
    log = _git(wd, "log", "--format=%s").stdout
    assert "subtask: a" in log and "subtask: c" in log
    assert not [b for b in _git(env.repo, "branch", "--format=%(refname:short)").stdout.split()
                if "-sub-" in b]
    for n in "abc":
        assert (os.path.exists(os.path.join(wd, f"{n}.txt")))


def test_a_failed_subtask_in_place_leaves_no_half_work(env):
    from aiforge_core.runtime import parallel_subtasks as pp
    wd = _cw().ensure(env.new_chat())["path"]
    base = _git(wd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def run_one(subtask, wt):
        with open(os.path.join(wt, f"{subtask['slug']}.txt"), "w") as fh:
            fh.write("x\n")
        return {"ok": subtask["slug"] != "bad"}

    os.environ["AIFORGE_SUBTASK_RETRIES"] = "0"
    try:
        agg = pp.run_parallel(wd, base, None, [{"slug": "ok1"}, {"slug": "bad"}, {"slug": "ok2"}],
                              run_one, in_place=True)
    finally:
        os.environ.pop("AIFORGE_SUBTASK_RETRIES", None)
    assert agg["done"] == 2 and agg["failed"] == 1 and not agg["ok"]
    assert not os.path.exists(os.path.join(wd, "bad.txt"))
    assert os.path.exists(os.path.join(wd, "ok1.txt")) and os.path.exists(os.path.join(wd, "ok2.txt"))
    assert len(_trees(env.repo)) == 2


def test_the_chat_turn_path_runs_in_place_unless_fan_out_is_opted_in(env, monkeypatch):
    """_stream._make_runner is what a chat turn calls."""
    import queue

    from aiforge_core.runtime.parallel_subtasks import _stream as st
    seen: dict = {}

    def fake(cwd, base, tid, subs, run_one, **kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(st, "run_parallel", fake)
    for opted, expect_in_place in ((False, True), (True, False)):
        if opted:
            monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "1")
            monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", "3")
        res: dict = {}
        st._make_runner("/x", "main", [{"slug": "a"}], None, None, lambda: False,
                        "", queue.Queue(), res)()
        assert seen["in_place"] is expect_in_place


def test_fan_out_needs_both_opt_in_switches(monkeypatch):
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS", raising=False)
    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS_MAX", raising=False)
    assert pp.fan_out_enabled() is False
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", "4")      # MAX alone: still off
    assert pp.fan_out_enabled() is False
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "1")
    assert pp.fan_out_enabled() is True
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", "1")      # one worker: off
    assert pp.fan_out_enabled() is False
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", "4")
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "0")
    assert pp.fan_out_enabled() is False
    assert pp.enabled() is False


def test_best_of_n_does_not_run_in_a_chat_without_the_opt_in(env, monkeypatch):
    """AIFORGE_BEST_OF_N alone (N worktrees) must not fan out inside a chat."""
    from aiforge_core.api.routes._chat import _routing as rt
    from aiforge_core.runtime import best_of_n as bon
    monkeypatch.setenv("AIFORGE_BEST_OF_N", "3")
    monkeypatch.setattr(bon, "stream_best_of_n",
                        lambda *a, **k: pytest.fail("best-of-N fanned out in a chat"))
    import types
    pp = types.SimpleNamespace(
        _enhance=lambda *a, **k: "spec", _architect=lambda *a, **k: [],
        _plan_files=lambda f: [], _decompose=lambda s: [{"slug": "one"}],
        _render_spec_md=lambda s, subs: "# SPEC", enabled=lambda: True)
    pctx: dict = {"done": False}
    wd = _cw().ensure(env.new_chat())["path"]
    list(rt._pipeline_route(pp, "build", wd, 1, [], lambda s: s, {}, 0.0, pctx))
    assert pctx["done"] is False and len(_trees(env.repo)) == 2
