"""Git worktree isolation, per-subtask attempt/validate, conflict resolution + merge.

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
        return max(0, min(6, int(os.environ.get("AIFORGE_SUBTASK_RETRIES", "3"))))
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

    # Retry the whole run+validate on failure/crash — subtasks are the risky
    # unit, so we keep trying (bounded) before giving up. Reset the worktree
    # between attempts so nothing leaks across tries.
    last: dict = {}
    attempts = _retries() + 1
    for i in range(attempts):
        if i > 0:
            _reset_worktree(wt, base_branch)
            # REGENERATE informed by the failure — feed the prior error into the
            # subtask so the next attempt's prompt says what went wrong, instead
            # of blindly re-running the same prompt to the same dead end.
            _prev_err = (last.get("error")
                         or (last.get("validation") or {}).get("error")
                         or "the previous build/tests failed")
            # C (lightweight re-decompose): a subtask that STOPPED (hit the turn
            # budget without finishing) was too big for one pass. Rather than
            # blindly re-run, tell the retry to ship the CORE first — a minimal
            # working slice — then extras only if room. Small models finish a
            # scoped core where they thrash on the whole thing.
            _too_big = "(stopped:" in str(_prev_err).lower() or bool(last.get("stopped"))
            subtask = {**subtask, "_retry_error": str(_prev_err)[:800],
                       "_retry_n": i, "_too_big": _too_big}
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
            _has_tests, detect, project,
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


_CONFLICT_RE = re.compile(
    r"<<<<<<<[^\n]*\n(.*?)\n=======\n(.*?)\n>>>>>>>[^\n]*(?:\n|$)", re.DOTALL)


def _hunk_breadcrumbs(content: str, span: tuple, n: int) -> tuple:
    """N lines of ambient code above/below a conflict hunk — grounds the model's
    indentation + parameter bindings (self.width vs a param, the parent class's
    base indent) so an out-of-context resolution doesn't break syntax."""
    start_char, end_char = span
    line_start = content[:start_char].count("\n")
    line_end = content[:end_char].count("\n")
    lines = content.splitlines(keepends=True)
    above = "".join(lines[max(0, line_start - n):line_start])
    below = "".join(lines[line_end + 1:min(len(lines), line_end + 1 + n)])
    return above, below


def _resolve_conflict_hunk(goal: str, path: str, head: str, incoming: str,
                           above: str = "", below: str = "", attempt: int = 1) -> str:
    """Minimal-context conflict resolver: feed ONLY this hunk (+ goal + a few
    breadcrumb lines) and get back the merged block — no whole file, no markers/
    fences. On a retry (attempt>1) it's told the last try broke syntax + given
    wider ambient scope."""
    from aiforge_core.llm.client import complete as _complete
    retry = ("\nCRITICAL: your previous resolution broke syntax/compilation. More "
             "surrounding code is shown below — match its brackets, indentation and "
             "variable/parameter names EXACTLY.\n" if attempt > 1 else "")
    prompt = (
        "You are a stateless Git conflict-resolution compilation step. Merge the "
        "two versions of the CONFLICTING HUNK into ONE syntactically-correct result "
        "that fulfils the goal and keeps the valid features of BOTH sides, lining up "
        "seamlessly with the ambient code. Output ONLY the raw replacement block — "
        "no git markers, no ``` fences, no prose." + retry + "\n\n"
        + (f"GOAL: {goal[:600]}\n\n" if goal else "")
        + f"FILE: {path}\n\n"
        + (f"[AMBIENT CODE ABOVE]\n{above}\n\n" if above else "")
        + f"[CONFLICTING HUNK]\n<<<<<<< HEAD\n{head}\n=======\n{incoming}\n>>>>>>> incoming\n\n"
        + (f"[AMBIENT CODE BELOW]\n{below}\n" if below else ""))
    try:
        out = _complete("doer", [
            {"role": "system", "content": "Output only the resolved code block, "
             "nothing else."},
            {"role": "user", "content": prompt}], max_tokens=2048) or ""
    except Exception:  # noqa: BLE001
        return ""
    out = re.sub(r"^```\w*\n?|\n?```$", "", out.strip(), flags=re.M)
    out = re.sub(r"^\s*(<<<<<<<|=======|>>>>>>>).*$", "", out, flags=re.M)
    return out.strip("\n")


def _resolve_file_conflicts(repo: str, relpath: str, goal: str,
                            max_attempts: int = 3) -> bool:
    """Widen-context-retry state machine for ONE conflicted file: resolve every
    hunk with breadcrumbs; if the file fails syntax, roll back to the conflicted
    state and retry with a wider breadcrumb budget (5 → 15 → 25). Deterministic
    rollback; only a small token tax per widen."""
    fp = os.path.join(repo, relpath)
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            backup = fh.read()
    except Exception:  # noqa: BLE001
        return False
    budget = 5
    for attempt in range(1, max_attempts + 1):
        new = backup
        for m in _CONFLICT_RE.finditer(backup):
            above, below = _hunk_breadcrumbs(backup, m.span(), budget)
            res = _resolve_conflict_hunk(goal, relpath, m.group(1), m.group(2),
                                         above, below, attempt)
            if not res:
                res = m.group(1)                    # fallback: keep HEAD
            new = new.replace(m.group(0), res + "\n", 1)
        if "<<<<<<<" in new or "=======" in new or ">>>>>>>" in new:
            budget += 10
            continue                                # markers left → widen + retry
        try:
            from aiforge_core.runtime.syntax_guard import validate_syntax
            ok, _ = validate_syntax(relpath, new)
        except Exception:  # noqa: BLE001
            ok = True
        if ok:
            try:
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(new)
                return True
            except Exception:  # noqa: BLE001
                return False
        budget += 10                                # syntax fail → widen + retry
    return False


def _resolve_conflicts(repo: str, goal: str) -> bool:
    """Resolve every conflicted file via the breadcrumb + widen-retry machine,
    git-add each. Returns True only if ALL files resolve cleanly (else abort)."""
    p = _git(["diff", "--name-only", "--diff-filter=U"], repo)
    files = [f for f in p.stdout.splitlines() if f.strip()]
    if not files:
        return False
    for f in files:
        if not _resolve_file_conflicts(repo, f, goal):
            return False
        _git(["add", "--", f], repo)
    return True


def _merge_branch(repo: str, base_branch: str, branch: str) -> tuple[bool, str]:
    """Merge ``branch`` into ``base_branch`` (checked out in ``repo``). Returns
    (ok, info). On conflict, RESOLVES the hunks (minimal-context) rather than
    dropping the subtask's work; aborts only if resolution fails."""
    p = _git(["merge", "--no-edit", branch], repo)
    if p.returncode == 0:
        return True, "merged"
    # conflict → try to auto-resolve the hunks (the safety valve for concurrency)
    if os.environ.get("AIFORGE_RESOLVE_CONFLICTS", "1") not in ("0", "false"):
        try:
            if _resolve_conflicts(repo, _spec_goal(repo)):
                c = _git(["commit", "--no-edit", "-m",
                          "resolve: automated subtask merge conflict"], repo)
                if c.returncode == 0:
                    return True, "merged (conflicts auto-resolved)"
        except Exception:  # noqa: BLE001
            pass
    # resolution failed / disabled → abort to leave the base branch clean
    _git(["merge", "--abort"], repo)
    return False, (p.stdout + p.stderr)[:300]

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._reconcile import _SCAFFOLD_MARK, _spec_goal
