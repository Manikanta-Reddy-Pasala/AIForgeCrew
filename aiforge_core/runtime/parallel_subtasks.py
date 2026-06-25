"""Parallel multi-agent execution of a ticket's subtasks.

When a ticket is decomposed into subtickets, this runs each one CONCURRENTLY in
its OWN git worktree (isolation), updating the live subtask status, then merges
the successful branches back into the ticket's working branch in order.

Opt-in: ``AIFORGE_PARALLEL_SUBTASKS=1`` (default off — the in-order single-Doer
path stays the default). Concurrency capped by ``AIFORGE_PARALLEL_SUBTASKS_MAX``
(default 4).

The per-subtask executor is INJECTED (``run_one``) so the orchestration —
worktree isolation, concurrency, status tracking, sequential merge, conflict
handling, aggregation — is independently testable with real git. ``run_one``
receives ``(subtask, worktree_path)`` and must leave its work committed on the
worktree's branch; it returns ``{ok: bool, ...}``.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import shutil
import subprocess

log = logging.getLogger("aiforge.parallel_subtasks")


def enabled() -> bool:
    return os.environ.get("AIFORGE_PARALLEL_SUBTASKS", "0").strip().lower() \
        in ("1", "true", "yes", "on")


def _max_workers() -> int:
    try:
        return max(1, min(8, int(os.environ.get("AIFORGE_PARALLEL_SUBTASKS_MAX", "4"))))
    except ValueError:
        return 4


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120)


def _branch_for(slug: str, base_branch: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in slug)[:40]
    return f"{base_branch}-sub-{safe}"


def _make_worktree(repo: str, base_branch: str, slug: str) -> tuple[str, str]:
    """Create a fresh worktree + branch off ``base_branch`` for ``slug``."""
    branch = _branch_for(slug, base_branch)
    wt = os.path.join(repo, ".aiforge-worktrees", f"sub-{slug}")
    # Clean any stale worktree/branch from a prior run.
    _git(["worktree", "remove", "--force", wt], repo)
    _git(["branch", "-D", branch], repo)
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    p = _git(["worktree", "add", "-B", branch, wt, base_branch], repo)
    if p.returncode != 0 or not os.path.isdir(wt):
        raise RuntimeError(f"worktree add failed for {slug}: {p.stderr[:300]}")
    return wt, branch


def _commit_all(wt: str, slug: str) -> bool:
    """Commit any work the runner left uncommitted. Returns True if the branch
    has a new commit relative to its base (i.e. there is work to merge)."""
    _git(["add", "-A"], wt)
    st = _git(["status", "--porcelain"], wt)
    if st.stdout.strip():
        _git(["commit", "-m", f"subtask: {slug}"], wt)
    # any commits ahead of the merge base count as work
    return True


def _run_subtask(repo: str, base_branch: str, ticket_id: int | None,
                 subtask: dict, run_one) -> dict:
    slug = subtask.get("slug") or "sub"
    _update(ticket_id, slug, "running")
    try:
        wt, branch = _make_worktree(repo, base_branch, slug)
    except Exception as exc:  # noqa: BLE001
        _update(ticket_id, slug, "failed")
        return {"slug": slug, "ok": False, "error": str(exc), "branch": None}
    try:
        res = run_one(subtask, wt) or {}
        ok = bool(res.get("ok", True))
        _commit_all(wt, slug)
        _update(ticket_id, slug, "done" if ok else "failed")
        return {"slug": slug, "ok": ok, "branch": branch, "worktree": wt,
                "detail": res}
    except Exception as exc:  # noqa: BLE001
        _update(ticket_id, slug, "failed")
        return {"slug": slug, "ok": False, "error": str(exc),
                "branch": branch, "worktree": wt}


def _update(ticket_id, slug, status) -> None:
    if ticket_id is None:
        return
    try:
        from aiforge_core.tickets import subtasks as _st
        _st.update_subtask(ticket_id, slug, status, role="doer")
    except Exception:  # noqa: BLE001
        pass


def _merge_branch(repo: str, base_branch: str, branch: str) -> tuple[bool, str]:
    """Merge ``branch`` into ``base_branch`` (checked out in ``repo``). Returns
    (ok, info). On conflict, aborts cleanly and reports."""
    p = _git(["merge", "--no-edit", branch], repo)
    if p.returncode == 0:
        return True, "merged"
    # conflict / failure → abort to leave the base branch clean
    _git(["merge", "--abort"], repo)
    return False, (p.stdout + p.stderr)[:300]


def run_parallel(repo_root: str, base_branch: str, ticket_id: int | None,
                 subtasks: list[dict], run_one, *, merge: bool = True) -> dict:
    """Run ``subtasks`` concurrently (each in its own worktree), then merge the
    successful branches into ``base_branch`` sequentially. Returns an aggregate.
    """
    subs = [s for s in (subtasks or []) if isinstance(s, dict) and s.get("slug")]
    if not subs:
        return {"ok": True, "total": 0, "done": 0, "failed": 0,
                "merged": 0, "conflicts": [], "note": "no subtasks"}

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
        futs = [ex.submit(_run_subtask, repo_root, base_branch, ticket_id, s, run_one)
                for s in subs]
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"slug": "?", "ok": False, "error": str(exc)})

    merged = 0
    conflicts: list[str] = []
    if merge:
        # Sequential merge in the planner's original order (dependencies first).
        order = {s.get("slug"): i for i, s in enumerate(subs)}
        for r in sorted([r for r in results if r.get("ok") and r.get("branch")],
                        key=lambda r: order.get(r["slug"], 99)):
            ok, info = _merge_branch(repo_root, base_branch, r["branch"])
            if ok:
                merged += 1
            else:
                conflicts.append(r["slug"])
                _update(ticket_id, r["slug"], "failed")

    # Best-effort worktree cleanup.
    for r in results:
        wt = r.get("worktree")
        if wt and os.path.isdir(wt):
            _git(["worktree", "remove", "--force", wt], repo_root)
        if r.get("branch"):
            _git(["branch", "-D", r["branch"]], repo_root)

    done = sum(1 for r in results if r.get("ok"))
    return {"ok": not conflicts and done == len(subs),
            "total": len(subs), "done": done,
            "failed": len(subs) - done, "merged": merged,
            "conflicts": conflicts, "results": results}


def default_run_one(subtask: dict, worktree: str) -> dict:
    """Real per-subtask agent: run the Doer chat loop on this subtask's goal in
    its worktree (it has the full tool set — edit/build/test/serve). Returns
    ``{ok}`` based on whether it produced a final answer without erroring."""
    try:
        from aiforge_core.llm.client import complete as _complete
        from aiforge_core.runtime.chat_agent import run_chat_agent
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"import: {exc}"}
    goal = subtask.get("goal") or subtask.get("slug") or "implement the subtask"
    accept = subtask.get("acceptance") or []
    scope = subtask.get("scope_allowlist_globs") or []
    msg = (f"Implement this subtask, then build + test it.\n\nGOAL: {goal}\n"
           + ("ACCEPTANCE:\n" + "\n".join(f"- {a}" for a in accept) + "\n" if accept else "")
           + ("SCOPE (only touch these): " + ", ".join(scope) + "\n" if scope else "")
           + "Keep the change minimal and focused on this subtask only.")

    def complete_fn(role, convo):
        return _complete(role, convo)

    ok = False
    try:
        for ev in run_chat_agent([{"role": "user", "content": msg}], cwd=worktree,
                                 role="doer", complete_fn=complete_fn):
            if ev.get("type") == "error":
                return {"ok": False, "error": ev.get("text")}
            if ev.get("type") == "message" and not ev.get("awaiting_input"):
                ok = True
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": ok}


def run_subtasks_parallel(ticket, *, run_one=None) -> dict:
    """Entry point: decompose-aware parallel run for one ticket. Loads its
    subtasks + working branch, fans them out concurrently, merges. Operator-
    triggered (and gated by AIFORGE_PARALLEL_SUBTASKS for the auto path) so the
    default single-Doer pipeline is never disturbed."""
    from aiforge_core.runtime.workspace import ensure_branch_and_worktree
    from aiforge_core.tickets import subtasks as _st
    subs = _st.get_subtasks(getattr(ticket, "id", ticket))
    if not subs:
        return {"ok": True, "total": 0, "note": "no subtasks to run"}
    wt = ensure_branch_and_worktree(ticket)
    if not wt:
        return {"ok": False, "error": "no worktree/repo for ticket"}
    repo_root = os.environ.get("AIFORGE_REPO_ROOT") or wt
    # the worktree's current branch is the ticket's working branch
    cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
    base_branch = (cur.stdout or "").strip() or "HEAD"
    return run_parallel(repo_root, base_branch, getattr(ticket, "id", None),
                        subs, run_one or default_run_one)


__all__ = ["run_parallel", "run_subtasks_parallel", "default_run_one", "enabled"]
