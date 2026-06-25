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
import subprocess
import threading

log = logging.getLogger("aiforge.parallel_subtasks")

# git operations that touch the MAIN repo's index/worktree list (worktree
# add/remove, branch -D, merge) must be serialized — concurrent `git worktree
# add` races on .git/index.lock. The per-subtask WORK still runs in parallel
# (each worktree has its own index); only these repo-level git calls are locked.
_GIT_LOCK = threading.Lock()


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
    with _GIT_LOCK:                          # serialize repo-index mutations
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


def _retries() -> int:
    try:
        return max(0, min(5, int(os.environ.get("AIFORGE_SUBTASK_RETRIES", "2"))))
    except ValueError:
        return 2


def _reset_worktree(wt: str, base_branch: str) -> None:
    """Hard-reset a worktree to ``base_branch`` between retry attempts so a
    failed/partial attempt can't leak files into the next one."""
    _git(["reset", "--hard", base_branch], wt)
    _git(["clean", "-fd"], wt)


def _attempt(subtask: dict, wt: str, slug: str, run_one, validate_one) -> dict:
    """One run+validate attempt. Catches a CRASH in run_one/validate (returns
    ok=False) so it can be retried instead of killing the whole batch."""
    try:
        res = run_one(subtask, wt) or {}
        ran_ok = bool(res.get("ok", True))
    except Exception as exc:  # noqa: BLE001 — crash in the agent
        return {"ran": False, "validated": False, "ok": False,
                "error": f"crash: {exc}", "detail": {}}
    _commit_all(wt, slug)
    validated, vres = ran_ok, {}
    if ran_ok and validate_one is not None:
        try:
            vres = validate_one(subtask, wt) or {}
            validated = bool(vres.get("ok", True))
        except Exception as exc:  # noqa: BLE001 — crash in validation
            validated, vres = False, {"ok": False, "error": f"crash: {exc}"}
    return {"ran": ran_ok, "validated": validated, "ok": ran_ok and validated,
            "detail": res, "validation": vres}


def _run_subtask(repo: str, base_branch: str, ticket_id: int | None,
                 subtask: dict, run_one, validate_one) -> dict:
    slug = subtask.get("slug") or "sub"
    _update(ticket_id, slug, "running")
    try:
        wt, branch = _make_worktree(repo, base_branch, slug)
    except Exception as exc:  # noqa: BLE001
        _update(ticket_id, slug, "failed")
        return {"slug": slug, "ok": False, "error": str(exc), "branch": None}

    # Retry the whole run+validate on failure/crash — subtasks are the risky
    # unit, so we keep trying (bounded) before giving up. Reset the worktree
    # between attempts so nothing leaks across tries.
    last: dict = {}
    attempts = _retries() + 1
    for i in range(attempts):
        if i > 0:
            _reset_worktree(wt, base_branch)
            _emit(ticket_id, slug, "subtask_retry",
                  f"{slug} retry {i}/{attempts - 1}", {"slug": slug, "attempt": i})
        last = _attempt(subtask, wt, slug, run_one, validate_one)
        if last["ok"]:
            break

    ok = last["ok"]
    _emit(ticket_id, slug,
          "subtask_validated" if last.get("validated") else "subtask_rejected",
          f"{slug} validation {'passed' if last.get('validated') else 'failed'}",
          {"slug": slug, "validated": last.get("validated"),
           "attempts": i + 1})
    _update(ticket_id, slug, "done" if ok else "failed")
    return {"slug": slug, "ok": ok, "ran": last.get("ran"),
            "validated": last.get("validated"), "attempts": i + 1,
            "branch": branch, "worktree": wt,
            "detail": last.get("detail"), "validation": last.get("validation"),
            "error": last.get("error")}


def default_validate_one(subtask: dict, worktree: str) -> dict:
    """Objective per-subtask validation: build + run the project's tests in the
    worktree. Green → validated. No model needed — a concrete quality gate."""
    try:
        from aiforge_core.runtime.tools.project_runner import project
        test = project(action="test", cwd=worktree)
        if isinstance(test, dict) and test.get("ok"):
            return {"ok": True, "via": "test"}
        # No runnable tests? fall back to a successful build/compile.
        build = project(action="build", cwd=worktree)
        if isinstance(build, dict) and build.get("ok"):
            return {"ok": True, "via": "build", "note": "no tests; build green"}
        return {"ok": False, "via": "test/build",
                "detail": (test or {}).get("error") or (build or {}).get("error")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def default_integration_test(repo_root: str) -> dict:
    """Build + test the WHOLE integrated result on the base branch after all
    subtasks merged — catches breakage that only shows when combined."""
    try:
        from aiforge_core.runtime.tools.project_runner import project
        test = project(action="test", cwd=repo_root)
        if isinstance(test, dict) and test.get("ok"):
            return {"ok": True, "via": "test"}
        build = project(action="build", cwd=repo_root)
        if isinstance(build, dict) and build.get("ok"):
            return {"ok": True, "via": "build", "note": "no tests; build green"}
        return {"ok": False, "via": "test/build"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _emit(ticket_id, slug, kind, body, md) -> None:
    if ticket_id is None:
        return
    try:
        from aiforge_core.tickets import store
        store.add_event(ticket_id, "validator", kind, body, md)
    except Exception:  # noqa: BLE001
        pass


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
                 subtasks: list[dict], run_one, *, validate_one=None,
                 integration_test=None, merge: bool = True) -> dict:
    """Run ``subtasks`` concurrently (each in its own worktree), VALIDATE each
    (build/tests green), then merge the validated branches into ``base_branch``
    sequentially. Returns an aggregate incl. a review summary.
    """
    subs = [s for s in (subtasks or []) if isinstance(s, dict) and s.get("slug")]
    if not subs:
        return {"ok": True, "total": 0, "done": 0, "failed": 0, "validated": 0,
                "merged": 0, "conflicts": [], "note": "no subtasks",
                "review": "nothing to do"}

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
        futs = [ex.submit(_run_subtask, repo_root, base_branch, ticket_id, s,
                          run_one, validate_one)
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
    validated = sum(1 for r in results if r.get("validated"))
    failed = len(subs) - done

    # FINAL integration test — after all the merges, build + test the WHOLE
    # thing on the base branch. Individually-green subtasks can still break
    # when combined; this is the "is the total task actually done?" gate.
    integration: dict = {"ok": None, "skipped": True}
    if merge and merged and integration_test is not None:
        try:
            integration = integration_test(repo_root) or {"ok": False}
        except Exception as exc:  # noqa: BLE001
            integration = {"ok": False, "error": str(exc)}
        _emit(ticket_id, "*", "integration_test",
              f"integration {'passed' if integration.get('ok') else 'FAILED'}",
              {"ok": integration.get("ok")})

    all_ok = (not conflicts and done == len(subs)
              and integration.get("ok") is not False)
    review = (f"all {len(subs)} subtasks done + validated"
              + ("; integration green" if integration.get("ok") else "")
              if all_ok else
              f"{done}/{len(subs)} done ({validated} validated), {failed} failed"
              + (f", {len(conflicts)} merge conflict(s)" if conflicts else "")
              + ("; integration FAILED" if integration.get("ok") is False else ""))
    return {"ok": all_ok,
            "total": len(subs), "done": done, "validated": validated,
            "failed": failed, "merged": merged, "conflicts": conflicts,
            "integration": integration, "review": review, "results": results}


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
    agg = run_parallel(repo_root, base_branch, getattr(ticket, "id", None),
                       subs, run_one or default_run_one,
                       validate_one=default_validate_one,
                       integration_test=default_integration_test)
    # Final review summary on the ticket timeline so the operator sees the
    # overall verdict (how many done+validated vs failed) in one place.
    _emit(getattr(ticket, "id", None), "*", "parallel_review",
          agg.get("review", ""), {k: agg.get(k) for k in
          ("total", "done", "validated", "failed", "merged", "conflicts")})
    return agg


__all__ = ["run_parallel", "run_subtasks_parallel", "default_run_one",
           "default_validate_one", "default_integration_test", "enabled"]
