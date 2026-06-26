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


def _grade(spec: str, diff: str) -> dict:
    """LLM grader: score a diff against the spec, 0-100, with a one-line why.

    Returns ``{"score": int, "why": str}``. Parses strict JSON defensively and
    SOFT-FAILS to ``{"score": 0, "why": "grade failed"}`` on any error (no LLM,
    empty/garbage output, unparseable JSON) so one bad grade never sinks a run."""
    capped = (diff or "")[:_DIFF_CAP]
    try:
        from aiforge_core.llm import client
        out = client.complete("reviewer", [
            {"role": "system", "content": _GRADER_SYS},
            {"role": "user", "content": f"SPEC:\n{spec}\n\nDIFF:\n{capped}"}],
            max_tokens=300)
    except Exception as exc:  # noqa: BLE001 — grader is best-effort
        log.warning("best_of_n grade LLM call failed: %s", exc)
        return {"score": 0, "why": "grade failed"}
    return _parse_grade(out)


def _parse_grade(out: str | None) -> dict:
    """Pull ``{"score":int,"why":str}`` out of an LLM response defensively."""
    m = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not m:
        return {"score": 0, "why": "grade failed"}
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return {"score": 0, "why": "grade failed"}
    if not isinstance(obj, dict):
        return {"score": 0, "why": "grade failed"}
    try:
        score = int(obj.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    why = str(obj.get("why") or "").strip()[:200]
    return {"score": score, "why": why}


def _attempt(spec: str, repo: str, base: str, i: int, run_one,
             on_status=None) -> dict:
    """Run ONE independent attempt in its own worktree, then grade its diff.

    Returns ``{slug, score, why, branch, worktree, ok}``. A crash in the runner
    is caught (score 0) so it can't kill the whole batch."""
    slug = f"bestof-{i}"
    _update(None, slug, "running", on_status)
    try:
        wt, branch = _make_worktree(repo, base, slug)
    except Exception as exc:  # noqa: BLE001
        log.warning("best_of_n worktree add failed for %s: %s", slug, exc)
        _update(None, slug, "failed", on_status)
        return {"slug": slug, "score": 0, "why": f"worktree failed: {exc}",
                "branch": None, "worktree": None, "ok": False}

    try:
        res = run_one({"slug": slug, "goal": spec}, wt) or {}
        ran_ok = bool(res.get("ok", True))
    except Exception as exc:  # noqa: BLE001 — crash in the runner
        log.warning("best_of_n attempt %s crashed: %s", slug, exc)
        ran_ok = False

    _commit_all(wt, slug)
    # Diff of the attempt's branch vs base — what this attempt actually changed.
    diff = (_git(["diff", base, "HEAD"], wt).stdout or "")
    if not diff.strip() or not ran_ok:
        _update(None, slug, "failed", on_status)
        return {"slug": slug, "score": 0,
                "why": "no diff produced" if not diff.strip() else "runner failed",
                "branch": branch, "worktree": wt, "ok": False}

    _update(None, slug, "grading", on_status)
    graded = _grade(spec, diff)
    _update(None, slug, "graded", on_status)
    return {"slug": slug, "score": graded["score"], "why": graded["why"],
            "branch": branch, "worktree": wt, "ok": True}


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

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
        futs = [ex.submit(_attempt, spec, cwd, base, i, runner, on_status)
                for i in range(n)]
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"slug": "?", "score": 0, "why": str(exc),
                                "branch": None, "worktree": None, "ok": False})

    # Pick the highest score (ties → first/stable). An attempt that produced no
    # diff scores 0 and can still "win" only when EVERY attempt failed.
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    winner = results[0] if results else {"slug": None, "score": 0,
                                         "why": "no attempts", "branch": None}

    merged = False
    if winner.get("ok") and winner.get("branch"):
        ok, _info = _merge_branch(cwd, base, winner["branch"])
        merged = ok
        _update(None, winner["slug"], "won" if ok else "failed", on_status)

    # Discard every loser's worktree (and the winner's now-merged worktree too).
    for r in results:
        if r is winner and merged:
            _cleanup(cwd, r)            # winner merged → its worktree is spent
        elif r is not winner:
            _cleanup(cwd, r)

    attempts = [{"slug": r["slug"], "score": r["score"], "why": r["why"]}
                for r in results]
    any_ok = any(r.get("ok") for r in results)
    review = (
        f"best of {n}: winner {winner.get('slug')} "
        f"scored {winner.get('score', 0)}"
        + (f" — {winner.get('why')}" if winner.get("why") else "")
        + ("; merged" if merged else "; nothing merged")
        if any_ok else
        f"best of {n}: all {n} attempts failed (no diff produced)")
    return {
        "ok": bool(merged),
        "n": n,
        "winner": {"slug": winner.get("slug"), "score": winner.get("score", 0),
                   "why": winner.get("why")},
        "attempts": attempts,
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
