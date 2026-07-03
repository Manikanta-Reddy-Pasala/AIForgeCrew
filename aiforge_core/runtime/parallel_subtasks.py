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
import re
import subprocess
import threading

from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS, ensure_artifact_gitignore

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
    # Explicit operator override always wins (a batching server — vLLM / TGI —
    # genuinely serves concurrent requests, so its operator sets this higher).
    raw = os.environ.get("AIFORGE_PARALLEL_SUBTASKS_MAX")
    if raw is not None:
        try:
            return max(1, min(8, int(raw)))
        except ValueError:
            return 4
    # Default: on a LOCAL single-model endpoint (mlx-lm / ollama / llama.cpp /
    # LM Studio) serving requests SERIALLY, fanning out N Doer calls just queues
    # them on one model — zero latency win, plus N× worktree + KV-cache thrash.
    # Run subtasks sequentially there (still isolated worktrees, no false
    # parallelism). A remote/cloud (or batching) endpoint keeps the fan-out.
    try:
        from aiforge_core.llm import router as _router
        if _router.is_local_endpoint("doer"):
            return 1
    except Exception:  # noqa: BLE001
        pass
    return 4


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120)


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "step"


def _branch_for(slug: str, base_branch: str, run_token: str | None = None) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in slug)[:40]
    # ``run_token`` makes the branch RUN-UNIQUE so concurrent runs in the SAME
    # repo don't collide on a fixed ``{base}-sub-{slug}`` name (CC1).
    if run_token:
        return f"{base_branch}-{run_token}-sub-{safe}"
    return f"{base_branch}-sub-{safe}"


def _make_worktree(repo: str, base_branch: str, slug: str,
                   run_token: str | None = None) -> tuple[str, str]:
    """Create a fresh worktree + branch off ``base_branch`` for ``slug``.

    ``run_token`` (a short uuid4 hex per run) makes BOTH the worktree dir and
    the branch run-unique so two concurrent parallel / best-of-N runs sharing
    one repo can't destroy each other's in-flight worktree (CC1). The ``slug``
    itself is unchanged (still used for display/status)."""
    branch = _branch_for(slug, base_branch, run_token)
    name = f"{run_token}-{slug}" if run_token else f"sub-{slug}"
    wt = os.path.join(repo, ".aiforge-worktrees", name)
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
    # Excludes keep .aiforge-worktrees/ + junk out even though this runs in
    # an isolated worktree (touched-path tracking isn't shared across the
    # per-subtask worktrees, so excludes are the right guard here).
    _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], wt)
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
    _git(["clean", "-fdx"], wt)        # -x also clears ignored files a failed
    #                                    attempt may have left (full isolation)


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
                 subtask: dict, run_one, validate_one, on_status=None,
                 run_token: str | None = None) -> dict:
    slug = subtask.get("slug") or "sub"
    _update(ticket_id, slug, "running", on_status)
    try:
        wt, branch = _make_worktree(repo, base_branch, slug, run_token)
    except Exception as exc:  # noqa: BLE001
        _update(ticket_id, slug, "failed", on_status)
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
    _files = (last.get("detail") or {}).get("files") if isinstance(last.get("detail"), dict) else None
    _update(ticket_id, slug, "done" if ok else "failed", on_status, _files)
    return {"slug": slug, "ok": ok, "ran": last.get("ran"),
            "validated": last.get("validated"), "attempts": i + 1,
            "branch": branch, "worktree": wt,
            "detail": last.get("detail"), "validation": last.get("validation"),
            "error": last.get("error")}


def _build_or_test(worktree: str) -> dict:
    """Quality gate for a checkout: if the project HAS tests, gate strictly on
    the test result (FAILING tests do NOT pass via a build fallback); only when
    there are NO tests do we accept a green build. No project → nothing to gate.
    """
    try:
        from aiforge_core.runtime.tools.project_runner import (
            _has_tests, detect, project,
        )
        stacks = (detect(worktree) or {}).get("stacks") or []
        if not stacks:
            return {"ok": True, "via": "no-project", "note": "nothing to build/test"}
        if _has_tests(worktree, stacks):
            test = project(action="test", cwd=worktree)
            ok = bool(isinstance(test, dict) and test.get("ok"))
            return {"ok": ok, "via": "test",
                    "detail": None if ok else (test or {}).get("error")}
        build = project(action="build", cwd=worktree)
        ok = bool(isinstance(build, dict) and build.get("ok"))
        return {"ok": ok, "via": "build", "note": "no tests",
                "detail": None if ok else (build or {}).get("error")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def default_validate_one(subtask: dict, worktree: str) -> dict:
    """Per-subtask validation = COMPILE/BUILD only.

    A subtask runs in an ISOLATED single-file worktree, so it can't pass
    cross-file tests (db.py alone has no tests; test_app.py alone imports files
    that live in other subtasks' worktrees). Gating each subtask on the full
    test suite would fail every one. So per-subtask we only check the written
    code COMPILES; the integration test (after merge, all files together) runs
    the real test suite. Set AIFORGE_PARALLEL_STRICT_VALIDATE=1 to test per
    subtask instead."""
    if os.environ.get("AIFORGE_PARALLEL_STRICT_VALIDATE", "0") in ("1", "true"):
        return _build_or_test(worktree)
    try:
        from aiforge_core.runtime.tools.project_runner import detect, project
        stacks = (detect(worktree) or {}).get("stacks") or []
        if not stacks:
            return {"ok": True, "via": "no-project"}
        build = project(action="build", cwd=worktree)
        ok = bool(isinstance(build, dict) and build.get("ok"))
        return {"ok": ok, "via": "build-only",
                "detail": None if ok else (build or {}).get("error")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def default_integration_test(repo_root: str) -> dict:
    """Build + test the WHOLE integrated result on the base branch after all
    subtasks merged — catches breakage that only shows when combined. Like the
    per-subtask gate, FAILING tests do not pass via a build fallback."""
    return _build_or_test(repo_root)


def _emit(ticket_id, slug, kind, body, md) -> None:
    if ticket_id is None:
        return
    try:
        from aiforge_core.tickets import store
        store.add_event(ticket_id, "validator", kind, body, md)
    except Exception:  # noqa: BLE001
        pass


def _update(ticket_id, slug, status, on_status=None, files=None) -> None:
    # Persist to the ticket (chart) AND/OR stream to a live consumer (chat SSE).
    # ``files`` (on done) lets the consumer show what the worker produced.
    if on_status is not None:
        try:
            on_status(slug, status, files)
        except TypeError:
            on_status(slug, status)   # back-compat 2-arg callbacks
        except Exception:  # noqa: BLE001
            pass
    if ticket_id is None:
        return
    try:
        from aiforge_core.tickets import subtasks as _st
        _st.update_subtask(ticket_id, slug, status, role="doer")
    except Exception:  # noqa: BLE001
        pass


def _dirty_warning(cwd: str) -> str | None:
    """B3 — warn (don't block) when ``cwd`` has uncommitted changes (EXCLUDING
    the agent's own artifacts) that a winner/branch merge could collide with.

    Returns a clear operator-facing message, or None when the tree is clean /
    the check itself fails. Best-effort: the artifact pathspecs are excluded so
    a stray ``.aiforge`` file never trips the warning."""
    try:
        # ``.gitignore`` is excluded: _ensure_git_workspace appends the
        # agent-artifact lines via ensure_artifact_gitignore BEFORE this check,
        # so on every default run the tree shows ` M .gitignore` and would
        # falsely warn. The agent's own gitignore edit isn't an operator change.
        st = _git(["status", "--porcelain", "--", ".", *_EXCLUDE_PATHSPECS,
                   ":(exclude).gitignore"], cwd)
    except Exception:  # noqa: BLE001
        return None
    if (st.stdout or "").strip():
        return ("workspace has uncommitted changes — merge may fail; "
                "commit or stash first")
    return None


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
                 integration_test=None, on_status=None, merge: bool = True) -> dict:
    """Run ``subtasks`` concurrently (each in its own worktree), VALIDATE each
    (build/tests green), then merge the validated branches into ``base_branch``
    sequentially. Returns an aggregate incl. a review summary.
    """
    subs = [s for s in (subtasks or []) if isinstance(s, dict) and s.get("slug")]
    if not subs:
        return {"ok": True, "total": 0, "done": 0, "failed": 0, "validated": 0,
                "merged": 0, "conflicts": [], "note": "no subtasks",
                "review": "nothing to do"}

    # ONE run-unique token per run → run-unique worktree dirs + branches, so
    # concurrent parallel runs sharing this repo never collide (CC1).
    import uuid as _uuid
    run_token = _uuid.uuid4().hex[:8]

    def _pass(batch: list[dict]) -> list[dict]:
        out: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
            futs = [ex.submit(_run_subtask, repo_root, base_branch, ticket_id, s,
                              run_one, validate_one, on_status, run_token)
                    for s in batch]
            for f in concurrent.futures.as_completed(futs):
                try:
                    out.append(f.result())
                except Exception as exc:  # noqa: BLE001
                    out.append({"slug": "?", "ok": False, "error": str(exc)})
        return out

    # Orchestrator-level RESTART rounds: after the first pass, re-dispatch the
    # still-failed subtasks in fresh worktrees (transient failures / contention
    # often clear on a retry). Bounded by AIFORGE_PARALLEL_RERUN_ROUNDS (1).
    by_slug: dict = {}
    for r in _pass(subs):
        by_slug[r.get("slug")] = r
    try:
        rounds = max(0, min(3, int(os.environ.get("AIFORGE_PARALLEL_RERUN_ROUNDS", "1"))))
    except ValueError:
        rounds = 1
    for _ in range(rounds):
        failed = [s for s in subs if not (by_slug.get(s["slug"]) or {}).get("ok")]
        if not failed:
            break
        log.info("orchestrator re-run round: %d failed subtask(s)", len(failed))
        for r in _pass(failed):
            by_slug[r.get("slug")] = r      # latest result wins
    results: list[dict] = [by_slug[s["slug"]] for s in subs if s["slug"] in by_slug]

    # B3 — warn (don't block) if the base tree is dirty before we merge into it.
    warnings: list[str] = []
    if merge:
        _dirty = _dirty_warning(repo_root)
        if _dirty:
            warnings.append(_dirty)

    merged = 0
    conflicts: list[str] = []
    conflict_details: list[str] = []   # surface git stderr, don't swallow it (B3)
    try:
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
                    conflict_details.append(f"{r['slug']}: {info}")
                    _update(ticket_id, r["slug"], "failed", on_status)
    finally:
        # ALWAYS clean up worktrees + branches — even if a merge raised — so a
        # crashed run can't leak worktree dirs + metadata unbounded.
        for r in results:
            wt = r.get("worktree")
            if wt and os.path.isdir(wt):
                _git(["worktree", "remove", "--force", wt], repo_root)
            if r.get("branch"):
                _git(["branch", "-D", r["branch"]], repo_root)
        _git(["worktree", "prune"], repo_root)

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
              + (" — " + "; ".join(conflict_details) if conflict_details else "")
              + ("; integration FAILED" if integration.get("ok") is False else ""))
    return {"ok": all_ok,
            "total": len(subs), "done": done, "validated": validated,
            "failed": failed, "merged": merged, "conflicts": conflicts,
            "conflict_details": conflict_details, "warnings": warnings,
            "integration": integration, "review": review, "results": results}


def default_run_one(subtask: dict, worktree: str, spec_md: str = "") -> dict:
    """Real per-subtask agent: run the Doer chat loop on this subtask's goal in
    its worktree (it has the full tool set — edit/build/test/serve) in a FRESH
    context — only this subtask's goal (+ the shared spec) is loaded, so a big
    multi-subtask build never exhausts one context. Returns ``{ok}`` based on
    whether it produced a final answer without erroring.

    ``spec_md`` (optional) is the shared requirements/plan document; it's given
    to every subtask so each fresh context knows the overall goal + how its slice
    fits, without carrying the other subtasks' conversation history."""
    try:
        from aiforge_core.llm.client import complete as _complete
        from aiforge_core.runtime.chat_agent import run_chat_agent
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"import: {exc}"}
    goal = subtask.get("goal") or subtask.get("slug") or "implement the subtask"
    accept = subtask.get("acceptance") or []
    scope = subtask.get("scope_allowlist_globs") or []
    msg = (
        (f"PROJECT SPEC (shared context — build YOUR slice to fit it):\n{spec_md.strip()[:6000]}\n\n---\n\n"
         if spec_md and spec_md.strip() else "")
        + f"Implement this subtask, then build + test it.\n\nGOAL: {goal}\n"
        + ("ACCEPTANCE:\n" + "\n".join(f"- {a}" for a in accept) + "\n" if accept else "")
        + ("SCOPE (only touch these): " + ", ".join(scope) + "\n" if scope else "")
        + "Keep the change focused on THIS subtask only; other subtasks handle the rest.")

    def complete_fn(role, convo):
        return _complete(role, convo)

    ok = False
    try:
        for ev in run_chat_agent([{"role": "user", "content": msg}], cwd=worktree,
                                 role="doer", complete_fn=complete_fn):
            if ev.get("type") == "error":
                return {"ok": False, "error": ev.get("text")}
            if ev.get("type") == "message" and not ev.get("awaiting_input"):
                # The runaway-safety-cap stop also emits a plain message — that's
                # a FAILURE (the Doer thrashed without finishing), not success.
                ok = not (ev.get("text") or "").startswith("(stopped:")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": ok}


_FILE_BLOCK_RE = None  # lazy-compiled in _parse_file_blocks


def _parse_file_blocks(text: str) -> dict:
    """Parse ``=== path/to/file ===\\n<content>`` blocks (also fenced ``)."""
    import re
    blocks: dict = {}
    # === path === markers
    for m in re.finditer(r"^===\s*([^\n=]+?)\s*===\n(.*?)(?=^===\s*[^\n=]+?\s*===|\Z)",
                         text, re.MULTILINE | re.DOTALL):
        path = m.group(1).strip().strip("`")
        body = m.group(2).strip()
        # strip a leading ```lang and trailing ``` fence if present
        body = re.sub(r"^```[\w.+-]*\n", "", body)
        body = re.sub(r"\n```\s*$", "", body)
        if path and body:
            blocks[path] = body + "\n"
    return blocks


def _in_scope(rel: str, globs: list[str]) -> bool:
    """True if relative path ``rel`` matches any allowlist glob.

    Fix 2: delegate to the ONE shared, robust matcher
    (``scope_guard._matches_any``) so parallel and single-doer mode enforce
    IDENTICAL scope semantics (directory globs, ``**``, normalization).
    Soft-fail to allow so a matcher slip never silently drops a legit write.
    """
    try:
        from aiforge_core.runtime import scope_guard as _sg
        return _sg._matches_any(rel, globs)
    except Exception:  # noqa: BLE001 — never crash the parallel runner
        return True


def lightweight_run_one(subtask: dict, worktree: str) -> dict:
    """Fast per-subtask runner: ONE LLM call to implement the subtask as
    complete file(s), written into the worktree. Far cheaper than the full
    ReAct Doer loop — so N subtasks actually finish on a shared local model."""
    try:
        from aiforge_core.llm.client import complete as _complete
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    goal = subtask.get("goal") or subtask.get("slug") or "implement the subtask"
    prompt = (
        f"Implement this subtask as COMPLETE, runnable Python file(s).\n\n"
        f"SUBTASK: {goal}\n\n"
        "Output ONLY the file(s), each as:\n=== relative/path.py ===\n"
        "<full file content>\n\nNo prose, no explanation. If multiple files are "
        "needed, emit multiple === path === blocks.")
    try:
        out = _complete("doer", [
            {"role": "system", "content": "You are a senior engineer. Output "
             "complete, working code files only, in the === path === format."},
            {"role": "user", "content": prompt}], max_tokens=2048)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    files = _parse_file_blocks(out or "")
    if not files:
        return {"ok": False, "error": "no file blocks produced"}
    # SAFETY: if the subtask carries a scope allowlist, REJECT writes whose
    # relative path doesn't match any glob — out-of-scope files never land.
    # No allowlist → preserve current behavior (don't break the common case).
    scope = subtask.get("scope_allowlist_globs") or []
    written = 0
    written_files: list[str] = []
    rejected: list[str] = []
    for rel, content in files.items():
        rel = rel.lstrip("/").replace("..", "")
        if scope and not _in_scope(rel, scope):
            rejected.append(rel)
            continue
        dest = os.path.join(worktree, rel)
        try:
            os.makedirs(os.path.dirname(dest) or worktree, exist_ok=True)
            with open(dest, "w") as f:
                f.write(content)
            written += 1
            written_files.append(rel)
        except OSError:
            continue
    if rejected and written == 0:
        return {"ok": False,
                "error": "all writes out of scope: " + ", ".join(rejected),
                "rejected": rejected}
    res = {"ok": written > 0, "files": written_files}
    if rejected:
        res["rejected"] = rejected
    return res


def _default_subtask_runner():
    """Lightweight single-shot by default (fast, completes on shared models);
    set AIFORGE_PARALLEL_FULL_DOER=1 for the heavier multi-step Doer loop."""
    if os.environ.get("AIFORGE_PARALLEL_FULL_DOER", "0") in ("1", "true"):
        return default_run_one
    return lightweight_run_one


_INFLIGHT: set = set()
_INFLIGHT_LOCK = threading.Lock()


def run_subtasks_parallel(ticket, *, run_one=None) -> dict:
    """Entry point: decompose-aware parallel run for one ticket. Loads its
    subtasks + working branch, fans them out concurrently, merges. Operator-
    triggered (and gated by AIFORGE_PARALLEL_SUBTASKS for the auto path) so the
    default single-Doer pipeline is never disturbed."""
    from aiforge_core.runtime.workspace import ensure_branch_and_worktree
    from aiforge_core.tickets import store as _store
    from aiforge_core.tickets import subtasks as _st
    tid = getattr(ticket, "id", ticket)
    subs = _st.get_subtasks(tid)
    # Decompose on demand: a fresh ticket has no subtasks yet — split its
    # title+body so "Run in parallel" works straight from `todo`.
    if not subs:
        prompt = (f"{getattr(ticket, 'title', '')}\n\n"
                  f"{getattr(ticket, 'body', '')}").strip()
        decomposed = _decompose(prompt)
        if len(decomposed) >= 2:
            subs = _st.set_subtasks(tid, decomposed, role="planner")
    if not subs:
        return {"ok": True, "total": 0, "note": "could not decompose into subtasks"}
    # Guard against a second parallel run for the SAME ticket (concurrent
    # POSTs would collide on the per-slug worktree paths).
    with _INFLIGHT_LOCK:
        if tid in _INFLIGHT:
            return {"ok": False, "error": "already running for this ticket"}
        _INFLIGHT.add(tid)
    # Move the ticket into the working state so its lifecycle status reflects
    # the run (todo → in_progress → done/blocked).
    try:
        _store.update_status(tid, "in_progress", role="doer")
    except Exception:  # noqa: BLE001
        pass
    try:
        wt = ensure_branch_and_worktree(ticket)
        if wt:
            # Ticket targets a real repo — merge into its working branch.
            cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
            base_branch = (cur.stdout or "").strip() or "HEAD"
        else:
            # No project repo (e.g. a standalone ticket) — use a per-ticket
            # git workspace so the parallel run still works end-to-end.
            ident = getattr(ticket, "identifier", str(tid))
            cfg = os.environ.get("AIFORGE_CONFIG_DIR", os.path.expanduser("~/.aiforge"))
            wt = os.path.join(os.path.expanduser(cfg), "ticket-workspaces", ident)
            base_branch = _ensure_git_workspace(wt)
        # NOTE: we do NOT touch the process-global AIFORGE_CURRENT_TICKET here.
        # That env is shared across the whole process, so setting it would let
        # a second (different-ticket) concurrent run clobber it and mis-route
        # subtask updates. The orchestrator tracks each subtask's status with
        # an EXPLICIT ticket_id (run_parallel arg → _update) — thread-safe, no
        # global state. The per-subtask Doer's focused prompt has no subtickets
        # array, so it never calls the env-based subtask_update tool.
        agg = run_parallel(wt, base_branch, getattr(ticket, "id", None),
                           subs, run_one or _default_subtask_runner(),
                           validate_one=default_validate_one,
                           integration_test=default_integration_test)
        _emit(getattr(ticket, "id", None), "*", "parallel_review",
              agg.get("review", ""), {k: agg.get(k) for k in
              ("total", "done", "validated", "failed", "merged", "conflicts")})
        # Reflect the outcome on the ticket lifecycle status.
        try:
            _store.update_status(tid, "done" if agg.get("ok") else "blocked",
                                 role="doer")
        except Exception:  # noqa: BLE001
            pass
        return agg
    except Exception:
        try:
            _store.update_status(tid, "blocked", role="doer")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(tid)


# ─────────────── Parallel chat mode (decompose → fan-out → merge) ──────────

_DECOMPOSE_SYS = (
    "You are a planner. Split the task into 3-8 subtasks that run IN PARALLEL. "
    "CRITICAL: each subtask must own a DISTINCT file (or files) — NO two subtasks "
    "may edit the same file, or they will merge-conflict. Put the target file in "
    "the goal, e.g. 'db.py: SQLite store + models'. One file per concern "
    "(db.py, models.py, slug.py, routes.py, main.py, test_app.py, README.md). "
    "Output ONLY: {\"subtickets\": [{\"slug\": \"kebab-id\", \"goal\": "
    "\"<file>: <what>\"}, ...]}. No prose."
)


_ENHANCE_SYS = (
    "You are a senior engineer assistant that cleans up and contextualizes "
    "user requests. First decide the request's intent:\n"
    "- BUILD/CHANGE request (add, fix, build, refactor, etc.): rewrite it as "
    "a clear, concrete build spec — 1-2 lines of goal, then the key "
    "components/files and acceptance criteria as tight bullets.\n"
    "- INFORMATIONAL/exploratory request (a question about the repo, code, "
    "or how something works — nothing to build or change): restate it as a "
    "single clear, well-formed question, folding in any relevant context. Do "
    "NOT invent build components, files, or acceptance criteria for a "
    "question, and do NOT answer the question yourself.\n"
    "Never respond by saying nothing was found, asking the user where to "
    "search, or requesting clarification — if context is sparse, restate the "
    "original request as-is with correct spelling and grammar. Keep it "
    "short. Output ONLY the rewritten request, no preamble."
)


def _orchestrator_timeout_s() -> int:
    """Wall-clock budget for the blocking pre-stream orchestrator LLM calls
    (enhancer / architect / decompose). A hung endpoint must not block every
    non-trivial chat turn for minutes under the default 600s × retries.

    Default 180s: slow *thinking* enhancer models (e.g. qwythos) burn
    300-600 reasoning tokens before emitting the spec and clock 60-150s on
    a real request — a 30s budget timed them out and silently fell back to
    the RAW prompt, dropping all memory/history enrichment. 180s lets a
    reasoning model finish while still bounding a truly hung endpoint.
    Tunable via AIFORGE_ENHANCER_TIMEOUT_S (default 180)."""
    try:
        return max(1, int(os.environ.get("AIFORGE_ENHANCER_TIMEOUT_S", "180")))
    except (TypeError, ValueError):
        return 30


def _enhancer_disabled() -> bool:
    return os.environ.get("AIFORGE_ENHANCER_DISABLE", "").strip().lower() \
        in ("1", "true")


def _enhancer_min_chars() -> int:
    """Pure-length floor: below this many chars a prompt is trivial-by-length
    (no build signal can fit). Kept VERY low so short real imperatives ("add a
    test", "fix the typo in app.py") fall through and ARE enhanced — only the
    whole-message conversational set short-circuits greetings/acks.
    Tunable via AIFORGE_ENHANCER_MIN_CHARS (default 8)."""
    try:
        return max(0, int(os.environ.get("AIFORGE_ENHANCER_MIN_CHARS", "8")))
    except (TypeError, ValueError):
        return 8


# Conversational / non-build openers — greetings, thanks, acks, short meta
# questions. Matched case-insensitively against the (stripped) prompt START.
_CONVERSATIONAL = (
    "hi", "hii", "hey", "hello", "yo", "sup", "gm", "good morning",
    "good evening", "good afternoon", "thanks", "thank you", "thx", "ty",
    "ok", "okay", "cool", "nice", "great", "got it", "sounds good",
    "yes", "yep", "yeah", "no", "nope", "lol", "haha", "bye", "cheers",
    "who are you", "what can you do", "how are you", "what's up", "whats up",
)


def _whole_conversational(low: str) -> bool:
    """True only when the WHOLE message is conversational — a greeting/ack and
    nothing else. Matches a multi-word opener directly (``head == pat``, e.g.
    "good morning", "thank you") OR a string of single-word acks (e.g.
    "ok thanks", "yeah cool"). Crucially it does NOT fire on ack-PREFIXED real
    instructions like "ok, refactor X" (the "refactor"/"X" tokens aren't acks)."""
    import re
    head = low.rstrip("!.?, ")
    if head in _CONVERSATIONAL:
        return True
    toks = [t for t in re.split(r"[\s,]+", head) if t]
    return bool(toks) and all(t in _CONVERSATIONAL for t in toks)


def _is_trivial_prompt(prompt: str) -> bool:
    """True when ``prompt`` is too short to carry a build signal, or the WHOLE
    message is conversational/non-build — so the enhancer (memory fan-out + an
    LLM call) is skipped. Keeps latency low and avoids reshaping chit-chat into
    a fake build spec, WITHOUT swallowing short real imperatives ("add a test")
    or ack-prefixed instructions ("ok, refactor X")."""
    p = (prompt or "").strip()
    if not p:
        return True
    low = p.lower()
    # Pure-length floor (very low): only the shortest fragments. Real short
    # imperatives are longer than this and fall through to be enhanced.
    if len(p) < _enhancer_min_chars():
        return True
    # Whole-message conversational opener (greeting/ack only), any length.
    if len(p) < 64 and _whole_conversational(low):
        return True
    return False


# Change 1 — concrete-prompt skip. A SHORT single-line imperative that already
# names a file + action ("fix the bug in app.py") is already a build spec; the
# enhancer's "rewrite as a build spec" LLM call just adds serial latency. Skip
# it (return the raw prompt) — conservative: only when CLEARLY concrete.
_ACTION_VERBS = (
    "fix", "add", "update", "change", "remove", "rename", "refactor",
    "implement", "write", "create", "delete", "edit", "move",
)
_VERB_RE = re.compile(r"\b(?:" + "|".join(_ACTION_VERBS) + r")\b", re.I)
# A token carrying a code file extension ("app.py", "src/parse.ts"). We
# require a REAL extension (not a bare slash token): matching any "X/Y" path
# over-fired on conceptual slash-phrases like "TCP/IP", "client/server",
# "CI/CD", "read/write" — those name no file, so a verb + one of those wrongly
# skipped enhancement and lost the memory/README context-fold. Concrete now
# means "names an actual code file".
_FILE_EXT_RE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|md|json|ya?ml|sql)\b", re.I)
# Multi-part connectors that mean "enhance, don't skip" (a list / sequence).
_MULTIPART_RE = re.compile(r"\band\b|\bthen\b|;| & ", re.I)


def _enhancer_skip_concrete_enabled() -> bool:
    """Change 1 gate. Default ENABLED; ``AIFORGE_ENHANCER_SKIP_CONCRETE=0``
    (or false/no/off) force-enhances every non-trivial prompt again."""
    return os.environ.get("AIFORGE_ENHANCER_SKIP_CONCRETE", "1") \
        .strip().lower() not in ("0", "false", "no", "off")


def _is_concrete_prompt(prompt: str) -> bool:
    """True when ``prompt`` is a SHORT, single-line-ish imperative that already
    names a concrete file (extension or path separator) AND carries an action
    verb — i.e. it's already actionable and does NOT need the enhancer LLM.

    Conservative by design (err toward enhancing): a vague, multi-part, or long
    prompt returns False so its context still gets folded. Multi-part
    (``and``/``then``/``;``/``&``), multi-line, >200-char, and prompts that name
    no actual code file are all rejected."""
    p = (prompt or "").strip()
    if not p or len(p) > 200:
        return False
    if "\n" in p:                       # multi-line → not a simple one-liner
        return False
    low = p.lower()
    if _MULTIPART_RE.search(low):       # list / sequence → enhance instead
        return False
    if not _VERB_RE.search(low):        # no action verb → not an imperative
        return False
    return bool(_FILE_EXT_RE.search(p))  # must name an actual code file


def _memory_block(prompt: str, repo: str | None) -> str:
    """RELEVANT MEMORY block from unified recall (memory + ticket + code RAG).
    Cheap, soft-fail — never raises, capped ~1200 chars."""
    try:
        from aiforge_core.memory import unified_query
        res = unified_query.query(prompt, repo=repo, limit=5) or {}
        hits = res.get("hits") or []
        lines: list[str] = []
        for h in hits:
            txt = (h.get("text") or "").strip()
            if txt:
                lines.append(f"- {txt}")
        if not lines:
            return ""
        block = "\n".join(lines)
        return "RELEVANT MEMORY:\n" + block[:1200]
    except Exception:  # noqa: BLE001
        return ""


def _history_block(history: list[dict] | None) -> str:
    """RECENT CONVERSATION block: last ~3 turns excluding the current (last)
    user message. Soft-fail, capped ~800 chars."""
    try:
        if not history:
            return ""
        prior = history[:-1]            # drop the current user message
        recent = prior[-3:]
        lines: list[str] = []
        for m in recent:
            role = (m.get("role") or "").strip() or "user"
            content = (m.get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        if not lines:
            return ""
        block = "\n".join(lines)
        return "RECENT CONVERSATION:\n" + block[:800]
    except Exception:  # noqa: BLE001
        return ""


def _readme_block(cwd: str | None) -> str:
    """REPO README block: head of a README in ``cwd``. Soft-fail, capped
    ~800 chars. Empty when no README present."""
    try:
        if not cwd:
            return ""
        for name in ("README.md", "README.rst", "README"):
            path = os.path.join(cwd, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(800)
                head = head.strip()
                if head:
                    return f"REPO README ({name}):\n{head}"
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _enhance(prompt: str, *, history: list[dict] | None = None,
             cwd: str | None = None, repo: str | None = None) -> str:
    """Layer-1 step 1: fix spelling/grammar, write proper sentences, RECALL
    context (memory + recent conversation + repo README), and fold it all into
    a clear, concrete build spec the planner/doer can act on.

    Backward compatible: existing callers pass just ``prompt``. Falls back to
    the raw ``prompt`` on any error or empty output. Disable entirely via
    ``AIFORGE_ENHANCER_DISABLE=1``."""
    if _enhancer_disabled():
        return prompt
    # Triviality / intent gate: greetings, thanks, short questions and other
    # non-build chit-chat are returned UNCHANGED — skip the memory fan-out and
    # the LLM call (latency) and don't reshape conversational turns into fake
    # build specs.
    if _is_trivial_prompt(prompt):
        return prompt
    # Concrete-prompt short-circuit (Change 1): a short single-line imperative
    # that already names a file + action is already actionable — skip the
    # enhancer LLM call (serial-model latency) and hand the raw prompt straight
    # to the ReAct loop. Gated by AIFORGE_ENHANCER_SKIP_CONCRETE (default on).
    if _enhancer_skip_concrete_enabled() and _is_concrete_prompt(prompt):
        return prompt
    # Gather context — each block is independently soft-failing.
    blocks = [b for b in (
        _memory_block(prompt, repo),
        _history_block(history),
        _readme_block(cwd),
    ) if b]
    context = ("\n\n".join(blocks)) if blocks else ""
    user_msg = (
        f"USER REQUEST:\n{prompt}\n\n"
        + (context + "\n\n" if context else "")
        + "Fix spelling and grammar, write proper sentences, and fold any of "
          "the context above that is relevant. Follow the system "
          "instructions above to decide build spec vs. restated question. "
          "Output ONLY the rewritten request."
    )
    try:
        from aiforge_core.llm import client
        out = client.complete("enhancer", [
            {"role": "system", "content": _ENHANCE_SYS},
            {"role": "user", "content": user_msg}], max_tokens=2048,
            timeout_s=_orchestrator_timeout_s())
        return (out or "").strip() or prompt
    except Exception:  # noqa: BLE001
        return prompt


# Public alias for clear imports elsewhere (api.py, etc.).
enhance = _enhance


_ARCHITECT_SYS = (
    "You are the architect. Given a build spec, design the FILE STRUCTURE: list "
    "the files to create, each with its single responsibility. Files must be "
    "DISJOINT (no shared concern). Honor any provided skills, workflows, and "
    "repo rules — design within their constraints. Output ONLY JSON: {\"files\": "
    "[{\"path\": \"db.py\", \"purpose\": \"SQLite store + models\"}, ...]}. No prose."
)


def _architect_context(spec: str, cwd: str | None) -> str:
    """Gather SKILLS / WORKFLOWS / REPO RULES blocks for the architect. Each
    source is independently soft-failing and capped ~1000 chars."""
    def _safe(fn) -> str:
        try:
            return (fn() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    from aiforge_core.runtime import repo_rules, skills, workflows
    parts: list[str] = []
    sk = _safe(lambda: skills.auto_context(spec, cwd))
    if sk:
        parts.append("SKILLS:\n" + sk[:1000])
    wf = _safe(lambda: workflows.auto_context(spec, cwd))
    if wf:
        parts.append("WORKFLOWS:\n" + wf[:1000])
    rl = _safe(lambda: repo_rules.collect(cwd) if cwd else "")
    if rl:
        parts.append("REPO RULES:\n" + rl[:1000])
    return "\n\n".join(parts)


def _architect(spec: str, *, cwd: str | None = None) -> list[dict]:
    """Orchestrator agent 2: design the file structure (disjoint files), guided
    by the repo's skills/workflows/rules. Returns [{path, purpose}, ...] — the
    single source of truth for the split. Backward compatible (cwd optional)."""
    import json as _json
    import re as _re
    context = ""
    try:
        context = _architect_context(spec, cwd)
    except Exception as exc:  # noqa: BLE001
        log.debug("architect context gather failed: %s", exc)
    user_msg = spec + (("\n\n" + context) if context else "")
    try:
        from aiforge_core.llm import client
        out = client.complete("architect", [
            {"role": "system", "content": _ARCHITECT_SYS},
            {"role": "user", "content": user_msg}], max_tokens=1000,
            timeout_s=_orchestrator_timeout_s())
        m = _re.search(r"\{.*\}", out or "", _re.DOTALL)
        obj = _json.loads(m.group(0)) if m else {}
        files = obj.get("files") if isinstance(obj, dict) else None
        return [f for f in (files or []) if isinstance(f, dict) and f.get("path")]
    except Exception as exc:  # noqa: BLE001
        log.warning("architect step failed: %s", exc)
        return []


def _plan_files(files: list[dict]) -> list[dict]:
    """Architect file list → one subtask per file (guaranteed distinct files).

    The slug must be UNIQUE within the plan: it names the worktree dir + branch,
    so two files sharing a basename (``a/db.py`` + ``b/db.py``) slugging to the
    same ``db`` would collide on one worktree → two workers clobber each other.
    On a slug collision we disambiguate with a short hash of the FULL path."""
    import hashlib
    out, seen_paths, seen_slugs = [], set(), set()
    for f in files:
        path = str(f.get("path") or "").strip().lstrip("/")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        slug = _slugify(path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or path)
        if slug in seen_slugs:
            # Same basename as an earlier file — append a short stable hash of
            # the full path so the worktree dir/branch stays unique.
            suffix = hashlib.sha1(path.encode("utf-8")).hexdigest()[:6]
            slug = f"{slug}-{suffix}"
        seen_slugs.add(slug)
        out.append({"slug": slug,
                    "goal": f"{path}: {f.get('purpose') or 'implement'}"})
    return out


def _decompose(prompt: str, tries: int = 2) -> list[dict]:
    """Planner LLM call → subtasks list (JSON array or markdown phases).
    Retries once: a single shot occasionally returns an unparseable format on a
    local model, so we try again before giving up."""
    from aiforge_core.runtime.subtasks_callback import _extract_subtickets
    for attempt in range(max(1, tries)):
        try:
            from aiforge_core.llm import client
            out = client.complete("planner", [
                {"role": "system", "content": _DECOMPOSE_SYS},
                {"role": "user", "content": prompt}], max_tokens=1500,
                timeout_s=_orchestrator_timeout_s())
            subs = _extract_subtickets(out)
            if len(subs) >= 2:
                return subs
        except Exception as exc:  # noqa: BLE001
            log.warning("parallel decompose attempt %d failed: %s", attempt, exc)
    return []


def _ensure_git_workspace(cwd: str) -> str:
    """Make ``cwd`` a git repo with a committed baseline so worktrees can branch
    off it. Returns the base branch name."""
    os.makedirs(cwd, exist_ok=True)
    if _git(["rev-parse", "--git-dir"], cwd).returncode != 0:
        _git(["init"], cwd)
        _git(["config", "user.email", "aiforge@local"], cwd)
        _git(["config", "user.name", "aiforge"], cwd)
    # A fresh workspace is born with the agent's own artifacts gitignored.
    ensure_artifact_gitignore(cwd)
    # need at least one commit for `worktree add <base>` to resolve
    if _git(["rev-parse", "HEAD"], cwd).returncode != 0:
        readme = os.path.join(cwd, ".aiforge-workspace")
        if not os.path.exists(readme):
            with open(readme, "w") as f:
                f.write("aiforge chat workspace\n")
        # .gitignore is the committed baseline (the workspace marker is
        # excluded); excludes keep any stray junk out of the baseline too.
        _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
        _git(["commit", "-m", "workspace baseline"], cwd)
    cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return (cur.stdout or "").strip() or "main"


def stream_parallel_team(prompt: str, cwd: str, subtasks: list[dict] | None = None,
                         enhanced: bool = False):
    """Chat 'parallel team' mode: run the (pre-decomposed) subtasks CONCURRENTLY
    in isolated worktrees under ``cwd``, streaming live status. If ``subtasks``
    isn't supplied, decompose here. Yields SSE-ready dicts."""
    import queue as _queue

    if enhanced:
        # Show the layer-1 spec (analyze → enhance) the planner split.
        yield {"type": "thought", "role": "enhancer", "text": prompt[:800]}
    subs = subtasks
    if not subs:
        yield {"type": "thought", "role": "planner",
               "text": "Decomposing into parallel subtasks…"}
        subs = _decompose(prompt)
    if len(subs) < 2:
        # Caller normally falls back to sequential team mode before reaching
        # here; this is the last-resort guard.
        yield {"type": "message", "text":
               "Couldn't split this into parallel subtasks — running normally."}
        return
    yield {"type": "subtasks", "items": [
        {"slug": s.get("slug") or f"sub-{i+1}",
         "goal": s.get("goal") or "", "status": "pending"}
        for i, s in enumerate(subs)]}

    # Requirements/plan document: persist the enhanced spec + the subtask
    # breakdown to SPEC.md in the workspace BEFORE any subtask runs. It's the
    # single source of truth — fed into every per-subtask fresh context (so each
    # isolated context knows the overall goal) and re-read by the final
    # verification pass to confirm nothing was dropped.
    spec_md = _render_spec_md(prompt, subs)
    try:
        with open(os.path.join(cwd, "SPEC.md"), "w", encoding="utf-8") as _fh:
            _fh.write(spec_md)
        yield {"type": "thought", "role": "planner",
               "text": f"Wrote SPEC.md ({len(subs)} subtasks) — the shared "
                       "requirements doc each subtask builds against."}
    except Exception as _exc:  # noqa: BLE001 — spec write is best-effort
        log.debug("SPEC.md write skipped: %s", _exc)

    yield {"type": "thought", "role": "system",
           "text": f"Running {len(subs)} subtasks — each in its OWN fresh "
                   f"context + worktree (max {_max_workers()} at once)…"}

    base = _ensure_git_workspace(cwd)
    # B3 — surface a dirty-cwd warning before merging into it.
    _warn = _dirty_warning(cwd)
    if _warn:
        yield {"type": "thought", "role": "system", "text": "⚠ " + _warn}
    q: "_queue.Queue" = _queue.Queue()
    result: dict = {}

    def on_status(slug, status, files=None):
        q.put({"type": "subtask_update", "slug": slug, "status": status})
        if files:   # show what the worker produced (expandable action)
            q.put({"type": "tool", "role": slug, "name": "wrote files",
                   "args": {"subtask": slug}, "result": {"files": files}})

    # Spec-bound per-subtask runner: every fresh subtask context is handed the
    # shared SPEC.md so it builds a coherent slice, without inheriting the other
    # subtasks' conversation (that's what keeps each context small).
    _base_run_one = _default_subtask_runner()

    def _spec_run_one(subtask, worktree):
        try:
            return _base_run_one(subtask, worktree, spec_md=spec_md)
        except TypeError:
            # A custom runner that doesn't accept spec_md — call it plainly.
            return _base_run_one(subtask, worktree)

    def _runner():
        try:
            result["agg"] = run_parallel(cwd, base, None, subs,
                                         _spec_run_one,
                                         validate_one=default_validate_one,
                                         integration_test=default_integration_test,
                                         on_status=on_status)
        except Exception as exc:  # noqa: BLE001
            result["err"] = str(exc)
        finally:
            q.put(None)

    t = threading.Thread(target=_runner, name="parallel-chat", daemon=True)
    t.start()
    while True:
        item = q.get()
        if item is None:
            break
        yield item

    agg = result.get("agg") or {}
    if result.get("err"):
        yield {"type": "message", "text": f"Parallel run error: {result['err']}"}
        return

    # Final verification pass — a FRESH context reads SPEC.md + the produced tree
    # and confirms every requirement was addressed (the "close the loop against
    # the original requirement file" step). Best-effort; never blocks the result.
    yield {"type": "thought", "role": "verifier",
           "text": "Verifying the merged result against SPEC.md…"}
    try:
        _verdict = _verify_against_spec(cwd, spec_md)
        if _verdict:
            yield {"type": "thought", "role": "verifier", "text": _verdict[:1500]}
    except Exception as _exc:  # noqa: BLE001
        log.debug("spec verification skipped: %s", _exc)

    yield {"type": "message", "text":
           f"**Parallel run complete** — {agg.get('review', 'done')}.\n\n"
           f"All work merged into the chat workspace. "
           f"{agg.get('done', 0)}/{agg.get('total', 0)} subtasks done. "
           f"See SPEC.md for the requirements each subtask built against."}


def _render_spec_md(prompt: str, subs: list[dict]) -> str:
    """The shared requirements/plan document written to SPEC.md before the run
    and re-read by the final verification pass."""
    lines = ["# Project Spec", "", "## Goal", "", prompt.strip(), "",
             f"## Subtasks ({len(subs)})", ""]
    for i, s in enumerate(subs):
        slug = s.get("slug") or f"sub-{i+1}"
        goal = (s.get("goal") or "").strip()
        lines.append(f"{i+1}. **{slug}** — {goal}")
        for a in (s.get("acceptance") or []):
            lines.append(f"   - [ ] {a}")
    lines.append("")
    return "\n".join(lines)


def _verify_against_spec(cwd: str, spec_md: str) -> str:
    """Fresh-context check: given SPEC.md + a listing of the produced files,
    ask the model whether every requirement is addressed. Returns a short
    verdict string (or '' on any failure)."""
    from aiforge_core.llm.client import complete as _complete
    tree = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".venv", "__pycache__", "node_modules")]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), cwd)
            tree.append(rel)
        if len(tree) > 400:
            break
    listing = "\n".join(sorted(tree)[:400]) or "(no files)"
    convo = [
        {"role": "system", "content":
         "You are a delivery auditor. Given a project SPEC and the file tree "
         "that was produced, state briefly whether every spec item appears "
         "addressed. List any MISSING or clearly-incomplete items as a short "
         "bullet list. Be concise (<200 words). If everything is covered, say so."},
        {"role": "user", "content":
         f"SPEC.md:\n{spec_md[:6000]}\n\nPRODUCED FILES:\n{listing}"},
    ]
    try:
        out = _complete("verifier", convo)
        return (out or "").strip()
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["run_parallel", "run_subtasks_parallel", "default_run_one",
           "default_validate_one", "default_integration_test",
           "stream_parallel_team", "enabled"]
