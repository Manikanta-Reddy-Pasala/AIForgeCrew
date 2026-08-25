"""Wave scheduling, sequential + shared-worktree drivers, run_parallel orchestration.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import threading

from pydantic import BaseModel

from aiforge_core.runtime import review_gates
from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS, ensure_artifact_gitignore

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
                import ast as _ast
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
                         should_cancel) -> None:
    """Write + commit the test files first (they are the executable spec). Not
    gated — tests alone fail to import until impls exist; that's the baseline."""
    for s in tests:
        if should_cancel and should_cancel():
            return
        slug = s.get("slug")
        _status(on_status, slug, "running")
        res = _safe_run(run_one, s, cwd)
        _commit_all(cwd, f"test: {slug}")
        _status(on_status, slug, "done", (res or {}).get("files"))


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
    _write_test_subtasks(cwd, tests, run_one, on_status, should_cancel)

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
    return {"ok": failed == 0, "total": len(subs), "done": done + len(tests),
            "failed": failed}


# ─────────── Shared-worktree scheduler (P2) + recursion (P3) ───────────────
# Instead of one worktree PER subtask + a merge step, run every subtask in ONE
# shared worktree, ordered into WAVES: deps first, and within a wave only
# file-DISJOINT subtasks run in parallel (file-sharing ones serialize — we
# never merge two edits to the SAME file). A subtask that itself fails big is
# decomposed ONE level deeper and its sub-agents run under the same scheduler
# (bounded by AIFORGE_DECOMP_MAX_DEPTH). Guarded by AIFORGE_SHARED_WORKTREE
# (default on); any failure falls back to the per-worktree path below.


def _shared_worktree_enabled() -> bool:
    # OPT-IN (default OFF). The per-worktree path (below) is the tested,
    # parallel-safe default: each subtask gets its OWN git index, so parallel
    # commits never race. A SHARED worktree cannot run subtasks in parallel
    # safely — they'd contend on one .git/index — so shared mode runs
    # SEQUENTIALLY (deps order), useful when subtasks must see each other's
    # files in one tree. Enable with AIFORGE_SHARED_WORKTREE=1.
    return os.environ.get("AIFORGE_SHARED_WORKTREE", "0").strip().lower() \
        in ("1", "true", "yes", "on")


# `<file.ext>:` anywhere in the goal — .search (not .match) so a leading verb
# ("update config.yaml: add key") still recovers the file. Requires a
# dotted-extension token before the colon, so "Refactor: split" stays empty.
_GOAL_FILE_RE = re.compile(r"\b([\w./\-]+\.\w+)\s*:")


def _files_of(s: dict) -> set:
    """The file set a subtask owns — for dependency/disjointness reasoning.
    Prefer explicit ``files`` / ``scope_allowlist_globs`` / ``path``; else
    recover the target from the ``goal`` (the decomposer's ``<file>: <what>``
    convention), so planner/decompose subtasks (which carry only slug+goal)
    aren't treated as owning NOTHING."""
    raw = (s.get("files") or s.get("scope_allowlist_globs")
           or ([s["path"]] if s.get("path") else []))
    if not raw:
        m = _GOAL_FILE_RE.search(str(s.get("goal") or ""))
        if m:
            raw = [m.group(1)]
    return {str(x) for x in raw if x}


def schedule_waves(subs: list[dict]) -> list[list[dict]]:
    """Order subtasks into execution WAVES for a shared worktree.

    - deps respected: a subtask runs only after every dep slug has completed;
    - within a wave, subtasks are pairwise file-DISJOINT (safe to run parallel
      in one tree). A subtask sharing a file with one already picked for the
      wave is deferred to a later wave (serialized — no same-file merge).
    Cycle/unknown-dep safe: if nothing is ready, force the first remaining
    subtask so the loop always makes progress.
    """
    remaining = [s for s in subs if isinstance(s, dict) and s.get("slug")]
    slugs = {s.get("slug") for s in remaining}
    done: set = set()
    waves: list[list[dict]] = []
    guard = 0
    while remaining and guard < 10000:
        guard += 1
        wave = _next_wave(remaining, done, slugs)
        for s in wave:
            done.add(s.get("slug"))
            remaining.remove(s)
        waves.append(wave)
    if remaining:                           # safety net: serialize leftovers
        waves.extend([[s] for s in remaining])
    return waves


def _next_wave(remaining: list[dict], done: set, slugs: set) -> list[dict]:
    """The subtasks that may run together now: deps satisfied and pairwise
    file-disjoint. A subtask sharing a file with one already picked is deferred
    to a later wave (serialized — no same-file merge)."""
    ready = [s for s in remaining
             if all(d in done or d not in slugs for d in (s.get("deps") or []))]
    if not ready:                           # dep cycle → force progress
        ready = [remaining[0]]
    wave: list[dict] = []
    used: set = set()
    for s in ready:
        files = _files_of(s)
        if files and (files & used):        # shares a file → next wave
            continue
        wave.append(s)
        used |= files
    return wave


def _recurse_max() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_DECOMP_MAX_DEPTH", "2")))
    except (TypeError, ValueError):
        return 2


def _decomp_retries() -> int:
    """Retries per subtask/sub-agent before it decomposes into deeper
    sub-agents. Applies at every recursion level."""
    try:
        return max(0, int(os.environ.get("AIFORGE_DECOMP_RETRIES", "2")))
    except (TypeError, ValueError):
        return 2


def _attempt_subtask(sub, wt, run_one, validate_one) -> dict:
    """One attempt at a subtask, gated on REAL validation (compile/tests) when
    a validator is supplied — "the agent emitted a final answer" is NOT "it
    works"."""
    try:
        rr = run_one(sub, wt) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not (rr.get("ok") and validate_one is not None):
        return rr
    try:
        v = validate_one(sub, wt) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"validate: {exc}"}
    if v.get("ok") is False:
        return {"ok": False, "error": v.get("error") or "validation failed",
                "validated": False}
    return {**rr, "validated": True}


def _attempt_with_retries(sub, wt, run_one, validate_one, ticket_id, slug,
                          depth: int, should_cancel) -> dict:
    """Informed retry loop — applies at EVERY level (each sub-agent also runs
    through this function), so a failing sub-agent retries too. Each retry feeds
    the prior failure back into the prompt (a blind identical re-run on a
    deterministic endpoint is a no-op). Count via AIFORGE_DECOMP_RETRIES
    (default 2); depth is threaded into the status so the UI shows nesting."""
    r = _attempt_subtask(sub, wt, run_one, validate_one)
    tries = 0
    while (not r.get("ok") and tries < _decomp_retries()
           and not (should_cancel and should_cancel())):
        tries += 1
        sub["_retry_error"] = str(r.get("error") or "")[:800]
        _emit(ticket_id, slug, "retry",
              f"retry {tries}/{_decomp_retries()} (depth {depth}) — "
              f"{str(r.get('error') or '')[:120]}", {})
        r = _attempt_subtask(sub, wt, run_one, validate_one)
    sub.pop("_retry_error", None)
    return r


def _recurse_subtask(sub, wt, run_one, validate_one, on_status, ticket_id,
                     should_cancel, depth: int, slug) -> dict | None:
    """Decompose a persistently-failing subtask one level deeper and run its
    sub-agents under the same scheduler. None when recursion does not apply."""
    if depth + 1 >= _recurse_max() or (should_cancel and should_cancel()):
        return None
    children = _decompose(sub.get("goal") or sub.get("title") or "")
    if len(children) < 2:
        return None
    for i, c in enumerate(children):
        c["slug"] = f"{slug}.{i + 1}"
        c["_depth"] = depth + 1
    _emit(ticket_id, slug, "recurse",
          f"subtask too big — split into {len(children)} sub-agents", {})
    child_results: dict = {}
    _run_wave_set(wt, children, run_one, validate_one, on_status,
                  ticket_id, should_cancel, child_results, depth + 1)
    ok = bool(child_results) and all(cr.get("ok")
                                     for cr in child_results.values())
    _update(ticket_id, slug, "done" if ok else "failed", on_status)
    return {"ok": ok, "slug": slug, "recursed": True, "children": len(children)}


def _run_one_recursive(sub, wt, run_one, validate_one, on_status, ticket_id,
                       should_cancel, depth: int) -> dict:
    """Run ONE subtask in the shared worktree ``wt``, VALIDATE it (build/tests
    green via ``validate_one`` when given) with N informed retries
    (AIFORGE_DECOMP_RETRIES), and on persistent failure decompose it one level
    deeper and run its sub-agents under the same scheduler — each sub-agent also
    gets the retry loop (P3, depth-capped). Returns ``{ok, slug, ...}``."""
    slug = sub.get("slug")
    _update(ticket_id, slug, "running", on_status)
    r = _attempt_with_retries(sub, wt, run_one, validate_one, ticket_id, slug,
                              depth, should_cancel)
    if r.get("ok"):
        _update(ticket_id, slug, "done", on_status)
        return {**r, "slug": slug}
    recursed = _recurse_subtask(sub, wt, run_one, validate_one, on_status,
                                ticket_id, should_cancel, depth, slug)
    if recursed is not None:
        return recursed
    _update(ticket_id, slug, "failed", on_status)
    return {**r, "slug": slug}


def _run_wave_set(wt, subs, run_one, validate_one, on_status, ticket_id,
                  should_cancel, results: dict, depth: int) -> None:
    """Execute subtasks in ``wt`` SEQUENTIALLY in wave (dependency) order. A
    SHARED worktree has ONE git index, so parallel subtasks would race on
    ``.git/index`` and on build output dirs — sequential is the only safe order
    here. (Parallelism lives in the per-worktree path, where each subtask has
    its own index.) Fills ``results`` slug→result."""
    for wave in schedule_waves(subs):
        for s in wave:
            if should_cancel and should_cancel():
                return
            results[s["slug"]] = _run_one_recursive(
                s, wt, run_one, validate_one, on_status, ticket_id,
                should_cancel, depth)


def _branch_is_ahead(repo_root: str, base_branch: str, branch: str) -> bool:
    """Does ``branch`` hold work base doesn't?

    True whether the stragglers commit landed OR the doers already committed
    milestones inside the shared tree. (A clean ``git commit`` returns non-zero,
    so we must NOT key off its exit code or we'd delete a branch that holds doer
    commits.) Unknowable → True: never lose work.
    """
    try:
        ahead = _git(["rev-list", "--count", f"{base_branch}..{branch}"],
                     repo_root)
        return int((ahead.stdout or "0").strip() or "0") > 0
    except Exception:  # noqa: BLE001
        return True


def _shared_integration(wt: str, integration_test) -> dict:
    """ONE integration build+test on the combined tree (P5 verify)."""
    if integration_test is None:
        return _build_or_test(wt)
    try:
        return integration_test(wt) or {"ok": False}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _cleanup_shared(repo_root: str, wt: str, branch: str, keep: bool,
                    cancelled: bool) -> None:
    """Remove the worktree; KEEP the branch only when it holds UNMERGED work
    worth inspecting — a cancel or a merge conflict, AND something was actually
    committed. Otherwise (clean merge, merge-off, or a mid-run exception before
    the commit) delete it: there's nothing on it, and run_parallel's fallback
    re-runs everything under fresh branches."""
    if wt and os.path.isdir(wt):
        _git(["worktree", "remove", "--force", wt], repo_root)
    if keep:
        log.warning("shared-worktree %s — KEEPING branch %s (holds subtask "
                    "work, NOT merged into base; inspect/re-merge manually)",
                    "CANCELLED" if cancelled else "merge conflict", branch)
    else:
        _git(["branch", "-D", branch], repo_root)
    _git(["worktree", "prune"], repo_root)


def _shared_review(subs, done, cancelled, conflicts, integ) -> str:
    return (("STOPPED — " if cancelled else "")
            + f"shared worktree: {done}/{len(subs)} subtasks done"
            + ("; integration green" if integ.get("ok") else "")
            + ("; integration FAILED" if integ.get("ok") is False else "")
            + ("; MERGE CONFLICT" if conflicts else "")
            + ("; partial work kept on branch, NOT merged" if cancelled else ""))


def _shared_run_and_merge(repo_root, base_branch, branch, wt, subs, run_one,
                          validate_one, on_status, ticket_id, should_cancel,
                          merge, integration_test, results: dict,
                          state: dict) -> None:
    """Run every wave in the shared tree, commit, then integrate + merge.
    Leaves conflicts / merged / cancelled / committed / integ in ``state``."""
    _run_wave_set(wt, subs, run_one, validate_one, on_status, ticket_id,
                  should_cancel, results, 0)
    # ALWAYS commit the subtask work onto the shared branch FIRST — the caller's
    # `finally` force-removes the worktree, which would DISCARD anything left
    # uncommitted (incl. earlier waves that already succeeded). Commit even on
    # cancel so the kept branch actually holds the work; we just skip MERGING
    # partial work into base.
    _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], wt)
    _git(["commit", "-m", "shared-worktree subtasks"], wt)  # no-op if clean
    state["committed"] = _branch_is_ahead(repo_root, base_branch, branch)
    if should_cancel and should_cancel():
        # Stop pressed mid-run: keep the committed branch, but do NOT
        # integrate/merge PARTIAL work into base.
        state["cancelled"] = True
        state["integ"] = {"ok": None, "skipped": True, "cancelled": True}
        return
    state["integ"] = _shared_integration(wt, integration_test)
    if not merge:
        return
    merge_ok, _info = _merge_branch(repo_root, base_branch, branch)
    if merge_ok:
        state["merged"] = 1
    else:
        state["conflicts"].append("shared")


def _run_shared_worktree(repo_root, base_branch, ticket_id, subs, run_one,
                         validate_one, on_status, run_token, should_cancel,
                         merge, integration_test) -> dict:
    """Run all subtasks in ONE shared worktree (waves), build+test the whole
    tree ONCE, then merge the single shared branch. No per-subtask worktrees,
    no cross-branch merge of same-file edits."""
    wt, branch = _make_worktree(repo_root, base_branch, "shared", run_token)
    results: dict = {}
    state = {"conflicts": [], "merged": 0, "cancelled": False,
             "committed": False, "integ": {"ok": None, "skipped": True}}
    try:
        _shared_run_and_merge(repo_root, base_branch, branch, wt, subs, run_one,
                              validate_one, on_status, ticket_id, should_cancel,
                              merge, integration_test, results, state)
    finally:
        kept = state["committed"] and (bool(state["conflicts"])
                                       or state["cancelled"])
        _cleanup_shared(repo_root, wt, branch, kept, state["cancelled"])
    conflicts = state["conflicts"]
    merged = state["merged"]
    cancelled = state["cancelled"]
    integ = state["integ"]

    ordered = [results.get(s["slug"], {"ok": False, "slug": s["slug"]})
               for s in subs]
    done = sum(1 for r in ordered if r.get("ok"))
    all_ok = (not cancelled and done == len(subs) and not conflicts
              and integ.get("ok") is not False)
    return {"ok": all_ok, "total": len(subs), "done": done, "validated": done,
            "failed": len(subs) - done, "merged": merged, "conflicts": conflicts,
            "cancelled": cancelled,
            "conflict_details": ([f"kept branch {branch}"] if kept else []),
            "warnings": [], "integration": integ,
            "kept_branch": (branch if kept else None),
            "review": _shared_review(subs, done, cancelled, conflicts, integ),
            "results": ordered, "mode": "shared_worktree"}


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
from ._reconcile import (_SCAFFOLD_MARK, _fail_count, _gather_sources, _project_test_output,
                         _prune_offplan_files)
from ._worktree import (_build_or_test, _dirty_warning, _emit, _git, _make_worktree, _max_workers,
                        _merge_branch, _retries, _run_subtask, _update, log)
def _decompose(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._decompose(*a, **k)
