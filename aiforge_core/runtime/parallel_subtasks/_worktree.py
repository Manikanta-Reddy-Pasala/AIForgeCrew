"""Git worktree isolation and per-subtask attempt/validate.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical).
Conflict resolution and merging live in ``_worktree_merge``."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading

from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS

from ._worktree_merge import (  # noqa: F401  # re-exported
    _conflict_hunks,
    _dirty_warning,
    _hunk_breadcrumbs,
    _merge_branch,
    _resolve_all_hunks,
    _resolve_conflict_hunk,
    _resolve_conflicts,
    _resolve_file_conflicts,
    _still_conflicted,
    _syntax_ok,
)

log = logging.getLogger("aiforge.parallel_subtasks")

# git operations that touch the MAIN repo's index/worktree list (worktree
# add/remove, branch -D, merge) must be serialized — concurrent `git worktree
# add` races on .git/index.lock. The per-subtask WORK still runs in parallel
# (each worktree has its own index); only these repo-level git calls are locked.
_GIT_LOCK = threading.Lock()


def enabled() -> bool:
    # DEFAULT ON (operator decision 2026-07-09): a multi-file build decomposes
    # + fans out unless explicitly disabled with AIFORGE_PARALLEL_SUBTASKS=0.
    return os.environ.get("AIFORGE_PARALLEL_SUBTASKS", "1").strip().lower() \
        in ("1", "true", "yes", "on")


def _max_workers() -> int:
    """Concurrent subtask workers — DEFAULT 4 (operator decision 2026-07-09;
    was: auto-1 on a local endpoint). On a strictly SERIAL local server the
    extra workers just queue on the one model (no speedup, some worktree
    overhead) — set AIFORGE_PARALLEL_SUBTASKS_MAX=1 there; modern LM Studio /
    llama.cpp slots and vLLM/TGI do serve concurrently and win from 4."""
    raw = os.environ.get("AIFORGE_PARALLEL_SUBTASKS_MAX")
    if raw is not None:
        try:
            return max(1, min(8, int(raw)))
        except ValueError:
            return 4
    # A local endpoint serves one request at a time. Extra workers only queue.
    try:
        from aiforge_core.llm.router import is_local_endpoint
        if is_local_endpoint("doer"):
            return 1
    except Exception:  # noqa: BLE001
        pass
    return 4


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120)


def _slugify(text: str) -> str:
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
        default = "1" if _max_workers() == 1 else "3"
        return max(0, min(6, int(os.environ.get("AIFORGE_SUBTASK_RETRIES", default))))
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


def _retry_subtask(subtask: dict, last: dict, i: int) -> dict:
    """The subtask dict for retry attempt ``i``, informed by the prior failure so
    the next prompt says what went wrong instead of re-running blindly. A subtask
    that STOPPED (hit the turn budget unfinished) was too big for one pass →
    ``_too_big`` tells the retry to ship a minimal working CORE first."""
    prev_err = (last.get("error")
                or (last.get("validation") or {}).get("error")
                or "the previous build/tests failed")
    too_big = "(stopped:" in str(prev_err).lower() or bool(last.get("stopped"))
    return {**subtask, "_retry_error": str(prev_err)[:800], "_retry_n": i,
            "_too_big": too_big}


def _run_with_retries(subtask: dict, wt: str, slug: str, base_branch: str,
                      ticket_id, run_one, validate_one) -> "tuple[dict, int]":
    """Run+validate the subtask, retrying (bounded) on failure/crash — subtasks
    are the risky unit. The worktree is reset between attempts so nothing leaks
    across tries. Returns ``(last_result, attempts_used_index)``."""
    last: dict = {}
    attempts = _retries() + 1
    i = 0
    for i in range(attempts):
        if i > 0:
            subtask = _retry_subtask(subtask, last, i)
            # One slot: keep the file and patch the error. Resetting and
            # regenerating the whole file is the 16-generation blowup.
            if _max_workers() == 1:
                subtask = {**subtask, "_patch_retry": True}
            else:
                _reset_worktree(wt, base_branch)
            _emit(ticket_id, slug, "subtask_retry",
                  f"{slug} retry {i}/{attempts - 1}", {"slug": slug, "attempt": i})
        last = _attempt(subtask, wt, slug, run_one, validate_one)
        if last["ok"]:
            break
    return last, i


def _run_subtask(repo: str, base_branch: str, ticket_id: int | None,
                 subtask: dict, run_one, validate_one, on_status=None,
                 run_token: str | None = None, should_cancel=None) -> dict:
    slug = subtask.get("slug") or "sub"
    # Graceful Stop: a subtask still queued when the user hits Stop never starts
    # its (expensive) agent run — it reports cancelled and the dock shows it.
    if should_cancel is not None and should_cancel():
        _update(ticket_id, slug, "cancelled", on_status)
        return {"slug": slug, "ok": False, "cancelled": True, "branch": None}
    _update(ticket_id, slug, "running", on_status)
    try:
        wt, branch = _make_worktree(repo, base_branch, slug, run_token)
    except Exception as exc:  # noqa: BLE001
        _update(ticket_id, slug, "failed", on_status)
        return {"slug": slug, "ok": False, "error": str(exc), "branch": None}

    last, i = _run_with_retries(subtask, wt, slug, base_branch, ticket_id,
                                run_one, validate_one)
    ok = last["ok"]
    _emit(ticket_id, slug,
          "subtask_validated" if last.get("validated") else "subtask_rejected",
          f"{slug} validation {'passed' if last.get('validated') else 'failed'}",
          {"slug": slug, "validated": last.get("validated"), "attempts": i + 1})
    _files = ((last.get("detail") or {}).get("files")
              if isinstance(last.get("detail"), dict) else None)
    _update(ticket_id, slug, "done" if ok else "failed", on_status, _files)
    return {"slug": slug, "ok": ok, "ran": last.get("ran"),
            "validated": last.get("validated"), "attempts": i + 1,
            "branch": branch, "worktree": wt,
            "detail": last.get("detail"), "validation": last.get("validation"),
            "error": last.get("error")}


def _project_fail_detail(res: dict) -> str:
    """Real compiler/test output from a ``project()`` result — buried under
    ``results[].output`` for compiled stacks (a ``javac``/``rustc``/``go``/gradle
    error lives there, not the top-level ``error`` which is usually None). Without
    this the integration verdict + the reconcile's fix prompt got an EMPTY detail
    and couldn't act on a Java build error at all."""
    if not isinstance(res, dict):
        return ""
    parts = []
    if res.get("error"):
        parts.append(str(res["error"]))
    for r in (res.get("results") or []):
        if isinstance(r, dict) and not r.get("ok"):
            parts.append(str(r.get("output") or r.get("error") or ""))
    return ("\n".join(p for p in parts if p).strip())[-4000:] or None


def _build_or_test(worktree: str) -> dict:
    """Quality gate for a checkout: if the project HAS tests, gate strictly on
    the test result (FAILING tests do NOT pass via a build fallback); only when
    there are NO tests do we accept a green build. No project → nothing to gate.
    """
    try:
        from aiforge_core.runtime.tools.project_runner import (
            _has_tests,
            detect,
            project,
        )
        stacks = (detect(worktree) or {}).get("stacks") or []
        if not stacks:
            return {"ok": True, "via": "no-project", "note": "nothing to build/test"}
        if _has_tests(worktree, stacks):
            test = project(action="test", cwd=worktree)
            ok = bool(isinstance(test, dict) and test.get("ok"))
            return {"ok": ok, "via": "test",
                    "detail": None if ok else _project_fail_detail(test)}
        build = project(action="build", cwd=worktree)
        ok = bool(isinstance(build, dict) and build.get("ok"))
        return {"ok": ok, "via": "build", "note": "no tests",
                "detail": None if ok else _project_fail_detail(build)}
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
    # GENERAL RULE (no hardcoded file-type list): a subtask produces ONE file in
    # an ISOLATED worktree, and NO single file can build/compile the whole project
    # by itself — its imports/deps live in the OTHER subtasks' worktrees. So a
    # project build here always fails for whichever file happens to carry the
    # manifest (pom.xml / pyproject / package.json / …). Per-subtask we therefore
    # only check the file was WRITTEN and is SYNTACTICALLY valid (language-agnostic
    # syntax_guard — Python compile, javac/gcc/go/node/… syntax-only). The REAL
    # build + tests run post-merge, all files together (default_integration_test).
    _path = str(subtask.get("path") or "").strip().lstrip("/")
    if not _path:
        return {"ok": True, "via": "no-path"}
    target = os.path.join(worktree, _path)
    if not (os.path.isfile(target) and os.path.getsize(target) > 0):
        return {"ok": False, "via": "written", "detail": f"file not written: {_path}"}
    try:
        with open(target, encoding="utf-8", errors="replace") as _fh:
            _content = _fh.read()
        # The scaffold pre-wrote a syntax-valid STUB. If the worker didn't
        # replace it (LLM failed / empty), the stub would falsely pass — reject
        # it so the subtask RETRIES instead of "succeeding" with an empty stub.
        if _SCAFFOLD_MARK in _content:
            return {"ok": False, "via": "stub",
                    "detail": f"still the scaffold stub — not implemented: {_path}"}
        from aiforge_core.runtime.syntax_guard import validate_syntax
        _ok, _err = validate_syntax(_path, _content)
        return {"ok": _ok, "via": "syntax", "detail": None if _ok else _err}
    except Exception:  # noqa: BLE001 — never fail a subtask on a guard glitch
        return {"ok": True, "via": "written"}


def default_integration_test(repo_root: str) -> dict:
    """Build + test the WHOLE integrated result on the base branch after all
    subtasks merged — catches breakage that only shows when combined. Like the
    per-subtask gate, FAILING tests do not pass via a build fallback."""
    return _build_or_test(repo_root)


def _emit(ticket_id, _slug, kind, body, md) -> None:
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

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._reconcile import (  # noqa: F401  # read via _pkg() or by tests
    _SCAFFOLD_MARK,
    _spec_goal,
)
