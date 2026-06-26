"""Best-of-N orchestrator — real git worktrees, mocked runner + LLM grader.

Hermetic: ``run_one`` writes canned files (no real agent), ``client.complete``
(the grader) returns scripted scores (no network/LLM). cwd is a fresh tmp_path;
``_ensure_git_workspace`` does the git init.
"""
import json
import re

import pytest

from aiforge_core.runtime import best_of_n as bon


def _writer():
    """Per-attempt runner that writes a unique file naming its own slug, so the
    resulting diff is non-empty AND identifies the attempt for the grader."""
    def run_one(subtask, wt):
        import os
        slug = subtask["slug"]
        with open(os.path.join(wt, f"{slug}.txt"), "w") as f:
            f.write(f"impl for {slug}\n")
        return {"ok": True}
    return run_one


def _grader(scores: dict):
    """Mock ``client.complete``: read the slug out of the diff in the user
    message, return its scripted score as strict grader JSON."""
    def complete(role, messages, **kw):
        text = " ".join(m.get("content", "") for m in messages)
        m = re.search(r"bestof-\d+", text)
        slug = m.group(0) if m else "?"
        return json.dumps({"score": scores.get(slug, 0), "why": f"why {slug}"})
    return complete


def _patch_grader(monkeypatch, complete):
    import aiforge_core.llm.client as _client
    monkeypatch.setattr(_client, "complete", complete)


# ── picks the highest-scored attempt + right shape ───────────────────────────
def test_picks_highest_score(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({"bestof-0": 10, "bestof-1": 90,
                                        "bestof-2": 50}))
    r = bon.best_of_n("build a thing", str(tmp_path), n=3, run_one=_writer())

    assert r["ok"] is True
    assert r["n"] == 3
    assert r["winner"]["slug"] == "bestof-1"
    assert r["winner"]["score"] == 90
    assert r["winner"]["why"] == "why bestof-1"
    assert len(r["attempts"]) == 3
    assert {a["slug"] for a in r["attempts"]} == {"bestof-0", "bestof-1", "bestof-2"}
    for a in r["attempts"]:
        assert set(a) == {"slug", "score", "why"}
    assert "best of 3" in r["review"]
    # Winner's file merged into the workspace; losers discarded.
    import os
    assert "bestof-1.txt" in set(os.listdir(str(tmp_path)))
    # Loser worktrees cleaned up.
    wt_dir = os.path.join(str(tmp_path), ".aiforge-worktrees")
    if os.path.isdir(wt_dir):
        assert os.listdir(wt_dir) == []


# ── on_status emits run → grade → win for the winner ─────────────────────────
def test_on_status_emitted(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({"bestof-0": 5, "bestof-1": 80}))
    seen: list = []
    bon.best_of_n("x", str(tmp_path), n=2, run_one=_writer(),
                  on_status=lambda slug, status, *a: seen.append((slug, status)))
    statuses = {s for _, s in seen}
    assert "running" in statuses and "grading" in statuses
    assert ("bestof-1", "won") in seen


# ── n guarded to [2, 6] ──────────────────────────────────────────────────────
def test_n_guarded_low(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({"bestof-0": 50, "bestof-1": 60}))
    r = bon.best_of_n("x", str(tmp_path), n=1, run_one=_writer())
    assert r["n"] == 2 and len(r["attempts"]) == 2


def test_n_guarded_high(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({f"bestof-{i}": i for i in range(10)}))
    r = bon.best_of_n("x", str(tmp_path), n=99, run_one=_writer())
    assert r["n"] == 6 and len(r["attempts"]) == 6


def test_default_n_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_BEST_OF_N", "4")
    _patch_grader(monkeypatch, _grader({f"bestof-{i}": i * 10 for i in range(4)}))
    r = bon.best_of_n("x", str(tmp_path), n=None, run_one=_writer())
    assert r["n"] == 4


# ── grading distinguishes "grader unavailable" (graded=False/score=None) from
#    a real score of 0 (B5) ────────────────────────────────────────────────────
def test_grade_soft_fail_unparseable(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "not json at all")
    g = bon._grade("spec", "some diff")
    assert g == {"score": None, "why": "grade failed", "graded": False}


def test_grade_soft_fail_llm_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    g = bon._grade("spec", "diff")
    assert g == {"score": None, "why": "grade failed", "graded": False}


def test_grade_parses_valid(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: 'noise {"score": 77, "why": "ok"} tail')
    g = bon._grade("spec", "diff")
    assert g == {"score": 77, "why": "ok", "graded": True}


def test_grade_clamps_score(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: '{"score": 999, "why": "x"}')
    assert bon._grade("s", "d")["score"] == 100


# CF5 — the grader call is labelled "grader" (not "reviewer") for Perf.
def test_grade_uses_grader_role(monkeypatch):
    seen = {}

    def complete(role, messages, **kw):
        seen["role"] = role
        return '{"score": 50, "why": "ok"}'

    monkeypatch.setattr("aiforge_core.llm.client.complete", complete)
    bon._grade("spec", "diff")
    assert seen["role"] == "grader"


# ── all attempts produce no diff → not ok, nothing merged ────────────────────
def test_all_fail_no_diff(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({}))

    def noop_run_one(subtask, wt):
        return {"ok": True}        # writes nothing → empty diff

    r = bon.best_of_n("x", str(tmp_path), n=2, run_one=noop_run_one)
    assert r["ok"] is False
    assert "all 2 attempts failed" in r["review"]
    assert all(a["score"] == 0 for a in r["attempts"])
    # M5 — nothing merged → the winner row matched neither old cleanup arm and
    # its worktree+branch leaked. Now EVERY attempt is cleaned unconditionally.
    import os
    wt_dir = os.path.join(str(tmp_path), ".aiforge-worktrees")
    if os.path.isdir(wt_dir):
        assert os.listdir(wt_dir) == []
    import subprocess
    branches = subprocess.run(["git", "branch"], cwd=str(tmp_path),
                              capture_output=True, text=True).stdout
    assert "-sub-" not in branches and "bestof-" not in branches


# ── B2: winner PRESERVED when its merge fails; losers still cleaned ───────────
def test_winner_preserved_on_merge_failure(tmp_path, monkeypatch):
    import os
    import subprocess
    _patch_grader(monkeypatch, _grader({"bestof-0": 10, "bestof-1": 90}))
    # Force the winner's merge to fail — its branch + worktree must survive for
    # recovery, and the reason must be surfaced (B2).
    monkeypatch.setattr(bon, "_merge_branch",
                        lambda repo, base, branch: (False, "CONFLICT boom"))
    r = bon.best_of_n("x", str(tmp_path), n=2, run_one=_writer())

    assert r["ok"] is False
    assert "merge FAILED" in r["review"]
    assert "CONFLICT boom" in (r.get("merge_error") or "")
    wbranch = r["winner"]["branch"]
    assert wbranch and "bestof-1" in wbranch       # the real winner (score 90)

    # Winner branch preserved; loser branch removed.
    branches = subprocess.run(["git", "branch"], cwd=str(tmp_path),
                              capture_output=True, text=True).stdout
    assert wbranch in branches
    assert "bestof-0" not in branches              # loser cleaned
    # Exactly ONE worktree kept — the winner's.
    wt_dir = os.path.join(str(tmp_path), ".aiforge-worktrees")
    remaining = os.listdir(wt_dir) if os.path.isdir(wt_dir) else []
    assert len(remaining) == 1 and "bestof-1" in remaining[0]


# ── B5: grader offline for ALL → fall back to a real diff, don't discard ─────
def test_grader_offline_falls_back_to_real_diff(tmp_path, monkeypatch):
    import os

    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    r = bon.best_of_n("x", str(tmp_path), n=3, run_one=_writer())

    assert r["ok"] is True                         # a real diff merged
    assert r["winner"]["slug"] == "bestof-0"       # deterministic ungraded pick
    assert r["winner"]["score"] is None            # ungraded sentinel, not 0
    assert "ungraded" in r["review"] and "merged" in r["review"]
    assert "bestof-0.txt" in set(os.listdir(str(tmp_path)))


def test_grader_offline_never_picks_no_diff_over_real_diff(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)

    def run_one(subtask, wt):
        import os
        slug = subtask["slug"]
        if slug != "bestof-0":                     # bestof-0 writes nothing
            open(os.path.join(wt, f"{slug}.txt"), "w").write(slug + "\n")
        return {"ok": True}

    r = bon.best_of_n("x", str(tmp_path), n=3, run_one=run_one)
    assert r["ok"] is True
    # The no-diff attempt (bestof-0) must NOT win over a real diff.
    assert r["winner"]["slug"] != "bestof-0"


# ── B1: tie-break is deterministic (lowest slug), not completion-order ────────
def test_tie_break_deterministic(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({f"bestof-{i}": 50 for i in range(4)}))
    winners = []
    for k in range(4):
        d = tmp_path / f"run{k}"
        d.mkdir()
        r = bon.best_of_n("x", str(d), n=4, run_one=_writer())
        winners.append(r["winner"]["slug"])
    assert len(set(winners)) == 1                  # reproducible across runs
    assert winners[0] == "bestof-0"                # lowest slug wins the tie


# ── CC1: worktree dirs are run-unique (token-prefixed), not fixed paths ───────
def test_best_of_n_worktree_paths_run_unique(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({"bestof-0": 1, "bestof-1": 2}))
    seen: list = []

    def run_one(subtask, wt):
        import os
        seen.append(os.path.basename(wt))
        open(os.path.join(wt, f"{subtask['slug']}.txt"), "w").write("x\n")
        return {"ok": True}

    bon.best_of_n("x", str(tmp_path), n=2, run_one=run_one)
    # Names look like "<token>-bestof-N" (NOT the legacy "sub-bestof-N"); all
    # share ONE non-empty run token so concurrent runs can't collide (CC1).
    assert seen and all("-bestof-" in name for name in seen)
    assert not any(name.startswith("sub-") for name in seen)
    tokens = {name.split("-bestof-")[0] for name in seen}
    assert len(tokens) == 1 and tokens.pop()
