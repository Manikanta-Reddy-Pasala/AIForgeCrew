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


# ── grading soft-fails to score 0 ────────────────────────────────────────────
def test_grade_soft_fail_unparseable(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "not json at all")
    g = bon._grade("spec", "some diff")
    assert g == {"score": 0, "why": "grade failed"}


def test_grade_soft_fail_llm_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    g = bon._grade("spec", "diff")
    assert g == {"score": 0, "why": "grade failed"}


def test_grade_parses_valid(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: 'noise {"score": 77, "why": "ok"} tail')
    g = bon._grade("spec", "diff")
    assert g == {"score": 77, "why": "ok"}


def test_grade_clamps_score(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: '{"score": 999, "why": "x"}')
    assert bon._grade("s", "d")["score"] == 100


# ── all attempts produce no diff → not ok, nothing merged ────────────────────
def test_all_fail_no_diff(tmp_path, monkeypatch):
    _patch_grader(monkeypatch, _grader({}))

    def noop_run_one(subtask, wt):
        return {"ok": True}        # writes nothing → empty diff

    r = bon.best_of_n("x", str(tmp_path), n=2, run_one=noop_run_one)
    assert r["ok"] is False
    assert "all 2 attempts failed" in r["review"]
    assert all(a["score"] == 0 for a in r["attempts"])
