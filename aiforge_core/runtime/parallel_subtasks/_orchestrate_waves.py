"""Scheduling subtasks into waves and retrying or recursing into a failed subtask."""
from __future__ import annotations

import os
import re


def _pkg():
    """``_orchestrate``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_orchestrate``; patch any other
    name on this module."""
    import aiforge_core.runtime.parallel_subtasks._orchestrate as package
    return package


# ─────────── Shared-worktree scheduler (P2) + recursion (P3) ───────────────
# Instead of one worktree PER subtask + a merge step, run every subtask in ONE
# shared worktree, ordered into WAVES: deps first, and within a wave only
# file-DISJOINT subtasks run in parallel (file-sharing ones serialize — we
# never merge two edits to the SAME file). A subtask that itself fails big is
# decomposed ONE level deeper and its sub-agents run under the same scheduler
# (bounded by AIFORGE_DECOMP_MAX_DEPTH). Guarded by AIFORGE_SHARED_WORKTREE
# (default on); any failure falls back to the per-worktree path in
# ``_orchestrate.run_parallel``.


def _shared_worktree_enabled() -> bool:
    # OPT-IN (default OFF). The per-worktree path (``run_parallel``) is the tested,
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
    pkg = _pkg()
    r = pkg._attempt_subtask(sub, wt, run_one, validate_one)
    tries = 0
    while (not r.get("ok") and tries < _decomp_retries()
           and not (should_cancel and should_cancel())):
        tries += 1
        sub["_retry_error"] = str(r.get("error") or "")[:800]
        pkg._emit(ticket_id, slug, "retry",
              f"retry {tries}/{_decomp_retries()} (depth {depth}) — "
              f"{str(r.get('error') or '')[:120]}", {})
        r = pkg._attempt_subtask(sub, wt, run_one, validate_one)
    sub.pop("_retry_error", None)
    return r


def _recurse_subtask(sub, wt, run_one, validate_one, on_status, ticket_id,
                     should_cancel, depth: int, slug) -> dict | None:
    """Decompose a persistently-failing subtask one level deeper and run its
    sub-agents under the same scheduler. None when recursion does not apply."""
    pkg = _pkg()
    if depth + 1 >= _recurse_max() or (should_cancel and should_cancel()):
        return None
    children = pkg._decompose(sub.get("goal") or sub.get("title") or "")
    if len(children) < 2:
        return None
    for i, c in enumerate(children):
        c["slug"] = f"{slug}.{i + 1}"
        c["_depth"] = depth + 1
    pkg._emit(ticket_id, slug, "recurse",
          f"subtask too big — split into {len(children)} sub-agents", {})
    child_results: dict = {}
    pkg._run_wave_set(wt, children, run_one, validate_one, on_status,
                  ticket_id, should_cancel, child_results, depth + 1)
    ok = bool(child_results) and all(cr.get("ok")
                                     for cr in child_results.values())
    pkg._update(ticket_id, slug, "done" if ok else "failed", on_status)
    return {"ok": ok, "slug": slug, "recursed": True, "children": len(children)}


def _run_one_recursive(sub, wt, run_one, validate_one, on_status, ticket_id,
                       should_cancel, depth: int) -> dict:
    """Run ONE subtask in the shared worktree ``wt``, VALIDATE it (build/tests
    green via ``validate_one`` when given) with N informed retries
    (AIFORGE_DECOMP_RETRIES), and on persistent failure decompose it one level
    deeper and run its sub-agents under the same scheduler — each sub-agent also
    gets the retry loop (P3, depth-capped). Returns ``{ok, slug, ...}``."""
    pkg = _pkg()
    slug = sub.get("slug")
    pkg._update(ticket_id, slug, "running", on_status)
    r = pkg._attempt_with_retries(sub, wt, run_one, validate_one, ticket_id, slug,
                              depth, should_cancel)
    if r.get("ok"):
        pkg._update(ticket_id, slug, "done", on_status)
        return {**r, "slug": slug}
    recursed = pkg._recurse_subtask(sub, wt, run_one, validate_one, on_status,
                                ticket_id, should_cancel, depth, slug)
    if recursed is not None:
        return recursed
    pkg._update(ticket_id, slug, "failed", on_status)
    return {**r, "slug": slug}


def _run_wave_set(wt, subs, run_one, validate_one, on_status, ticket_id,
                  should_cancel, results: dict, depth: int) -> None:
    """Execute subtasks in ``wt`` SEQUENTIALLY in wave (dependency) order. A
    SHARED worktree has ONE git index, so parallel subtasks would race on
    ``.git/index`` and on build output dirs — sequential is the only safe order
    here. (Parallelism lives in the per-worktree path, where each subtask has
    its own index.) Fills ``results`` slug→result."""
    pkg = _pkg()
    for wave in pkg.schedule_waves(subs):
        for s in wave:
            if should_cancel and should_cancel():
                return
            results[s["slug"]] = pkg._run_one_recursive(
                s, wt, run_one, validate_one, on_status, ticket_id,
                should_cancel, depth)
