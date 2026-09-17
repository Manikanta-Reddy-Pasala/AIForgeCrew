"""Before the team runs: planning subtasks, writing the spec, preparing the
tree and announcing the run."""
from __future__ import annotations

import os

from aiforge_core.runtime import review_gates

from ._stream_changes import (
    _ensure_test_coverage,
)


def _pkg():
    """``_stream``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_stream``; patch any other
    name on this module."""
    import aiforge_core.runtime.parallel_subtasks._stream as package
    return package


def _plan_subtasks(prompt: str, subtasks, state: dict):
    """Decompose (if needed) and repair the plan. Leaves the final list in
    ``state['subs']``; an empty list means the caller should bail out."""
    subs = subtasks
    if not subs:
        yield {"type": "thought", "role": "planner",
               "text": "Decomposing into parallel subtasks…"}
        subs = _pkg()._decompose(prompt)
    if len(subs) < 2:
        # The caller normally falls back to sequential team mode before reaching
        # here; this is the last-resort guard.
        yield {"type": "message", "text":
               "Couldn't split this into parallel subtasks — running normally."}
        state["subs"] = []
        return
    # Backstop: guarantee test coverage so the build can be verified +
    # self-healed even when the planner omitted tests.
    subs = _ensure_test_coverage(subs)
    # Decomposition consistency: every per-module test needs a matching impl file
    # (test_board→board, BookServiceTest→BookService). When the architect
    # collapses impl into one file but writes per-module tests, add the missing
    # impl modules.
    before = len(subs)
    subs = _ensure_impl_modules(subs)
    if len(subs) > before:
        added = [s.get("path") for s in subs[before:]]
        yield {"type": "thought", "role": "planner",
               "text": f"Decomposition fix — {len(subs) - before} test(s) target "
                       f"modules with no impl file; added: {', '.join(added)}"}
    # FILE-OWNERSHIP ENFORCEMENT (don't trust the plan — check with code). Two
    # subtasks owning the SAME file = two agents editing it in parallel = the #1
    # cause of worktree merge conflicts. Fold duplicates into one owner so each
    # file has exactly one author.
    subs, dupes = _enforce_disjoint_files(subs)
    if dupes:
        yield {"type": "thought", "role": "planner",
               "text": f"File-ownership check — folded {dupes} overlapping "
                       "subtask(s) so no two agents edit the same file (conflict "
                       "prevention)."}
    # PLAN REVIEW — a different model checks the file manifest for typos
    # (kvdakade→kvfacade), near-duplicate/missing modules, scope creep BEFORE any
    # code is built (a patch-reconcile can't fix a structural naming error later).
    try:
        subs, note = review_gates.review_plan(prompt, subs)
        if note:
            yield {"type": "thought", "role": "reviewer", "text": f"🔍 {note}"}
    except Exception as exc:  # noqa: BLE001
        log.debug("plan review skipped: %s", exc)
    state["subs"] = subs
    yield {"type": "subtasks", "items": [
        {"slug": s.get("slug") or f"sub-{i+1}",
         "goal": s.get("goal") or "", "status": "pending"}
        for i, s in enumerate(subs)]}


def _write_spec(prompt: str, subs: list, cwd: str, state: dict):
    """Requirements/plan document: persist the enhanced spec + the subtask
    breakdown to SPEC.md in the workspace BEFORE any subtask runs. It's the
    single source of truth — fed into every per-subtask fresh context (so each
    isolated context knows the overall goal) and re-read by the final
    verification pass to confirm nothing was dropped."""
    pkg = _pkg()
    spec_md = pkg._render_spec_md(prompt, subs)
    # SPEC REVIEW — check the spec before any code is built (contradictions,
    # ambiguity, missing cases, scope creep). Refines it if needed.
    try:
        spec_md, note = review_gates.review_spec(prompt, spec_md)
        if note:
            yield {"type": "thought", "role": "reviewer", "text": f"🔍 {note}"}
    except Exception as exc:  # noqa: BLE001
        log.debug("spec review skipped: %s", exc)
    state["spec_md"] = spec_md
    try:
        with open(os.path.join(cwd, pkg._SPEC_MD), "w", encoding="utf-8") as fh:
            fh.write(spec_md)
        yield {"type": "thought", "role": "planner",
               "text": f"Wrote SPEC.md ({len(subs)} subtasks) — the shared "
                       "requirements doc each subtask builds against."}
    except Exception as exc:  # noqa: BLE001
        # A silent skip here is how runs ended up spec-less with no trace
        # (unwritable cwd etc.) — surface it so the operator can fix the cause.
        log.warning("SPEC.md write failed in %s: %s", cwd, exc)
        yield {"type": "thought", "role": "planner",
               "text": f"⚠ SPEC.md write failed ({exc}) — subtasks still get "
                       "the spec in-context, but nothing is persisted to disk."}


def _prepare_tree(cwd: str, subs: list):
    """Record the pre-existing code so greenfield-only steps (scaffold, off-plan
    prune) never touch an EXISTING repo — on a real repo they'd delete the whole
    codebase (everything not in this task's small plan) — then scaffold when the
    tree really is empty."""
    pkg = _pkg()
    preexisting = pkg._snapshot_baseline(cwd)
    if not pkg._is_greenfield(cwd):
        yield {"type": "thought", "role": "system",
               "text": f"Existing repo ({preexisting} source files) — editing in "
                       "place; skipping scaffold + off-plan prune (greenfield-only)."}
        return
    # SCAFFOLD — deterministically create every file at its canonical path (stub
    # + API-contract header) BEFORE parallelizing, then commit to base so
    # worktrees branch from a fixed tree. GREENFIELD ONLY (stubbing over an
    # existing repo is wrong). Gated (default on).
    if os.environ.get("AIFORGE_SCAFFOLD", "1") in ("0", "false"):
        return
    try:
        stubs = pkg._scaffold_stubs(cwd, subs)
    except Exception as exc:  # noqa: BLE001
        log.debug("scaffold skipped: %s", exc)
        return
    if stubs:
        yield {"type": "tool", "role": "planner", "name": "scaffolded project",
               "args": {}, "result": {"files": stubs}}


def _announce_execution(subs: list):
    """OBSERVABILITY — surface the effective execution config so a regression is
    VISIBLE (e.g. a stray AIFORGE_SEQUENTIAL=1 forcing 1-at-a-time, or the
    reviewer model missing). Silent config drift is what made "why only 1?" hard."""
    sequential = os.environ.get("AIFORGE_SEQUENTIAL", "0") not in ("0", "false")
    mode = ("SEQUENTIAL (1 at a time)" if sequential
            else f"parallel, up to {_pkg()._max_workers()} at once")
    try:
        reviewer = (review_gates.pick_reviewer_model()
                    or "same model (no 2nd model loaded)")
    except Exception:  # noqa: BLE001
        reviewer = "?"
    yield {"type": "thought", "role": "system",
           "text": f"Running {len(subs)} subtasks — each in its OWN fresh context "
                   f"+ git worktree · execution: {mode} · reviewer: {reviewer}."}


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._reconcile import _enforce_disjoint_files, _ensure_impl_modules
from ._worktree import log
