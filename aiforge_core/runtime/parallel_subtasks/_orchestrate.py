"""Sequential driver and run_parallel orchestration.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical).
Wave scheduling and recursion live in ``_orchestrate_waves``, the shared-worktree
driver in ``_orchestrate_shared``."""
from __future__ import annotations

import concurrent.futures
import os

from ._orchestrate_shared import (  # noqa: F401  # re-exported
    _branch_is_ahead,
    _cleanup_shared,
    _run_shared_worktree,
    _shared_integration,
    _shared_review,
    _shared_run_and_merge,
    _SharedRun,
)
from ._orchestrate_waves import (  # noqa: F401  # re-exported
    _GOAL_FILE_RE,
    _attempt_subtask,
    _attempt_with_retries,
    _decomp_retries,
    _files_of,
    _next_wave,
    _recurse_max,
    _recurse_subtask,
    _run_one_recursive,
    _run_wave_set,
    _shared_worktree_enabled,
    schedule_waves,
)


def _existing_source_digest(cwd: str, own_path: str, budget: int = 16000) -> str:
    """The REAL source files currently on disk (excluding this subtask's own file
    + tests), so a sequential worker builds against actual committed code instead
    of guessing an interface. Fenced, budget-capped."""
    own = os.path.basename(str(own_path or ""))
    parts: list[str] = []
    total = 0
    for rel, content in _gather_sources(cwd):
        b = os.path.basename(rel)
        if b == own or b.startswith("test_") or b.endswith("_test.py") \
           or "/tests/" in ("/" + rel) or b == "conftest.py":
            continue
        if not content.strip() or _SCAFFOLD_MARK in content:
            continue                                # skip empty / still-stub files
        block = f"### {rel}\n```\n{content}\n```"
        if total + len(block) > budget:
            continue
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def _sequential_order(subs: list) -> list:
    """Impl build order for sequential mode: fewest local imports first (leaf
    modules before the files that depend on them) so each worker sees its deps
    already built. Stable within a tier."""
    def _rank(s):
        p = s.get("path") or ""
        n = 0
        if p.endswith(".py"):
            try:
                # can't read cwd here; rank by declared api size as a proxy for
                # 'foundational' (fewer public symbols → likely a leaf/util)
                n = len(s.get("api") or [])
            except Exception:  # noqa: BLE001
                n = 0
        return n
    return sorted(subs, key=_rank)


def _status(on_status, slug, state: str, files=None) -> None:
    """Report a subtask's state, when the caller asked for reports."""
    if not on_status:
        return
    if files is None:
        on_status(slug, state)
    else:
        on_status(slug, state, files)


def _safe_run(run_one, s: dict, cwd: str) -> dict:
    try:
        return run_one(s, cwd)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _commit_all(cwd: str, message: str) -> None:
    _git(["add", "-A"], cwd)
    _git(["commit", "--no-edit", "-m", message], cwd)


def _write_test_subtasks(cwd: str, tests: list[dict], run_one, on_status,
                         should_cancel) -> int:
    """Write + commit the test files first (they are the executable spec). Not
    gated — tests alone fail to import until impls exist; that's the baseline.
    Returns how many were actually written: a test subtask whose agent failed
    (the model endpoint dropped) used to count as done regardless, so a run
    that built nothing still reported partial success."""
    written = 0
    for s in tests:
        if should_cancel and should_cancel():
            return written
        slug = s.get("slug")
        _status(on_status, slug, "running")
        res = _safe_run(run_one, s, cwd) or {}
        _commit_all(cwd, f"test: {slug}")
        ok = res.get("ok") is not False and bool(res.get("files") or res.get("ok"))
        written += int(ok)
        _status(on_status, slug, "done" if ok else "failed", res.get("files"))
    return written


def _revert_attempt(cwd: str) -> None:
    _git(["reset", "--hard", "HEAD"], cwd)
    _git(["clean", "-fd", "-e", ".aiforge-venv", "-e", ".aiforge-contracts"], cwd)


def _build_one_impl(cwd: str, s: dict, subs: list, run_one, prev_fails: int,
                    should_cancel, emit) -> tuple[bool, int, dict]:
    """One impl subtask, retried: commit when the failure count HOLDS or drops,
    revert when it rises. Returns ``(committed, fails_now, last_result)``."""
    slug = s.get("slug")
    retries = _retries()
    for attempt in range(retries):
        if should_cancel and should_cancel():
            break
        res = _safe_run(run_one, s, cwd)
        _prune_offplan_files(cwd, subs)      # drop any phantom file this step made
        _, out = _project_test_output(cwd)
        fails = _fail_count(out)
        if fails <= prev_fails:
            _commit_all(cwd, f"feat: {slug}")
            emit({"type": "tool", "role": slug, "name": "committed",
                  "args": {"status": ("tests can't run yet" if fails >= 999
                                      else f"{fails} failing")},
                  "result": {"ok": True, "files": (res or {}).get("files") or []}})
            return True, fails, res
        # regression → undo this attempt, retry with the error
        _revert_attempt(cwd)
        s["_retry_error"] = (out or "")[-1500:]
        emit({"type": "thought", "role": slug,
              "text": f"{slug} raised failures {prev_fails}→{fails} — reverted, "
                      f"retry {attempt + 1}/{retries}…"})
    return False, prev_fails, {}


def _build_impls(cwd: str, impls: list, subs: list, run_one, prev_fails: int,
                 on_status, should_cancel, emit) -> tuple[int, int]:
    """Every impl subtask in dependency order. Returns ``(done, failed)``."""
    done = failed = 0
    for s in impls:
        if should_cancel and should_cancel():
            break
        slug = s.get("slug")
        _status(on_status, slug, "running")
        s["_existing_files"] = _existing_source_digest(cwd, s.get("path"))
        s["_tests"] = _matching_tests_for(cwd, s.get("path") or "")
        committed, prev_fails, res = _build_one_impl(
            cwd, s, subs, run_one, prev_fails, should_cancel, emit)
        if committed:
            done += 1
            _status(on_status, slug, "done", (res or {}).get("files"))
        else:
            failed += 1
            _status(on_status, slug, "failed")
    return done, failed


def _run_sequential(cwd: str, _base_branch: str, subs: list, run_one, *,
                    on_status=None, should_cancel=None, emit=None) -> dict:
    """SINGLE-BRANCH SEQUENTIAL build (Coordinator + dependent sub-agents). Each
    subtask runs directly in ``cwd`` — seeing the REAL prior committed files, so
    no isolated worker guesses an interface for code that doesn't exist yet. After
    each: run the tests; if the failure count didn't RISE, git-commit (lock in
    progress); if it regressed, git reset --hard (undo). Git is the undo/redo
    stack; monotonic progress, no merges/conflicts."""
    def _e(ev):
        if emit:
            emit(ev)

    tests = [s for s in subs if _is_test_subtask(s)]
    impls = _sequential_order([s for s in subs if not _is_test_subtask(s)])
    wrote = _write_test_subtasks(cwd, tests, run_one, on_status, should_cancel)

    # Baseline fail count with tests present, impls not yet built. Prune any
    # off-plan files first so the tree matches the plan.
    _prune_offplan_files(cwd, subs)
    _, out = _project_test_output(cwd)
    prev_fails = _fail_count(out)
    _e({"type": "thought", "role": "coordinator",
        "text": f"Sequential build — baseline {prev_fails} failing. Building "
                f"{len(impls)} module(s) one at a time, committing each that holds "
                "or improves the score…"})

    # Each impl in dep order, seeing the REAL prior files; commit or revert.
    done, failed = _build_impls(cwd, impls, subs, run_one, prev_fails,
                                on_status, should_cancel, _e)
    failed += len(tests) - wrote
    return {"ok": failed == 0, "total": len(subs), "done": done + wrote,
            "failed": failed}


def _dispatch_batch(batch: list[dict], *, repo_root, base_branch, ticket_id,
                    run_one, validate_one, on_status, run_token,
                    should_cancel) -> list[dict]:
    """Run one batch of subtasks concurrently, each in its own worktree."""
    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
        futs = [ex.submit(_run_subtask, repo_root, base_branch, ticket_id, s,
                          run_one, validate_one, on_status, run_token,
                          should_cancel)
                for s in batch]
        for f in concurrent.futures.as_completed(futs):
            # On Stop, cancel every still-queued (not-yet-started) future so no
            # further subtask agent kicks off.
            if should_cancel is not None and should_cancel():
                for pf in futs:
                    pf.cancel()
            try:
                out.append(f.result())
            except concurrent.futures.CancelledError:
                continue
            except Exception as exc:  # noqa: BLE001
                out.append({"slug": "?", "ok": False, "error": str(exc)})
    return out


def _rerun_rounds() -> int:
    try:
        return max(0, min(5, int(os.environ.get(
            "AIFORGE_PARALLEL_RERUN_ROUNDS", "3"))))
    except ValueError:
        return 1


def _run_with_restarts(subs: list[dict], should_cancel, **kw) -> list[dict]:
    """Orchestrator-level RESTART rounds: after the first pass, re-dispatch the
    still-failed subtasks in fresh worktrees (transient failures / contention
    often clear on a retry). Bounded by AIFORGE_PARALLEL_RERUN_ROUNDS."""
    by_slug = {r.get("slug"): r
               for r in _dispatch_batch(subs, should_cancel=should_cancel, **kw)}
    for _ in range(_rerun_rounds()):
        if should_cancel is not None and should_cancel():
            break
        failed = [s for s in subs if not (by_slug.get(s["slug"]) or {}).get("ok")]
        if not failed:
            break
        log.info("orchestrator re-run round: %d failed subtask(s)", len(failed))
        for r in _dispatch_batch(failed, should_cancel=should_cancel, **kw):
            by_slug[r.get("slug")] = r      # latest result wins
    return [by_slug[s["slug"]] for s in subs if s["slug"] in by_slug]


def _merge_results(repo_root: str, base_branch: str, ticket_id, subs: list[dict],
                   results: list[dict], on_status) -> tuple[int, list[str], list[str]]:
    """Sequential merge in the planner's original order (dependencies first).
    Returns ``(merged, conflict_slugs, conflict_details)`` — the details carry
    git's stderr rather than swallowing it (B3)."""
    merged = 0
    conflicts: list[str] = []
    details: list[str] = []
    order = {s.get("slug"): i for i, s in enumerate(subs)}
    for r in sorted([r for r in results if r.get("ok") and r.get("branch")],
                    key=lambda r: order.get(r["slug"], 99)):
        ok, info = _merge_branch(repo_root, base_branch, r["branch"])
        if ok:
            merged += 1
        else:
            conflicts.append(r["slug"])
            details.append(f"{r['slug']}: {info}")
            _update(ticket_id, r["slug"], "failed", on_status)
    return merged, conflicts, details


def _cleanup_worktrees(repo_root: str, results: list[dict]) -> None:
    """ALWAYS clean up worktrees + branches — even if a merge raised — so a
    crashed run can't leak worktree dirs + metadata unbounded."""
    for r in results:
        wt = r.get("worktree")
        if wt and os.path.isdir(wt):
            _git(["worktree", "remove", "--force", wt], repo_root)
        if r.get("branch"):
            _git(["branch", "-D", r["branch"]], repo_root)
    _git(["worktree", "prune"], repo_root)


def _run_integration(repo_root: str, ticket_id, integration_test) -> dict:
    """FINAL integration test — after all the merges, build + test the WHOLE
    thing on the base branch. Individually-green subtasks can still break when
    combined; this is the "is the total task actually done?" gate."""
    try:
        integration = integration_test(repo_root) or {"ok": False}
    except Exception as exc:  # noqa: BLE001
        integration = {"ok": False, "error": str(exc)}
    _emit(ticket_id, "*", "integration_test",
          f"integration {'passed' if integration.get('ok') else 'FAILED'}",
          {"ok": integration.get("ok")})
    return integration


def _review_line(subs, done, validated, failed, conflicts, conflict_details,
                 integration, all_ok) -> str:
    if all_ok:
        return (f"all {len(subs)} subtasks done + validated"
                + ("; integration green" if integration.get("ok") else ""))
    return (f"{done}/{len(subs)} done ({validated} validated), {failed} failed"
            + (f", {len(conflicts)} merge conflict(s)" if conflicts else "")
            + (" — " + "; ".join(conflict_details) if conflict_details else "")
            + ("; integration FAILED" if integration.get("ok") is False else ""))


def run_parallel(repo_root: str, base_branch: str, ticket_id: int | None,
                 subtasks: list[dict], run_one, *, validate_one=None,
                 integration_test=None, on_status=None, merge: bool = True,
                 should_cancel=None) -> dict:
    """Run ``subtasks`` concurrently (each in its own worktree), VALIDATE each
    (build/tests green), then merge the validated branches into ``base_branch``
    sequentially. Returns an aggregate incl. a review summary.

    With AIFORGE_SHARED_WORKTREE=1 (OPT-IN; default OFF) this delegates to the
    shared-worktree sequential scheduler (P2); on any error it falls back to the
    per-worktree path below so a scheduler bug can never brick a run.
    """
    import uuid as _uuid
    subs = [s for s in (subtasks or []) if isinstance(s, dict) and s.get("slug")]
    if subs and _shared_worktree_enabled():
        try:
            return _run_shared_worktree(
                repo_root, base_branch, ticket_id, subs, run_one, validate_one,
                on_status, _uuid.uuid4().hex[:8], should_cancel, merge,
                integration_test)
        except Exception as exc:  # noqa: BLE001
            log.warning("shared-worktree scheduler failed (%s) — falling back "
                        "to per-worktree", exc)
    if not subs:
        return {"ok": True, "total": 0, "done": 0, "failed": 0, "validated": 0,
                "merged": 0, "conflicts": [], "note": "no subtasks",
                "review": "nothing to do"}

    results = _run_with_restarts(
        subs, should_cancel,
        repo_root=repo_root, base_branch=base_branch, ticket_id=ticket_id,
        run_one=run_one, validate_one=validate_one, on_status=on_status,
        # ONE run-unique token per run → run-unique worktree dirs + branches, so
        # concurrent parallel runs sharing this repo never collide (CC1).
        run_token=_uuid.uuid4().hex[:8])

    # B3 — warn (don't block) if the base tree is dirty before we merge into it.
    warnings = [w for w in [(_dirty_warning(repo_root) if merge else None)] if w]
    merged, conflicts, conflict_details = 0, [], []
    try:
        if merge:
            merged, conflicts, conflict_details = _merge_results(
                repo_root, base_branch, ticket_id, subs, results, on_status)
    finally:
        _cleanup_worktrees(repo_root, results)

    done = sum(1 for r in results if r.get("ok"))
    validated = sum(1 for r in results if r.get("validated"))
    failed = len(subs) - done
    integration: dict = {"ok": None, "skipped": True}
    if merge and merged and integration_test is not None:
        integration = _run_integration(repo_root, ticket_id, integration_test)

    all_ok = (not conflicts and done == len(subs)
              and integration.get("ok") is not False)
    return {"ok": all_ok,
            "total": len(subs), "done": done, "validated": validated,
            "failed": failed, "merged": merged, "conflicts": conflicts,
            "conflict_details": conflict_details, "warnings": warnings,
            "integration": integration,
            "review": _review_line(subs, done, validated, failed, conflicts,
                                   conflict_details, integration, all_ok),
            "results": results}

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._contracts import _is_test_subtask, _matching_tests_for
from ._reconcile import (
    _SCAFFOLD_MARK,
    _fail_count,
    _gather_sources,
    _project_test_output,
    _prune_offplan_files,
)
from ._worktree import (  # noqa: F401  # read via _pkg() or by tests
    _build_or_test,
    _dirty_warning,
    _emit,
    _git,
    _max_workers,
    _merge_branch,
    _retries,
    _run_subtask,
    _update,
    log,
)


def _decompose(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._decompose(*a, **k)
