"""Parallel subtask orchestrator — real git worktrees + merge."""
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


def test_parallel_isolation_concurrency_merge_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", "4")
    repo, g = _repo(tmp_path)
    running = [0]; peak = [0]; lock = threading.Lock()

    def run_one(subtask, wt):
        with lock:
            running[0] += 1; peak[0] = max(peak[0], running[0])
        time.sleep(0.3)
        open(os.path.join(wt, f"{subtask['slug']}.txt"), "w").write("x\n")
        with lock:
            running[0] -= 1
        return {"ok": subtask["slug"] != "bad"}

    subs = [{"slug": s, "goal": s} for s in ("a", "b", "c", "bad")]
    r = ps.run_parallel(repo, "work", None, subs, run_one)
    files = set(os.listdir(repo))
    assert {"a.txt", "b.txt", "c.txt"} <= files          # successes merged
    assert "bad.txt" not in files                         # failure not merged
    assert r["done"] == 3 and r["merged"] == 3 and r["failed"] == 1
    assert peak[0] > 1                                     # genuinely parallel
    assert not os.path.isdir(repo + "/.aiforge-worktrees/sub-a")  # cleaned


def test_merge_conflict_aborts_and_reports(tmp_path):
    repo, g = _repo(tmp_path)

    def run_one(subtask, wt):
        open(os.path.join(wt, "shared.txt"), "w").write(f"by {subtask['slug']}\n")
        return {"ok": True}

    # seed the shared file so both branches modify it
    open(repo + "/shared.txt", "w").write("base\n"); g("add", "-A"); g("commit", "-q", "-m", "shared")
    r = ps.run_parallel(repo, "work", None,
                        [{"slug": "x", "goal": "1"}, {"slug": "y", "goal": "2"}], run_one)
    assert r["merged"] == 1 and len(r["conflicts"]) == 1
    # base branch left clean (no dangling conflict markers)
    assert "UU" not in g("status").stdout


def test_no_subtasks_is_noop(tmp_path):
    repo, _ = _repo(tmp_path)
    r = ps.run_parallel(repo, "work", None, [], lambda s, w: {"ok": True})
    assert r["total"] == 0 and r["ok"]
