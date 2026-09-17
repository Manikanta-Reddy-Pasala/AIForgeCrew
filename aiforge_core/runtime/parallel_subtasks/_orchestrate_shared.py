"""Running a batch in one shared worktree and merging it back."""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS


def _pkg():
    """``_orchestrate``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_orchestrate``; patch any other
    name on this module."""
    import aiforge_core.runtime.parallel_subtasks._orchestrate as package
    return package


def _branch_is_ahead(repo_root: str, base_branch: str, branch: str) -> bool:
    """Does ``branch`` hold work base doesn't?

    True whether the stragglers commit landed OR the doers already committed
    milestones inside the shared tree. (A clean ``git commit`` returns non-zero,
    so we must NOT key off its exit code or we'd delete a branch that holds doer
    commits.) Unknowable → True: never lose work.
    """
    try:
        ahead = _pkg()._git(["rev-list", "--count", f"{base_branch}..{branch}"],
                     repo_root)
        return int((ahead.stdout or "0").strip() or "0") > 0
    except Exception:  # noqa: BLE001
        return True


def _shared_integration(wt: str, integration_test) -> dict:
    """ONE integration build+test on the combined tree (P5 verify)."""
    if integration_test is None:
        return _pkg()._build_or_test(wt)
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
    pkg = _pkg()
    if wt and os.path.isdir(wt):
        pkg._git(["worktree", "remove", "--force", wt], repo_root)
    if keep:
        log.warning("shared-worktree %s — KEEPING branch %s (holds subtask "
                    "work, NOT merged into base; inspect/re-merge manually)",
                    "CANCELLED" if cancelled else "merge conflict", branch)
    else:
        pkg._git(["branch", "-D", branch], repo_root)
    pkg._git(["worktree", "prune"], repo_root)


def _shared_review(subs, done, cancelled, conflicts, integ) -> str:
    return (("STOPPED — " if cancelled else "")
            + f"shared worktree: {done}/{len(subs)} subtasks done"
            + ("; integration green" if integ.get("ok") else "")
            + ("; integration FAILED" if integ.get("ok") is False else "")
            + ("; MERGE CONFLICT" if conflicts else "")
            + ("; partial work kept on branch, NOT merged" if cancelled else ""))


@dataclass(frozen=True)
class _SharedRun:
    """Everything one shared-worktree run needs, in one value.

    These fourteen arguments always travelled together — where the tree is,
    which branch it came from, and the five callables that drive it — and
    threading them through positionally made the call site a line nobody could
    read without counting commas against the signature.
    """
    repo_root: str
    base_branch: str
    branch: str
    wt: str
    ticket_id: str
    run_one: Callable
    validate_one: Callable
    on_status: Callable | None
    should_cancel: Callable | None
    merge: bool
    integration_test: Callable | None


def _shared_run_and_merge(run: _SharedRun, subs, results: dict,
                          state: dict) -> None:
    """Run every wave in the shared tree, commit, then integrate + merge.
    Leaves conflicts / merged / cancelled / committed / integ in ``state``."""
    pkg = _pkg()
    repo_root, base_branch = run.repo_root, run.base_branch
    branch, wt, ticket_id = run.branch, run.wt, run.ticket_id
    should_cancel, merge = run.should_cancel, run.merge
    integration_test = run.integration_test
    pkg._run_wave_set(wt, subs, run.run_one, run.validate_one, run.on_status,
                  ticket_id, should_cancel, results, 0)
    # ALWAYS commit the subtask work onto the shared branch FIRST — the caller's
    # `finally` force-removes the worktree, which would DISCARD anything left
    # uncommitted (incl. earlier waves that already succeeded). Commit even on
    # cancel so the kept branch actually holds the work; we just skip MERGING
    # partial work into base.
    pkg._git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], wt)
    pkg._git(["commit", "-m", "shared-worktree subtasks"], wt)  # no-op if clean
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
        _shared_run_and_merge(
            _SharedRun(repo_root=repo_root, base_branch=base_branch,
                       branch=branch, wt=wt, ticket_id=ticket_id,
                       run_one=run_one, validate_one=validate_one,
                       on_status=on_status, should_cancel=should_cancel,
                       merge=merge, integration_test=integration_test),
            subs, results, state)
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


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import _make_worktree, _merge_branch, log
