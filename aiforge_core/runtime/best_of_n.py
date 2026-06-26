"""Best-of-N execution with an LLM grader (Gap C — quality lever).

Run the SAME task ``N`` independent times, each in its OWN isolated git
worktree, GRADE every attempt's diff against the spec with an LLM grader,
pick the highest-scored attempt, merge ONLY the winner back, discard the
rest. This is the "N attempts, pick best" lever (Cursor / Claude-Code style).

OPT-IN: nothing here runs unless a caller invokes :func:`best_of_n` (the api
chat handler only routes here when ``AIFORGE_BEST_OF_N`` is set AND the task
couldn't be split into ≥2 distinct subtasks — i.e. it's really ONE hard task).
The default single-attempt / parallel-team flows are untouched.

Reuses the worktree machinery from :mod:`parallel_subtasks` —
``_ensure_git_workspace``, ``_make_worktree``, ``_commit_all``,
``_merge_branch``, ``_git``, ``_max_workers``, ``_default_subtask_runner``,
``_update`` — so the isolation/concurrency/merge mechanics stay in one place.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import uuid

from aiforge_core.runtime.parallel_subtasks import (
    _commit_all,
    _default_subtask_runner,
    _ensure_git_workspace,
    _git,
    _make_worktree,
    _max_workers,
    _merge_branch,
    _update,
)

log = logging.getLogger("aiforge.best_of_n")

_GRADER_SYS = (
    "You are a strict code-review grader. Score how well the diff satisfies "
    "the spec, 0-100. Output ONLY JSON {\"score\":int,\"why\":\"...\"}."
)

_DIFF_CAP = 6000


def _default_n() -> int:
    """Default attempt count from ``AIFORGE_BEST_OF_N`` (when the caller doesn't
    pass ``n``), guarded to [2, 6]. Falls back to 3."""
    raw = os.environ.get("AIFORGE_BEST_OF_N", "3")
    try:
        return _guard_n(int(raw))
    except (TypeError, ValueError):
        return 3


def _guard_n(n: int) -> int:
    return max(2, min(6, int(n)))


_GRADE_FAILED = {"score": None, "why": "grade failed", "graded": False}


def _grade(spec: str, diff: str) -> dict:
    """LLM grader: score a diff against the spec, 0-100, with a one-line why.

    Returns ``{"score": int|None, "why": str, "graded": bool}``. On success
    ``graded`` is True and ``score`` is an int 0-100. On ANY failure (no LLM,
    empty/garbage output, unparseable JSON) it returns ``graded=False`` /
    ``score=None`` — a "grader unavailable" signal that is DISTINCT from a real
    score of 0, so the caller can fall back to keeping a real diff instead of
    discarding everything when grading is offline (B5)."""
    capped = (diff or "")[:_DIFF_CAP]
    try:
        from aiforge_core.llm import client
        # CF5 — label the grader call "grader" (not "reviewer") so the Perf
        # page attributes grading latency to the grader, not the reviewer role.
        out = client.complete("grader", [
            {"role": "system", "content": _GRADER_SYS},
            {"role": "user", "content": f"SPEC:\n{spec}\n\nDIFF:\n{capped}"}],
            max_tokens=300)
    except Exception as exc:  # noqa: BLE001 — grader is best-effort
        log.warning("best_of_n grade LLM call failed: %s", exc)
        return dict(_GRADE_FAILED)
    return _parse_grade(out)


def _parse_grade(out: str | None) -> dict:
    """Pull ``{"score":int,"why":str}`` out of an LLM response defensively.
    Returns ``graded=False``/``score=None`` when nothing parseable is found."""
    m = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not m:
        return dict(_GRADE_FAILED)
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return dict(_GRADE_FAILED)
    if not isinstance(obj, dict):
        return dict(_GRADE_FAILED)
    try:
        score = int(obj.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    why = str(obj.get("why") or "").strip()[:200]
    return {"score": score, "why": why, "graded": True}


def _attempt(spec: str, repo: str, base: str, i: int, run_one,
             on_status=None, run_token: str | None = None) -> dict:
    """Run ONE independent attempt in its own worktree, then grade its diff.

    Returns ``{slug, score, why, branch, worktree, ok, graded}``. A crash in the
    runner is caught (ok=False) so it can't kill the whole batch. ``graded`` is
    True only when the LLM grader actually returned a score; a real diff whose
    grade is unavailable carries ``ok=True, graded=False, score=None``.

    ``run_token`` makes the worktree dir + branch RUN-UNIQUE so concurrent
    best-of-N runs in the SAME cwd can't collide on fixed paths (CC1)."""
    slug = f"bestof-{i}"
    _update(None, slug, "running", on_status)
    try:
        wt, branch = _make_worktree(repo, base, slug, run_token)
    except Exception as exc:  # noqa: BLE001
        log.warning("best_of_n worktree add failed for %s: %s", slug, exc)
        _update(None, slug, "failed", on_status)
        return {"slug": slug, "score": 0, "why": f"worktree failed: {exc}",
                "branch": None, "worktree": None, "ok": False, "graded": False}

    try:
        res = run_one({"slug": slug, "goal": spec}, wt) or {}
        ran_ok = bool(res.get("ok", True))
    except Exception as exc:  # noqa: BLE001 — crash in the runner
        log.warning("best_of_n attempt %s crashed: %s", slug, exc)
        ran_ok = False

    # Everything past worktree creation MUST carry branch+worktree back in its
    # result even on failure, so the caller can clean it up. A bare git timeout
    # in _commit_all/_git would otherwise raise out of here, the future would be
    # recorded with branch=None/worktree=None, and the worktree would orphan.
    try:
        _commit_all(wt, slug)
        # Diff of the attempt's branch vs base — what this attempt changed.
        diff = (_git(["diff", base, "HEAD"], wt).stdout or "")
        if not diff.strip() or not ran_ok:
            _update(None, slug, "failed", on_status)
            return {"slug": slug, "score": 0,
                    "why": "no diff produced" if not diff.strip() else "runner failed",
                    "branch": branch, "worktree": wt, "ok": False, "graded": False}

        _update(None, slug, "grading", on_status)
        graded = _grade(spec, diff)
        _update(None, slug, "graded", on_status)
        return {"slug": slug, "score": graded["score"], "why": graded["why"],
                "branch": branch, "worktree": wt, "ok": True,
                "graded": bool(graded.get("graded"))}
    except Exception as exc:  # noqa: BLE001 — keep branch+worktree for cleanup
        log.warning("best_of_n post-worktree step failed for %s: %s", slug, exc)
        _update(None, slug, "failed", on_status)
        return {"slug": slug, "score": 0, "why": f"post-worktree error: {exc}",
                "branch": branch, "worktree": wt, "ok": False, "graded": False}


def _cleanup(repo: str, attempt: dict) -> None:
    """Discard a (loser) attempt's worktree + branch — mirrors the best-effort
    cleanup ``parallel_subtasks.run_parallel`` does after merging."""
    wt = attempt.get("worktree")
    if wt and os.path.isdir(wt):
        _git(["worktree", "remove", "--force", wt], repo)
    if attempt.get("branch"):
        _git(["branch", "-D", attempt["branch"]], repo)


def best_of_n(spec: str, cwd: str, *, n: int = 3, run_one=None,
              on_status=None) -> dict:
    """Run ``spec`` ``n`` independent times in isolated worktrees, grade each,
    merge the best, discard the rest.

    Args:
        spec:      the task — run identically in every attempt.
        cwd:       working dir; made a git workspace if it isn't one.
        n:         attempt count (guarded to [2, 6]); defaults from
                   ``AIFORGE_BEST_OF_N`` when the caller passes the sentinel 3.
        run_one:   per-attempt executor ``(subtask, worktree) -> {ok, ...}``.
                   Defaults to ``parallel_subtasks._default_subtask_runner()``.
        on_status: optional ``(slug, status)`` callback emitted as attempts
                   run / grade / win — mirrors ``parallel_subtasks._update``.

    Returns ``{ok, n, winner, attempts, review}``.
    """
    n = _guard_n(n if n is not None else _default_n())
    runner = run_one or _default_subtask_runner()
    base = _ensure_git_workspace(cwd)
    # ONE run-unique token per best_of_n run → run-unique worktree dirs +
    # branches, so two concurrent runs in the SAME cwd never collide (CC1).
    run_token = uuid.uuid4().hex[:8]

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
        futs = [ex.submit(_attempt, spec, cwd, base, i, runner, on_status,
                          run_token)
                for i in range(n)]
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"slug": "?", "score": 0, "why": str(exc),
                                "branch": None, "worktree": None, "ok": False,
                                "graded": False})

    # Deterministic winner selection (B1/B5). Sort key, all DESCENDING priority
    # except the final slug tie-break (ASCENDING for reproducibility):
    #   1. ok            — a real diff (ok=True) always beats a no-diff attempt;
    #                      a no-diff attempt is NEVER chosen over a real diff.
    #   2. graded        — a graded attempt beats an ungraded one (so a real
    #                      score wins when grading worked for at least one).
    #   3. score         — highest grade wins among graded attempts.
    #   4. slug          — stable, reproducible tie-break.
    # B5 fallback falls out naturally: when grading is unavailable for ALL
    # attempts, every ok=True attempt has graded=False/score=None, so the top
    # of the sort is simply an ok=True attempt (a real diff) rather than nothing.
    def _sort_key(r: dict):
        score = r.get("score")
        score = score if isinstance(score, (int, float)) else -1
        return (-(1 if r.get("ok") else 0),
                -(1 if r.get("graded") else 0),
                -score,
                str(r.get("slug") or ""))

    results.sort(key=_sort_key)
    winner = results[0] if results else {"slug": None, "score": 0,
                                         "why": "no attempts", "branch": None,
                                         "ok": False, "graded": False}

    merged = False
    merge_info = ""
    winner_real = bool(winner.get("ok") and winner.get("branch"))
    if winner_real:
        merged, merge_info = _merge_branch(cwd, base, winner["branch"])
        _update(None, winner["slug"], "won" if merged else "failed", on_status)

    # B2 — clean up losers + a SUCCESSFULLY-merged winner, but PRESERVE the
    # winner's branch + worktree when its merge FAILED so the work is
    # recoverable (the merge stderr is surfaced in ``review`` / ``merge_error``).
    # A merged winner's commits already live on ``base``; an all-failed run's
    # "winner" produced no diff, so nothing is worth keeping there.
    preserve_branch = winner["branch"] if (winner_real and not merged) else None
    for r in results:
        if preserve_branch and r.get("branch") == preserve_branch:
            continue
        _cleanup(cwd, r)

    attempts = [{"slug": r["slug"], "score": r["score"], "why": r["why"]}
                for r in results]
    any_ok = any(r.get("ok") for r in results)
    w_score = winner.get("score")
    score_str = "ungraded" if w_score is None else str(w_score)
    why_str = f" — {winner.get('why')}" if winner.get("why") else ""
    if not any_ok:
        review = f"best of {n}: all {n} attempts failed (no diff produced)"
    elif merged:
        review = (f"best of {n}: winner {winner.get('slug')} scored "
                  f"{score_str}{why_str}; merged")
    else:
        # Real diff but the merge failed — branch kept for recovery.
        review = (f"best of {n}: winner {winner.get('slug')} scored "
                  f"{score_str}{why_str}; merge FAILED (winner branch kept): "
                  f"{merge_info or 'unknown'}")
    return {
        "ok": bool(merged),
        "n": n,
        "winner": {"slug": winner.get("slug"), "score": w_score,
                   "why": winner.get("why"), "branch": preserve_branch},
        "attempts": attempts,
        "merge_error": (merge_info or "merge failed") if preserve_branch else None,
        "review": review,
    }


def stream_best_of_n(spec: str, cwd: str, n: int | None = None):
    """Streaming wrapper for the chat surface: yields SSE-ready dicts
    (``thought`` / ``subtask_update`` / ``message``) like
    ``parallel_subtasks.stream_parallel_team`` so a UI can show each attempt
    run → grade → win live."""
    import queue as _queue
    import threading as _threading

    n = _guard_n(n if n is not None else _default_n())
    yield {"type": "thought", "role": "system",
           "text": f"Running {n} independent attempts, grading each, "
                   f"keeping the best (max {_max_workers()} at once)…"}
    yield {"type": "subtasks", "items": [
        {"slug": f"bestof-{i}", "goal": spec, "status": "pending"}
        for i in range(n)]}

    q: "_queue.Queue" = _queue.Queue()
    result: dict = {}

    def on_status(slug, status, files=None):
        q.put({"type": "subtask_update", "slug": slug, "status": status})

    def _runner():
        try:
            result["agg"] = best_of_n(spec, cwd, n=n, on_status=on_status)
        except Exception as exc:  # noqa: BLE001
            result["err"] = str(exc)
        finally:
            q.put(None)

    t = _threading.Thread(target=_runner, name="best-of-n", daemon=True)
    t.start()
    while True:
        item = q.get()
        if item is None:
            break
        yield item

    if result.get("err"):
        yield {"type": "message", "text": f"Best-of-N run error: {result['err']}"}
        return
    agg = result.get("agg") or {}
    w = agg.get("winner") or {}
    yield {"type": "message", "text":
           f"**Best-of-{agg.get('n', n)} complete** — {agg.get('review', 'done')}.\n\n"
           + (f"Winner `{w.get('slug')}` (score {w.get('score')}) merged into the "
              f"workspace." if agg.get("ok") else
              "No attempt produced a mergeable result.")}


__all__ = ["best_of_n", "stream_best_of_n"]
