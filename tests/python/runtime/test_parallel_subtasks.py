"""Parallel subtask orchestrator — real git worktrees, retries, validation,
integration test, conflict handling, no file clobbering."""
import os
import subprocess
import threading
import time

import pytest

from aiforge_core.runtime import parallel_subtasks as ps


def _repo(tmp_path, base="work"):
    repo = str(tmp_path)
    def g(*a): return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    open(repo + "/README.md", "w").write("base\n"); g("add", "-A"); g("commit", "-q", "-m", "init")
    g("branch", "-M", base)
    return repo, g


def _writer(name_ok=lambda s: True):
    def run_one(subtask, wt):
        open(os.path.join(wt, f"{subtask['slug']}.txt"), "w").write(subtask["slug"] + "\n")
        return {"ok": name_ok(subtask["slug"])}
    return run_one


# ── success / parallelism / cleanup ──────────────────────────────────────────
def test_all_success_merge_cleanup_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", "4")
    repo, g = _repo(tmp_path)
    peak = [0]; cur = [0]; lock = threading.Lock()

    def run_one(subtask, wt):
        with lock:
            cur[0] += 1; peak[0] = max(peak[0], cur[0])
        time.sleep(0.25)
        open(os.path.join(wt, f"{subtask['slug']}.txt"), "w").write("x\n")
        with lock:
            cur[0] -= 1
        return {"ok": True}

    subs = [{"slug": s, "goal": s} for s in ("a", "b", "c", "d")]
    r = ps.run_parallel(repo, "work", None, subs, run_one)
    assert r["ok"] and r["done"] == 4 and r["merged"] == 4 and r["failed"] == 0
    assert {"a.txt", "b.txt", "c.txt", "d.txt"} <= set(os.listdir(repo))
    assert peak[0] > 1                                   # genuinely parallel
    assert not os.path.isdir(repo + "/.aiforge-worktrees/sub-a")


# ── persistent failure → not merged ──────────────────────────────────────────
def test_persistent_failure_not_merged(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SUBTASK_RETRIES", "2")
    repo, _ = _repo(tmp_path)
    r = ps.run_parallel(repo, "work", None,
                        [{"slug": s, "goal": s} for s in ("a", "bad")],
                        _writer(lambda s: s != "bad"))
    files = set(os.listdir(repo))
    assert "a.txt" in files and "bad.txt" not in files
    assert r["done"] == 1 and r["failed"] == 1
    # bad was retried (initial + 2 retries = 3 attempts)
    bad = next(x for x in r["results"] if x["slug"] == "bad")
    assert bad["attempts"] == 3


# ── retry: fails twice then succeeds ─────────────────────────────────────────
def test_retry_then_success(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SUBTASK_RETRIES", "3")
    repo, _ = _repo(tmp_path)
    tries = {"flaky": 0}

    def run_one(subtask, wt):
        open(os.path.join(wt, f"{subtask['slug']}.txt"), "w").write("x\n")
        if subtask["slug"] == "flaky":
            tries["flaky"] += 1
            return {"ok": tries["flaky"] >= 3}        # succeeds on 3rd attempt
        return {"ok": True}

    r = ps.run_parallel(repo, "work", None,
                        [{"slug": "flaky", "goal": "f"}], run_one)
    assert r["done"] == 1 and r["ok"]
    assert next(x for x in r["results"] if x["slug"] == "flaky")["attempts"] == 3
    assert "flaky.txt" in set(os.listdir(repo))


# ── crash (exception) is caught + retried ────────────────────────────────────
def test_crash_is_caught_and_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SUBTASK_RETRIES", "2")
    repo, _ = _repo(tmp_path)
    n = {"c": 0}

    def run_one(subtask, wt):
        n["c"] += 1
        if n["c"] < 2:
            raise RuntimeError("boom")                 # crash on first attempt
        open(os.path.join(wt, "c.txt"), "w").write("x\n")
        return {"ok": True}

    r = ps.run_parallel(repo, "work", None, [{"slug": "c", "goal": "c"}], run_one)
    assert r["done"] == 1 and "c.txt" in set(os.listdir(repo))   # recovered after crash


# ── per-subtask validation gate ──────────────────────────────────────────────
def test_validation_gate_blocks_unvalidated(tmp_path):
    repo, _ = _repo(tmp_path)
    r = ps.run_parallel(repo, "work", None,
                        [{"slug": s, "goal": s} for s in ("a", "b", "c")],
                        _writer(),
                        validate_one=lambda s, w: {"ok": s["slug"] != "c"})
    files = set(os.listdir(repo))
    assert {"a.txt", "b.txt"} <= files and "c.txt" not in files
    assert r["done"] == 2 and r["validated"] == 2 and r["failed"] == 1


# ── final integration test gate ──────────────────────────────────────────────
def test_integration_test_pass_and_fail(tmp_path):
    repo, _ = _repo(tmp_path)
    # integration passes
    r = ps.run_parallel(repo, "work", None, [{"slug": "a", "goal": "a"}], _writer(),
                        integration_test=lambda root: {"ok": True})
    assert r["ok"] and r["integration"]["ok"] is True and "integration green" in r["review"]
    # integration fails → overall not ok even though subtasks merged
    (tmp_path / "two").mkdir(); repo2, _ = _repo(tmp_path / "two")
    r2 = ps.run_parallel(repo2, "work", None, [{"slug": "a", "goal": "a"}], _writer(),
                         integration_test=lambda root: {"ok": False})
    assert r2["ok"] is False and r2["integration"]["ok"] is False
    assert "integration FAILED" in r2["review"]


# ── no file/folder clobbering across subtasks ────────────────────────────────
def test_no_clobber_distinct_files(tmp_path):
    repo, _ = _repo(tmp_path)
    # each subtask writes a DIFFERENT file → all merge, none overwritten
    r = ps.run_parallel(repo, "work", None,
                        [{"slug": s, "goal": s} for s in ("x", "y", "z")], _writer())
    assert r["merged"] == 3
    for s in ("x", "y", "z"):
        assert open(repo + f"/{s}.txt").read().strip() == s   # content intact, not clobbered


def test_same_file_conflict_aborts_clean(tmp_path, monkeypatch):
    # This test asserts the ABORT-on-conflict contract. The default path now
    # auto-resolves conflict hunks (AIFORGE_RESOLVE_CONFLICTS, default on) via
    # an LLM safety valve, so pin it off to deterministically exercise the
    # clean-abort branch (no LLM dependency, no half-merged clobber).
    monkeypatch.setenv("AIFORGE_RESOLVE_CONFLICTS", "0")
    repo, g = _repo(tmp_path)
    open(repo + "/shared.txt", "w").write("base\n"); g("add", "-A"); g("commit", "-q", "-m", "s")

    def run_one(subtask, wt):
        open(os.path.join(wt, "shared.txt"), "w").write(f"by {subtask['slug']}\n")
        return {"ok": True}

    r = ps.run_parallel(repo, "work", None,
                        [{"slug": "x", "goal": "1"}, {"slug": "y", "goal": "2"}], run_one)
    assert r["merged"] == 1 and len(r["conflicts"]) == 1
    assert "UU" not in g("status").stdout            # base left clean, no clobber


def test_no_subtasks_noop(tmp_path):
    repo, _ = _repo(tmp_path)
    r = ps.run_parallel(repo, "work", None, [], _writer())
    assert r["total"] == 0 and r["ok"]


def test_plan_files_same_basename_distinct_slugs():
    """Two architect files sharing a basename (a/db.py + b/db.py) must get
    DISTINCT slugs — same slug → same worktree dir/branch → workers collide."""
    plan = ps._plan_files([
        {"path": "a/db.py", "purpose": "store A"},
        {"path": "b/db.py", "purpose": "store B"},
    ])
    slugs = [p["slug"] for p in plan]
    assert len(plan) == 2
    assert len(set(slugs)) == 2, slugs            # unique within the plan
    assert slugs[0].startswith("db")              # base slug preserved for the first
    # distinct full paths still both present in the goals
    assert {"a/db.py", "b/db.py"} == {p["goal"].split(":")[0] for p in plan}


# ── CC1: run-unique worktree dirs + branches (concurrent runs don't collide) ──
def test_make_worktree_run_unique(tmp_path):
    repo, _ = _repo(tmp_path)
    # Same slug, two different run tokens → DISTINCT worktree dirs + branches,
    # both existing at once (one run can't force-remove the other's, CC1).
    wt1, b1 = ps._make_worktree(repo, "work", "a", "tok11111")
    wt2, b2 = ps._make_worktree(repo, "work", "a", "tok22222")
    assert wt1 != wt2 and b1 != b2
    assert "tok11111" in wt1 and "tok11111" in b1
    assert "tok22222" in wt2 and "tok22222" in b2
    assert os.path.isdir(wt1) and os.path.isdir(wt2)   # coexist, no destruction
    # Back-compat: no token → legacy fixed path/branch.
    wt3, b3 = ps._make_worktree(repo, "work", "b")
    assert wt3.endswith(os.path.join(".aiforge-worktrees", "sub-b"))
    assert b3 == "work-sub-b"


def test_run_parallel_uses_run_unique_worktrees(tmp_path):
    repo, _ = _repo(tmp_path)
    seen: list = []

    def run_one(subtask, wt):
        seen.append(os.path.basename(wt))
        open(os.path.join(wt, f"{subtask['slug']}.txt"), "w").write("x\n")
        return {"ok": True}

    ps.run_parallel(repo, "work", None,
                    [{"slug": s, "goal": s} for s in ("a", "b")], run_one)
    # Each worktree dir is token-prefixed (e.g. "<token>-a"), not "sub-a", and
    # all subtasks in one run share the SAME token (CC1).
    assert seen and not any(n.startswith("sub-") for n in seen)
    tokens = {n.rsplit("-", 1)[0] for n in seen}
    assert len(tokens) == 1 and tokens.pop()


def test_validation_tests_strict(monkeypatch):
    """Failing tests must NOT pass via the build fallback."""
    import aiforge_core.runtime.tools.project_runner as pr
    monkeypatch.setattr(pr, "detect", lambda cwd: {"stacks": ["python"]})
    monkeypatch.setattr(pr, "_has_tests", lambda cwd, stacks: True)
    monkeypatch.setattr(pr, "project",
                        lambda action, cwd: {"ok": action != "test"})
    assert ps._build_or_test("/x")["ok"] is False        # tests failed → fail
    # no tests + green build → pass
    monkeypatch.setattr(pr, "_has_tests", lambda cwd, stacks: False)
    monkeypatch.setattr(pr, "project", lambda action, cwd: {"ok": action == "build"})
    assert ps._build_or_test("/x")["ok"] is True
    # no project → nothing to gate
    monkeypatch.setattr(pr, "detect", lambda cwd: {"stacks": []})
    assert ps._build_or_test("/x")["ok"] is True


def test_inflight_guard_rejects_second_run(monkeypatch):
    from aiforge_core.tickets import subtasks as st
    monkeypatch.setattr(st, "get_subtasks", lambda tid: [{"slug": "a", "goal": "a"}])

    class T:
        id = 999
        identifier = "ONE-999"
    ps._INFLIGHT.add(999)               # pretend a run is already in flight
    try:
        r = ps.run_subtasks_parallel(T())
        assert r["ok"] is False and "already running" in r["error"]
    finally:
        ps._INFLIGHT.discard(999)


def test_parallel_does_not_mutate_global_ticket_env(monkeypatch, tmp_path):
    """Cross-ticket safety: the parallel path must NOT write the process-global
    AIFORGE_CURRENT_TICKET (two different tickets would clobber each other)."""
    import os
    from aiforge_core.tickets import subtasks as st
    monkeypatch.setattr(st, "get_subtasks", lambda tid: [{"slug": "a", "goal": "a"}])
    monkeypatch.setattr(ps, "ensure_branch_and_worktree", lambda t: None, raising=False)
    # patch the imported name inside the function via the workspace module
    import aiforge_core.runtime.workspace as ws
    monkeypatch.setattr(ws, "ensure_branch_and_worktree", lambda t: None)
    monkeypatch.setenv("AIFORGE_CURRENT_TICKET", "ONE-SENTINEL")

    class T:
        id = 4242
        identifier = "ONE-4242"
    ps.run_subtasks_parallel(T())                    # returns early (no worktree)
    assert os.environ["AIFORGE_CURRENT_TICKET"] == "ONE-SENTINEL"  # untouched
