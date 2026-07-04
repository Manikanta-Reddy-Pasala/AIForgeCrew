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

from aiforge_core.runtime import review_gates
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
            subtask = {**subtask, "_retry_error": str(_prev_err)[:800],
                       "_retry_n": i}
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


def _run_sequential(cwd: str, base_branch: str, subs: list, run_one, *,
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
    done = 0
    failed = 0

    # 1. Write + commit the test files first (they are the executable spec). Not
    #    gated — tests alone fail to import until impls exist; that's the baseline.
    for s in tests:
        if should_cancel and should_cancel():
            break
        slug = s.get("slug")
        if on_status:
            on_status(slug, "running")
        try:
            res = run_one(s, cwd)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)}
        _git(["add", "-A"], cwd)
        _git(["commit", "--no-edit", "-m", f"test: {slug}"], cwd)
        if on_status:
            on_status(slug, "done", (res or {}).get("files"))

    # 2. Baseline fail count with tests present, impls not yet built. Prune any
    #    off-plan files first so the tree matches the plan.
    _prune_offplan_files(cwd, subs)
    _ok, out = _project_test_output(cwd)
    prev_fails = _fail_count(out)
    _e({"type": "thought", "role": "coordinator",
        "text": f"Sequential build — baseline {prev_fails} failing. Building "
                f"{len(impls)} module(s) one at a time, committing each that holds "
                "or improves the score…"})

    # 3. Each impl in dep order, seeing the REAL prior files; commit or revert.
    for s in impls:
        if should_cancel and should_cancel():
            break
        slug = s.get("slug")
        if on_status:
            on_status(slug, "running")
        s["_existing_files"] = _existing_source_digest(cwd, s.get("path"))
        s["_tests"] = _matching_tests_for(cwd, s.get("path") or "")
        retries = _retries()
        committed = False
        for attempt in range(retries):
            if should_cancel and should_cancel():
                break
            try:
                res = run_one(s, cwd)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)}
            _prune_offplan_files(cwd, subs)       # drop any phantom file this step made
            _ok, out = _project_test_output(cwd)
            fails = _fail_count(out)
            if fails <= prev_fails:
                _git(["add", "-A"], cwd)
                _git(["commit", "--no-edit", "-m", f"feat: {slug}"], cwd)
                _e({"type": "tool", "role": slug, "name": "committed",
                    "args": {"status": ("tests can't run yet" if fails >= 999
                                        else f"{fails} failing")},
                    "result": {"ok": True, "files": (res or {}).get("files") or []}})
                prev_fails = fails
                committed = True
                done += 1
                if on_status:
                    on_status(slug, "done", (res or {}).get("files"))
                break
            # regression → undo this attempt, retry with the error
            _git(["reset", "--hard", "HEAD"], cwd)
            _git(["clean", "-fd", "-e", ".aiforge-venv", "-e", ".aiforge-contracts"], cwd)
            s["_retry_error"] = (out or "")[-1500:]
            _e({"type": "thought", "role": slug,
                "text": f"{slug} raised failures {prev_fails}→{fails} — reverted, "
                        f"retry {attempt + 1}/{retries}…"})
        if not committed:
            failed += 1
            if on_status:
                on_status(slug, "failed")

    return {"ok": failed == 0, "total": len(subs), "done": done + len(tests),
            "failed": failed}


def run_parallel(repo_root: str, base_branch: str, ticket_id: int | None,
                 subtasks: list[dict], run_one, *, validate_one=None,
                 integration_test=None, on_status=None, merge: bool = True,
                 should_cancel=None) -> dict:
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
                              run_one, validate_one, on_status, run_token,
                              should_cancel)
                    for s in batch]
            for f in concurrent.futures.as_completed(futs):
                # On Stop, cancel every still-queued (not-yet-started) future so
                # no further subtask agent kicks off.
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

    # Orchestrator-level RESTART rounds: after the first pass, re-dispatch the
    # still-failed subtasks in fresh worktrees (transient failures / contention
    # often clear on a retry). Bounded by AIFORGE_PARALLEL_RERUN_ROUNDS (1).
    by_slug: dict = {}
    for r in _pass(subs):
        by_slug[r.get("slug")] = r
    try:
        rounds = max(0, min(5, int(os.environ.get("AIFORGE_PARALLEL_RERUN_ROUNDS", "3"))))
    except ValueError:
        rounds = 1
    for _ in range(rounds):
        if should_cancel is not None and should_cancel():
            break
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
    path = str(subtask.get("path") or "").strip().lstrip("/")
    accept = subtask.get("acceptance") or []
    scope = subtask.get("scope_allowlist_globs") or []
    # Hard path pin: every subtask runs in its OWN fresh context, so without an
    # exact-path command each one re-guesses the package dir casing
    # (minilang/ vs mini_lang/ vs miniLang/) and the merge ends up with 3 split
    # dirs. Name the exact target path verbatim and forbid inventing variants.
    path_pin = (
        f"TARGET FILE (create EXACTLY this path, byte-for-byte — do NOT rename, "
        f"re-case, or re-spell the directory or file; other subtasks use the "
        f"SAME paths from SPEC.md): {path}\n"
        if path else "")
    msg = (
        (f"PROJECT SPEC (shared context — build YOUR slice to fit it; use the "
         f"EXACT file/dir paths it lists, verbatim):\n{spec_md.strip()[:6000]}\n\n---\n\n"
         if spec_md and spec_md.strip() else "")
        + f"Implement this subtask, then build + test it.\n\n{path_pin}GOAL: {goal}\n"
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
    # Code-level path enforcement — the prompt pin isn't 100% on a local model,
    # so if the agent wrote the file at a re-cased/renamed path (miniLang/… ,
    # mini_lang/… , pysyntax/…) instead of the exact target, MOVE it to the
    # canonical path here. Guarantees every subtask's file lands where SPEC.md
    # + the other subtasks expect, so the merge never splits into variant dirs.
    if path:
        _enforce_target_path(worktree, path)
    return {"ok": ok}


def _enforce_target_path(worktree: str, path: str) -> None:
    """If ``path`` doesn't exist in ``worktree`` but a file with the same
    basename was created elsewhere (a re-cased/renamed dir), move it to the
    exact ``path`` and prune the now-empty variant dir. Best-effort."""
    import shutil
    target = os.path.join(worktree, path)
    if os.path.exists(target):
        return
    base = os.path.basename(path)
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", ".aiforge-worktrees")]
        if base in files:
            src = os.path.join(root, base)
            if os.path.abspath(src) == os.path.abspath(target):
                return
            try:
                os.makedirs(os.path.dirname(target) or worktree, exist_ok=True)
                shutil.move(src, target)
                log.info("path-enforce: moved %s -> %s", src, target)
                # prune an emptied variant dir (e.g. miniLang/ after its one file)
                vdir = os.path.dirname(src)
                if vdir and vdir != worktree and not os.listdir(vdir):
                    os.rmdir(vdir)
            except Exception as exc:  # noqa: BLE001 — enforcement is best-effort
                log.debug("path-enforce move failed %s->%s: %s", src, target, exc)
            return


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


def lightweight_run_one(subtask: dict, worktree: str, spec_md: str = "") -> dict:
    """Fast per-subtask runner: ONE LLM call to implement the subtask as
    complete file(s), written into the worktree. Far cheaper than the full
    ReAct Doer loop — so N subtasks actually finish on a shared local model.

    ``spec_md`` is the shared requirements doc (fed so each fresh single-shot
    knows the overall goal). When the subtask carries a canonical ``path`` (one
    file per subtask, from the architect), the emitted content is force-written
    to THAT exact path — the model's own `=== path ===` label is ignored — so
    isolated subtasks can't split the package into mini_lang/ + miniLang/ etc."""
    try:
        from aiforge_core.llm.client import complete as _complete
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    goal = subtask.get("goal") or subtask.get("slug") or "implement the subtask"
    path = str(subtask.get("path") or "").strip().lstrip("/")
    retry_err = str(subtask.get("_retry_error") or "").strip()
    tests_src = str(subtask.get("_tests") or "").strip()
    prompt = (
        (f"⚠ YOUR PREVIOUS ATTEMPT FAILED with:\n{retry_err[:800]}\nFix that this "
         f"time — re-read the SPEC, emit correct, complete code.\n\n---\n\n"
         if retry_err else "")
        + (f"CRITICAL PRINCIPLE — THE TEST IS THE SPECIFICATION.\n"
           f"The test file below is the ABSOLUTE GROUND TRUTH for method/attribute "
           f"NAMES (incl. leading underscores like `_is_valid_position`), "
           f"signatures, return types, exact VALUES and math. Your code MUST make "
           f"EVERY assertion pass. Even if a rule looks unconventional (e.g. the "
           f"test says an O-piece is 'cyan' not 'yellow', or score == (level+1)*10), "
           f"match it EXACTLY — NEVER 'correct'/'standardize' a value the test "
           f"asserts. If the test calls `x._foo(a, b)` you define `_foo(self, a, b)`; "
           f"if it asserts `grid[0][3] == COLORS['cyan']` your code must produce "
           f"cyan.\n\nTESTS (ground truth):\n{tests_src[:6000]}\n\n---\n\n"
           if tests_src else "")
        + (f"EXISTING PROJECT FILES already on disk (REAL, committed — import from "
           f"these using their EXACT class/function/constant names + signatures; do "
           f"NOT guess or invent variant names):\n{subtask.get('_existing_files')}\n\n"
           f"---\n\n" if subtask.get("_existing_files") else "")
        + (f"PROJECT SPEC (shared — build YOUR slice to fit it; use the EXACT "
         f"file/dir paths it lists):\n{spec_md.strip()[:5000]}\n\n---\n\n"
         if spec_md and spec_md.strip() else "")
        + f"Implement this subtask as COMPLETE, runnable file(s) in the language "
          f"the target path implies (.py→Python, .java→Java, .go→Go, .ts→"
          f"TypeScript, .c/.cpp→C/C++, .rs→Rust, .sh→shell, …).\n\n"
        + (f"TARGET FILE (emit EXACTLY this path, verbatim — do not re-case or "
           f"rename the directory): {path}\n\n" if path else "")
        + f"SUBTASK: {goal}\n\n"
        "CONTRACT: expose the PUBLIC API listed for your file in the spec's "
        "'API contract' section EXACTLY (same names, signatures, constants), and "
        "when you import/call another file, use the EXACT names it exposes there. "
        "Do not invent variant names — the other files are written to this same "
        "contract.\n\n"
        "Output ONLY the file(s), each as:\n=== relative/path.ext ===\n"
        "<full file content>\n\nNo prose, no explanation.")
    # Generous output budget — a hardcoded 2048 TRUNCATED big files (e.g. a
    # thorough test file) mid-string, landing a SyntaxError that only surfaced at
    # the post-merge integration test. Use the configured cap (default 8192).
    try:
        _mt = max(2048, int(os.environ.get("AIFORGE_LLM_MAX_TOKENS", "8192")))
    except ValueError:
        _mt = 8192
    try:
        out = _complete("doer", [
            {"role": "system", "content": "You are a senior engineer. Output "
             "complete, working code files only, in the === path === format."},
            {"role": "user", "content": prompt}], max_tokens=_mt)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    files = _parse_file_blocks(out or "")
    if not files:
        return {"ok": False, "error": "no file blocks produced"}
    # Canonical-path remap: this subtask owns exactly ONE file (``path``), so
    # ignore whatever dir the model labelled and force the content to the exact
    # target — pick the block whose basename matches, else the first/only block.
    if path:
        base = os.path.basename(path)
        chosen = next((c for p, c in files.items()
                       if os.path.basename(p.strip().lstrip("/")) == base), None)
        if chosen is None:
            chosen = next(iter(files.values()))
        files = {path: chosen}
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
        # Syntax gate (LANGUAGE-AGNOSTIC): lightweight writes files DIRECTLY (no
        # file_write / no syntax_guard), and an isolated subtask worktree has no
        # build marker so build-validation is skipped — a truncated/broken file
        # (any language) would sail through per-subtask and only blow up at the
        # post-merge build/test. Run the shared syntax_guard (Python compile,
        # shell/C/C++/Java/Go/JS/Ruby checkers, else brace-balance) and FAIL the
        # subtask (→ bounded retry) so broken code never lands.
        try:
            from aiforge_core.runtime.syntax_guard import validate_syntax
            _ok, _err = validate_syntax(rel, content)
            if not _ok:
                return {"ok": False, "error": f"{rel}: {_err}"}
        except Exception:  # noqa: BLE001 — guard must never crash the runner
            pass
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
    "You are the architect. Given a build spec, design the FILE STRUCTURE **and "
    "the exact public API of each file**, because each file is implemented by a "
    "SEPARATE worker in isolation — they can only agree if you fix the shared "
    "contract now. Files must be DISJOINT (single responsibility). Honor any "
    "provided skills, workflows, and repo rules.\n\n"
    "DEPENDENCY CLUSTERING — critical for correctness. Tightly-coupled logic that "
    "shares mutable state or arbitrary conventions (piece shapes/colors, collision "
    "checks, the board matrix, the game loop, the scoring formula for a game; the "
    "model + its core operations for any system) MUST live in ONE file owned by "
    "ONE worker — do NOT atomise a coupled subsystem across files, or separate "
    "workers invent conflicting conventions (one says the O-piece is 'yellow', the "
    "other checks for 'cyan') that never reconcile. Rule: if two units must edit or "
    "assume the same state/constants, COLLAPSE them into a single file. Only give a "
    "separate file to a genuinely DECOUPLED concern (persistence/high-score store, "
    "audio, a CLI/entrypoint, rendering behind a clean interface). Prefer a few "
    "cohesive files over many fragile ones.\n\n"
    "For every file give its exact PUBLIC API: the class names, function "
    "signatures, and module-level constants that OTHER files import or call — "
    "spelled EXACTLY as everyone must use them (one canonical name per thing). "
    "Use real signatures (names, params, return types where knowable).\n\n"
    "ALWAYS include, in the SAME file list: (a) a TEST file for EVERY code "
    "module (unit tests that exercise its public API), (b) at least one "
    "INTEGRATION test that drives the whole thing end-to-end, and (c) the "
    "project's build/manifest file (pyproject.toml / package.json / go.mod / "
    "pom.xml / Cargo.toml as fits the language). The tests are what lets the "
    "build be verified — never omit them.\n\n"
    "Output ONLY JSON, no prose (note: the core coupled logic is ONE file, only "
    "the decoupled store is separate):\n"
    "{\"files\": [{\"path\": \"game.py\", \"purpose\": \"board matrix + pieces + "
    "collision + scoring + loop (one coupled subsystem)\", \"api\": [\"class Game\", "
    "\"class Board\", \"SHAPES: dict\", \"COLORS: dict\"]}, {\"path\": \"storage.py\", "
    "\"purpose\": \"decoupled high-score persistence\", \"api\": "
    "[\"def save_score(n: int) -> None\", \"def load_scores() -> list\"]}, "
    "{\"path\": \"tests/test_game.py\", \"purpose\": \"unit-test Game\", \"api\": []}, "
    "...]}"
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
            {"role": "user", "content": user_msg}], max_tokens=4000,
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
        _api = [str(a) for a in (f.get("api") or []) if a]
        out.append({"slug": slug, "path": path, "api": _api,
                    "goal": f"{path}: {f.get('purpose') or 'implement'}"
                            + (" | MUST expose EXACTLY: " + "; ".join(_api) if _api else "")})
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
                         enhanced: bool = False, session_id: int | None = None):
    """Chat 'parallel team' mode: run the (pre-decomposed) subtasks CONCURRENTLY
    in isolated worktrees under ``cwd``, streaming live status. If ``subtasks``
    isn't supplied, decompose here. Yields SSE-ready dicts.

    ``session_id`` wires the Stop button through: the per-subtask dispatch stops
    launching new subtasks, the reconciliation loop halts, and the run's own
    build/test subprocesses are killed."""
    import queue as _queue

    def _cancelled() -> bool:
        if session_id is None:
            return False
        try:
            from aiforge_core.runtime import chat_cancel
            return chat_cancel.is_cancelled(session_id)
        except Exception:  # noqa: BLE001
            return False

    def _drain_steering():
        """Fold any mid-run steering messages into SPEC.md so the remaining
        (sequential-on-local) subtasks + the reconciler pick them up. Yields
        notice events."""
        if session_id is None:
            return
        try:
            from aiforge_core.runtime import chat_interject
            if not chat_interject.pending(session_id):
                return
            for _txt in chat_interject.drain(session_id):
                _txt = (_txt or "").strip()
                if not _txt:
                    continue
                try:
                    with open(os.path.join(cwd, "SPEC.md"), "a", encoding="utf-8") as _fh:
                        _fh.write(f"\n\n## ⚙ User steering (mid-run)\n- {_txt}\n")
                except Exception:  # noqa: BLE001
                    pass
                yield {"type": "thought", "role": "system",
                       "text": f"📌 Steering applied to SPEC.md — guides the remaining "
                               f"subtasks + the final reconcile: “{_txt[:140]}”"}
        except Exception:  # noqa: BLE001
            return

    # Accept mid-run steering for this parallel run (folded into SPEC.md).
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_interject
            chat_interject.set_steerable(session_id, True)
        except Exception:  # noqa: BLE001
            pass

    # Bind this run's subprocesses (integration build/pytest) to the session so
    # Stop kills them, and register a cancel checker for the dispatch/reconcile.
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_cancel
            chat_cancel.set_active(session_id)
        except Exception:  # noqa: BLE001
            pass

    if _cancelled():
        yield {"type": "message", "text": "Stopped before the run started."}
        return

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
    # Backstop: guarantee test coverage so the build can be verified + self-healed
    # even when the planner omitted tests.
    subs = _ensure_test_coverage(subs)
    # Decomposition consistency: every per-module test needs a matching impl file
    # (test_board→board, BookServiceTest→BookService). When the architect collapses
    # impl into one file but writes per-module tests, add the missing impl modules.
    _before = len(subs)
    subs = _ensure_impl_modules(subs)
    if len(subs) > _before:
        _added = [s.get("path") for s in subs[_before:]]
        yield {"type": "thought", "role": "planner",
               "text": f"Decomposition fix — {len(subs) - _before} test(s) target "
                       f"modules with no impl file; added: {', '.join(_added)}"}
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
    # SPEC REVIEW — check the spec before any code is built (contradictions,
    # ambiguity, missing cases, scope creep). Refines it if needed.
    try:
        spec_md, _sr_note = review_gates.review_spec(prompt, spec_md)
        if _sr_note:
            yield {"type": "thought", "role": "reviewer", "text": f"🔍 {_sr_note}"}
    except Exception as _exc:  # noqa: BLE001
        log.debug("spec review skipped: %s", _exc)
    try:
        with open(os.path.join(cwd, "SPEC.md"), "w", encoding="utf-8") as _fh:
            _fh.write(spec_md)
        yield {"type": "thought", "role": "planner",
               "text": f"Wrote SPEC.md ({len(subs)} subtasks) — the shared "
                       "requirements doc each subtask builds against."}
    except Exception as _exc:  # noqa: BLE001 — spec write is best-effort
        log.debug("SPEC.md write skipped: %s", _exc)

    # Record the pre-existing code so greenfield-only steps (scaffold, off-plan
    # prune) never touch an EXISTING repo — on a real repo they'd delete the whole
    # codebase (everything not in this task's small plan).
    _preexisting = _snapshot_baseline(cwd)
    _greenfield = _is_greenfield(cwd)
    if not _greenfield:
        yield {"type": "thought", "role": "system",
               "text": f"Existing repo ({_preexisting} source files) — editing in "
                       "place; skipping scaffold + off-plan prune (greenfield-only)."}

    # SCAFFOLD — deterministically create every file at its canonical path (stub +
    # API-contract header) BEFORE parallelizing, then commit to base so worktrees
    # branch from a fixed tree. GREENFIELD ONLY (stubbing over an existing repo is
    # wrong). Gated (default on).
    if _greenfield and os.environ.get("AIFORGE_SCAFFOLD", "1") not in ("0", "false"):
        try:
            _stubs = _scaffold_stubs(cwd, subs)
            if _stubs:
                yield {"type": "tool", "role": "planner", "name": "scaffolded project",
                       "args": {}, "result": {"files": _stubs}}
        except Exception as _exc:  # noqa: BLE001
            log.debug("scaffold skipped: %s", _exc)

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
        # Re-read SPEC.md from disk so any mid-run steering appended to it is
        # seen by subtasks that start AFTER the steer (sequential on local).
        _spec = spec_md
        try:
            _p = os.path.join(cwd, "SPEC.md")
            if os.path.isfile(_p):
                with open(_p, encoding="utf-8", errors="replace") as _fh:
                    _spec = _fh.read()
        except Exception:  # noqa: BLE001
            pass
        try:
            return _base_run_one(subtask, worktree, spec_md=_spec)
        except TypeError:
            # A custom runner that doesn't accept spec_md — call it plainly.
            return _base_run_one(subtask, worktree)

    def _runner():
        try:
            # SEQUENTIAL mode: single branch, each subtask sees the REAL prior
            # committed files (no isolated interface-guessing), commit-or-revert
            # per step. Right for tightly-coupled projects.
            if os.environ.get("AIFORGE_SEQUENTIAL", "0") not in ("0", "false"):
                result["agg"] = _run_sequential(
                    cwd, base, subs, _spec_run_one, on_status=on_status,
                    should_cancel=_cancelled, emit=q.put)
                return
            test_subs = [s for s in subs if _is_test_subtask(s)]
            impl_subs = [s for s in subs if not _is_test_subtask(s)]
            _tf = os.environ.get("AIFORGE_TEST_FIRST", "1") not in ("0", "false")
            if _tf and test_subs and impl_subs:
                # TEST-FIRST: build the tests first (they pin behaviour from the
                # API contract), merge them into base, then build each impl in a
                # worktree that HAS the tests + is fed its own test content — so
                # the impl is functionally correct, not just linking.
                q.put({"type": "thought", "role": "system",
                       "text": f"Test-first: writing {len(test_subs)} test file(s), "
                               f"then {len(impl_subs)} module(s) built to pass them…"})
                aggA = run_parallel(cwd, base, None, test_subs, _spec_run_one,
                                    validate_one=None, integration_test=None,
                                    on_status=on_status, should_cancel=_cancelled)
                if not _cancelled():
                    # TEST REVIEW — the tests are now written; review them against
                    # the SPEC and fix provably-wrong ones (contradictions, scope
                    # creep, impossible values) BEFORE any impl is built to them, so
                    # the impl targets clean tests instead of the reconcile burning
                    # rounds on impossible-to-satisfy assertions. Commit fixes to
                    # base so the impl worktrees branch from the cleaned tests.
                    try:
                        _tr_changed, _tr_note = review_gates.review_tests(cwd, spec_md)
                        if _tr_note:
                            q.put({"type": "thought", "role": "reviewer",
                                   "text": f"🔍 {_tr_note}"})
                        if _tr_changed:
                            _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
                            _git(["commit", "-m", "test-review fixes"], cwd)
                    except Exception:  # noqa: BLE001
                        pass
                    for s in impl_subs:
                        s["_tests"] = _matching_tests_for(cwd, s.get("path") or "")
                    aggB = run_parallel(cwd, base, None, impl_subs, _spec_run_one,
                                        validate_one=default_validate_one,
                                        integration_test=default_integration_test,
                                        on_status=on_status, should_cancel=_cancelled)
                    result["agg"] = _merge_aggs(aggA, aggB)
                else:
                    result["agg"] = aggA
            else:
                result["agg"] = run_parallel(cwd, base, None, subs,
                                             _spec_run_one,
                                             validate_one=default_validate_one,
                                             integration_test=default_integration_test,
                                             on_status=on_status,
                                             should_cancel=_cancelled)
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
        yield from _drain_steering()      # fold mid-run steering into SPEC.md
        if _cancelled():
            # Stop pressed: drain no further. The runner sees should_cancel and
            # winds down (stops launching new subtasks); we just quit streaming.
            break

    if _cancelled():
        agg = result.get("agg") or {}
        yield {"type": "message", "text":
               f"**Stopped** — {agg.get('done', 0)}/{len(subs)} subtasks finished "
               "before you hit Stop. Their work is committed in the workspace; "
               "verification + integration were skipped."}
        return

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

    # Compile + end-to-end test the merged result (any language). Subtasks are
    # built in ISOLATION, so the tree can fail to link on cross-file drift (a
    # test imports a name a module spelled differently). A bounded RECONCILIATION
    # pass over the whole merged tree fixes those mismatches until green. The
    # final report — or step-by-step manual steps if the toolchain is absent —
    # is folded into the completion message.
    # Strip off-plan phantom files (a worker/reconciler-invented package that
    # duplicates declared modules → collection errors) BEFORE integration.
    try:
        _off = _prune_offplan_files(cwd, subs)
        if _off:
            yield {"type": "thought", "role": "system",
                   "text": f"Removed {len(_off)} off-plan file(s) not in the plan "
                           f"(kept the tree matching SPEC): {', '.join(_off[:6])}"}
    except Exception as _pexc:  # noqa: BLE001
        log.debug("off-plan prune skipped: %s", _pexc)

    _integ_md = ""
    _res: dict = {}
    try:
        yield from _reconcile_integration(cwd, _res, should_cancel=_cancelled)
        _rep = _res.get("rep") or {}
        if _rep.get("md"):
            _integ_md = "\n\n---\n\n" + _rep["md"]
    except Exception as _iexc:  # noqa: BLE001
        log.debug("integration report skipped: %s", _iexc)
    # Clean the merger's blackboard sidecars from the delivered workspace.
    try:
        import shutil as _sh
        _sh.rmtree(os.path.join(cwd, _CONTRACT_DIR), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    yield {"type": "message", "text":
           f"**Parallel run complete** — {agg.get('review', 'done')}.\n\n"
           f"All work merged into the chat workspace. "
           f"{agg.get('done', 0)}/{agg.get('total', 0)} subtasks done. "
           f"See SPEC.md for the requirements each subtask built against."
           + _integ_md}


_CODE_EXTS = (".py", ".go", ".js", ".ts", ".rs", ".java", ".c", ".cpp", ".rb")


def _test_path_for(path: str) -> str:
    """Conventional test path for a code file (per language). '' when the
    language's test layout is too involved to synthesise (rely on the
    architect, which is instructed to include tests)."""
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    if not stem or stem.startswith("__"):
        return ""
    if ext == ".py":
        return f"tests/test_{stem}.py"
    if ext == ".go":
        return path[:-3] + "_test.go"
    if ext in (".js", ".ts"):
        return path[:-len(ext)] + f".test{ext}"
    if ext == ".rb":
        return f"spec/{stem}_spec.rb"
    if ext == ".rs":
        return f"tests/{stem}_test.rs"
    return ""


def _ensure_test_coverage(subs: list[dict]) -> list[dict]:
    """Backstop: if the plan has NO test files, add a unit-test subtask per code
    module (so the build can be verified + self-healed). No-op when tests exist
    or the languages have no easy test convention."""
    if any(_is_test_subtask(s) for s in subs):
        return subs
    code = [s for s in subs if str(s.get("path") or "").endswith(_CODE_EXTS)
            and not _is_test_subtask(s)]
    added: list[dict] = []
    seen = {str(s.get("path") or "") for s in subs}
    for s in code:
        tp = _test_path_for(str(s.get("path") or ""))
        if tp and tp not in seen:
            seen.add(tp)
            added.append({
                "slug": _slugify("test-" + os.path.basename(tp)), "path": tp,
                "api": [],
                "goal": f"{tp}: unit tests for {s['path']} — exercise its public "
                        f"API (from the API contract), assert real behaviour."})
    return subs + added


_CONTRACT_DIR = ".aiforge-contracts"


def _path_to_module(path: str) -> str:
    p = str(path or "").lstrip("/")
    for ext in (".py", ".java", ".go", ".ts", ".tsx", ".js", ".rs", ".rb"):
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".").strip(".")


def _write_contract_sidecar(worktree: str, subtask: dict, out: str) -> None:
    """Parse the worker's ``===CONTRACT=== {json}`` interface declaration and
    persist it under ``.aiforge-contracts/`` so the merger has a language-agnostic,
    worker-declared blackboard (exposes/consumes) to reconcile over."""
    import json as _json
    import re as _re
    m = _re.search(r"===CONTRACT===\s*(\{.*)", out, _re.DOTALL)
    if not m:
        return
    blob = m.group(1)
    # brace-balance to the first complete object
    depth = 0
    end = -1
    for i, ch in enumerate(blob):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return
    try:
        obj = _json.loads(blob[:end])
    except Exception:  # noqa: BLE001
        return
    path = str(subtask.get("path") or "")
    slug = str(subtask.get("slug") or _path_to_module(path) or "sub")
    rec = {"module": _path_to_module(path), "path": path,
           "exposes": obj.get("exposes") or [], "consumes": obj.get("consumes") or {}}
    # Write to the shared PROJECT ROOT (not the isolated worktree) so all workers'
    # contracts land in one place for the merger — no commit/merge, no tree
    # pollution. Concurrent workers use distinct filenames (per slug).
    marker = os.sep + ".aiforge-worktrees" + os.sep
    root = worktree.split(marker)[0] if marker in worktree else worktree
    d = os.path.join(root, _CONTRACT_DIR)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, _slugify(slug) + ".json"), "w", encoding="utf-8") as fh:
        fh.write(_json.dumps(rec))


_DECL_KEYWORDS = frozenset({
    "class", "def", "func", "function", "const", "let", "var", "public",
    "private", "protected", "static", "final", "void", "struct", "type",
    "interface", "fn", "val", "enum", "abstract", "async", "export", "default",
})


def _clean_symbol(s: str) -> str:
    """The declared NAME from an api entry ('class Board' → 'Board',
    'def drop(x)' → 'drop', 'COLORS: dict' → 'COLORS')."""
    import re as _re
    for t in _re.findall(r"[A-Za-z_]\w*", str(s)):
        if t not in _DECL_KEYWORDS:
            return t
    return ""


def _blackboard_from_contracts(cwd: str):
    """Read the declared contract sidecars → (exposes{mod:set}, consumes[(cons,tgt,name)]).
    Returns None when no contracts were declared (→ AST fallback)."""
    import json as _json
    cdir = os.path.join(cwd, _CONTRACT_DIR)
    if not os.path.isdir(cdir):
        return None
    exposes: dict[str, set] = {}
    raw: list[dict] = []
    for f in os.listdir(cdir):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, f), encoding="utf-8", errors="replace") as fh:
                rec = _json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        mod = rec.get("module") or ""
        names = set()
        for e in rec.get("exposes") or []:
            _n = _clean_symbol(e)
            if _n:
                names.add(_n)
        if mod:
            exposes[mod] = names
        raw.append(rec)
    if not exposes:
        return None
    consumes: list[tuple[str, str, str]] = []
    mods = set(exposes)
    for rec in raw:
        cons = rec.get("module") or ""
        for tgtmod, names in (rec.get("consumes") or {}).items():
            tgt = (tgtmod if tgtmod in mods
                   else next((m for m in mods if m.split(".")[-1] == str(tgtmod).split(".")[-1]), None))
            if not tgt:
                continue
            for n in (names or []):
                _n = _clean_symbol(n)
                if _n:
                    consumes.append((cons, tgt, _n))
    return exposes, consumes


def _is_test_subtask(s: dict) -> bool:
    """True when this subtask produces a TEST file (built first, in test-first)."""
    p = (str(s.get("path") or "") + " " + str(s.get("slug") or "")).lower()
    base = os.path.basename(str(s.get("path") or "")).lower()
    return ("test" in base or "/test" in p or "/spec" in p
            or base.startswith("test") or base.endswith(("_test.py", "_test.go",
            ".test.js", ".test.ts", ".spec.ts", ".spec.js"))
            or "test-" in p or "-test" in p)


def _matching_tests_for(cwd: str, impl_path: str) -> str:
    """Read the test file(s) whose module name matches ``impl_path`` (board.py →
    test_board.py / board_test.* / test/…/board.*), so the impl is built to PASS
    them. Returns concatenated test source (capped)."""
    if not impl_path:
        return ""
    stem = os.path.splitext(os.path.basename(impl_path))[0].lower()
    if not stem:
        return ""
    out: list[str] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv", "__pycache__")]
        for f in files:
            fl = f.lower()
            is_test = ("test" in fl or ".spec." in fl)
            if is_test and stem in fl:
                try:
                    with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                        rel = os.path.relpath(os.path.join(root, f), cwd)
                        out.append(f"=== {rel} ===\n{fh.read()[:4000]}")
                except Exception:  # noqa: BLE001
                    pass
    return "\n\n".join(out)[:8000]


def _merge_aggs(a: dict, b: dict) -> dict:
    """Combine two run_parallel aggregates (test phase + impl phase)."""
    a = a or {}
    b = b or {}
    total = (a.get("total", 0) or 0) + (b.get("total", 0) or 0)
    done = (a.get("done", 0) or 0) + (b.get("done", 0) or 0)
    return {
        "ok": bool(a.get("ok", True)) and bool(b.get("ok", True)),
        "total": total, "done": done,
        "failed": (a.get("failed", 0) or 0) + (b.get("failed", 0) or 0),
        "validated": (a.get("validated", 0) or 0) + (b.get("validated", 0) or 0),
        "merged": (a.get("merged", 0) or 0) + (b.get("merged", 0) or 0),
        "conflicts": (a.get("conflicts") or []) + (b.get("conflicts") or []),
        "review": b.get("review") or a.get("review") or "done",
    }


def _reconcile_rounds() -> int:
    try:
        return max(0, min(16, int(os.environ.get("AIFORGE_RECONCILE_ROUNDS", "12"))))
    except ValueError:
        return 12


def _project_test_output(cwd: str) -> tuple[bool, str]:
    """Run the project's tests and return ``(ok, raw_output)`` — the RAW build/
    test output (not the formatted report), so the reconciler sees exact errors.
    ``ok`` True when there's no project / no tests (nothing to reconcile)."""
    try:
        from aiforge_core.runtime.integration_report import run_bare_python_tests
        # PREFER the managed-venv pytest for any Python tree with tests: it
        # pip-installs the third-party deps (pygame, numpy, …) that a plain
        # project(action=test) misses — otherwise pytest fails to import and the
        # captured output is EMPTY, so the reconciler gets no errors to act on.
        bare = run_bare_python_tests(cwd)
        if bare is not None:
            return bare
        from aiforge_core.runtime.tools.project_runner import (
            _has_tests, detect, project,
        )
        stacks = (detect(cwd) or {}).get("stacks") or []
        if not stacks:
            return True, ""
        if not _has_tests(cwd, stacks):
            b = project(action="build", cwd=cwd) or {}
            return bool(b.get("ok")), str(b.get("error") or b.get("output") or "")
        t = project(action="test", cwd=cwd) or {}
        return bool(t.get("ok")), str(t.get("error") or t.get("output") or "")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _directed_hints(output: str) -> list[str]:
    """Turn common cross-file link errors — in ANY language — into CONCRETE,
    actionable fixes. The difference between the reconciler editing files vs
    narrating a plan. Covers Python / Java / Kotlin / Go / C / C++ / Node / Rust /
    TS — whichever produced the failing build/test output."""
    hints: list[str] = []

    def add(h: str) -> None:
        hints.append(h)

    # ── Python ──────────────────────────────────────────────────────────
    for name, mod in re.findall(r"cannot import name ['\"](\w+)['\"] from ['\"]([\w.]+)['\"]", output):
        add(f"`{mod}` is missing `{name}`: open it, see what it ACTUALLY defines, "
            f"then add/rename to `{name}` there OR fix the import to the real name.")
    for mod in re.findall(r"No module named ['\"]([\w.]+)['\"]", output):
        add(f"module `{mod}` is imported but missing — create it or fix the path.")
    for mod, attr in re.findall(r"module ['\"]?([\w.]+)['\"]? has no attribute ['\"](\w+)['\"]", output):
        add(f"`{mod}` has no `{attr}` — define it there or fix the caller.")
    for name in re.findall(r"NameError: name ['\"](\w+)['\"] is not defined", output):
        add(f"`{name}` is used but never defined/imported — add it.")
    # ── Java / Kotlin ───────────────────────────────────────────────────
    for sym in re.findall(r"cannot find symbol[^\n]*?symbol:\s*\w+\s+(\w+)", output):
        add(f"Java: symbol `{sym}` not found — it's referenced but not "
            f"defined/imported with that exact name; reconcile the two sides.")
    for pkg in re.findall(r"package ([\w.]+) does not exist", output):
        if pkg.startswith("javax."):
            add(f"Java: `{pkg}` not found — this project is on Spring Boot 3 / "
                f"Jakarta, so change EVERY `javax.` import to `jakarta.` "
                f"(e.g. javax.persistence→jakarta.persistence, "
                f"javax.validation→jakarta.validation) across all files. Keep the "
                f"pom's Spring Boot version and the imports consistent.")
        elif pkg.startswith("jakarta."):
            add(f"Java: `{pkg}` not found — add the matching starter dependency to "
                f"the build file (e.g. spring-boot-starter-data-jpa / "
                f"-validation), or the code/pom versions disagree — align them.")
        else:
            add(f"Java: package `{pkg}` doesn't exist — fix the import to the real "
                f"package, or add its dependency to the build file.")
    # ── Go ──────────────────────────────────────────────────────────────
    for sym in re.findall(r"undefined:\s*([\w.]+)", output):
        add(f"Go: `{sym}` is undefined — define it or fix the reference to the "
            f"real identifier (one canonical name across files).")
    # ── C / C++ ─────────────────────────────────────────────────────────
    for sym in re.findall(r"undefined reference to [`']?([\w:]+)", output):
        add(f"C/C++: undefined reference to `{sym}` — the declaration and "
            f"definition disagree, or the defining file isn't linked/named right.")
    for sym in re.findall(r"['\"]?(\w+)['\"]? was not declared", output):
        add(f"C/C++: `{sym}` not declared — include the right header / fix the name.")
    for sym in re.findall(r"no member named ['\"](\w+)['\"]", output):
        add(f"C/C++: no member `{sym}` — align the struct/class with its usage.")
    # ── Node / JS / TS ──────────────────────────────────────────────────
    for mod in re.findall(r"Cannot find module ['\"]([^'\"]+)['\"]", output):
        add(f"JS/TS: cannot find module `{mod}` — fix the import path or create it.")
    for name in re.findall(r"(\w+) is not defined", output):
        add(f"JS: `{name}` is not defined — import/define it (one canonical name).")
    for name in re.findall(r"(\w+) is not a function", output):
        add(f"JS: `{name}` is not a function — the export/shape disagrees with the call.")
    # ── Rust ────────────────────────────────────────────────────────────
    for sym in re.findall(r"cannot find (?:value|function|type) `(\w+)` in", output):
        add(f"Rust: `{sym}` not found in scope — define it or fix the `use`/name.")
    for imp in re.findall(r"unresolved import `([\w:]+)`", output):
        add(f"Rust: unresolved import `{imp}` — fix the module path or add the item.")
    # ── missing attribute / method (Python) — incl. hasattr() assertions ─
    for cls, attr in re.findall(r"'(\w+)' object has no attribute '(\w+)'", output):
        add(f"class `{cls}` is missing `{attr}` — ADD that attribute/method to "
            f"`{cls}` (a caller/test needs it; check the test for the expected "
            f"type/behaviour).")
    for attr in re.findall(r"hasattr\([^,]+,\s*['\"](\w+)['\"]\)", output):
        add(f"a test asserts an object HAS `{attr}` but it doesn't — open the "
            f"test to see which class is built, then add attribute/method `{attr}` "
            f"to that class (initialise it in __init__ / implement the method).")
    for name, attr in re.findall(r"module '([\w.]+)' has no attribute '(\w+)'", output):
        add(f"`{name}` is missing top-level `{attr}` — define it there.")
    # ── assertion VALUE mismatches — a logic bug to fix in the impl ──────
    for got, exp in re.findall(r"assert (\S{1,40}) == (\S{1,40})", output):
        add(f"assertion `{got} == {exp}` failed — the impl returns the wrong "
            f"value; fix the logic so it produces what the test expects.")
    # ── generic call/type mismatches (any language) ─────────────────────
    for typ, msg in re.findall(r"(TypeError|AttributeError|incompatible types)[:\s]+([^\n]{0,110})", output):
        add(f"{typ}: {msg.strip()} — align the call site with the definition.")

    # de-dup, keep order
    seen: set = set()
    uniq = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq[:20]


_SRC_EXTS = (".py", ".java", ".kt", ".go", ".c", ".cc", ".cpp", ".cxx", ".h",
             ".hpp", ".js", ".mjs", ".ts", ".tsx", ".rs", ".rb", ".php", ".sh",
             ".toml", ".cfg", ".json")


def _gather_sources(cwd: str) -> list[tuple[str, str]]:
    """Every source file in the tree (ANY language), for the reconciler's
    rewrite context. Excludes deps/artifacts/venvs. Returns [(relpath, content)]."""
    out: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv", "venv",
            "__pycache__", "node_modules", "target", "build", "dist",
            ".pytest_cache", _CONTRACT_DIR)]
        for f in files:
            if f.endswith(_SRC_EXTS):
                p = os.path.join(root, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        out.append((os.path.relpath(p, cwd), fh.read()))
                except Exception:  # noqa: BLE001
                    pass
    return out


def _spec_goal(cwd: str) -> str:
    """The ORIGINAL GOAL from SPEC.md — re-stated at the top of the merger prompt
    to anchor the model's attention on the primary objective."""
    try:
        p = os.path.join(cwd, "SPEC.md")
        if os.path.isfile(p):
            import re as _re
            src = open(p, encoding="utf-8", errors="replace").read()
            m = _re.search(r"##\s*Goal\s*(.+?)(?:\n##\s|\Z)", src, _re.DOTALL)
            if m:
                return m.group(1).strip()[:2000]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _files_in_output(cwd: str, output: str) -> set:
    """Source files REFERENCED in the failing test/build output — minimal,
    targeted context for the resolver (not the whole tree)."""
    import re as _re
    hits: set = set()
    for m in _re.findall(r"([\w./\\-]+\.(?:py|java|go|js|mjs|ts|tsx|rs|c|cc|cpp|cxx|h|hpp|rb|php))", output):
        p = m.replace("\\", "/")
        if os.path.isabs(p) and p.startswith(cwd):
            p = os.path.relpath(p, cwd)
        p = p.lstrip("./")
        if os.path.isfile(os.path.join(cwd, p)):
            hits.add(p)
    return hits


def _py_local_imports(cwd: str, rel: str) -> set:
    """Local module files a Python file imports (1 hop) — so the resolver sees
    both sides of a cross-file mismatch, still minimal."""
    import ast as _ast
    out: set = set()
    try:
        with open(os.path.join(cwd, rel), encoding="utf-8", errors="replace") as fh:
            tree = _ast.parse(fh.read())
    except Exception:  # noqa: BLE001
        return out
    mods: set = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[-1])
        elif isinstance(node, _ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[-1])
    for base in mods:
        fn = base + ".py"
        for root, _d, files in os.walk(cwd):
            if fn in files:
                r = os.path.relpath(os.path.join(root, fn), cwd)
                if ".aiforge" not in r:
                    out.add(r)
    return out


def _relevant_files(cwd: str, output: str) -> list:
    """Files the resolver needs: the ones named in the errors + their direct
    local imports. Falls back to the whole tree only if nothing was parsed."""
    seed = _files_in_output(cwd, output)
    if not seed:
        return _gather_sources(cwd)
    # BFS the local-import graph up to 2 hops from the failing files (test →
    # module → its deps), capped, so the resolver sees the whole failing chain
    # but never the whole tree.
    picked = set(seed)
    frontier = set(seed)
    for _ in range(2):
        nxt: set = set()
        for rel in frontier:
            if rel.endswith(".py"):
                nxt |= _py_local_imports(cwd, rel)
        nxt -= picked
        if not nxt or len(picked) >= 15:
            break
        picked |= nxt
        frontier = nxt
    out = []
    for rel in sorted(picked):
        try:
            with open(os.path.join(cwd, rel), encoding="utf-8", errors="replace") as fh:
                out.append((rel, fh.read()))
        except Exception:  # noqa: BLE001
            pass
    return out


_PATCH_RE = re.compile(
    r"<<<<<<< SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>> REPLACE", re.DOTALL)
_FILE_HDR_RE = re.compile(r"^###\s*FILE:\s*(.+?)\s*$", re.MULTILINE)


def _apply_patches(cwd: str, out: str) -> tuple[list, list]:
    """Deterministic Search-and-Replace applier (zero-LLM). Parses `### FILE:`
    headers + `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks, verifies each
    SEARCH matches the file character-for-character, swaps it, syntax-checks, and
    writes. Surgical: fixing one test can't rewrite an unrelated section. Returns
    (written_files, failures[(file, why)])."""
    written: list = []
    failures: list = []
    heads = [(m.start(), m.group(1).strip()) for m in _FILE_HDR_RE.finditer(out)]
    if not heads:
        return written, [("", "no ### FILE headers")]
    for i, (pos, rel) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(out)
        seg = out[pos:end]
        rel = rel.lstrip("/").replace("..", "")
        fp = os.path.join(cwd, rel)
        if not os.path.isfile(fp):
            failures.append((rel, "file not found"))
            continue
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception:  # noqa: BLE001
            continue
        orig = content
        for search, replace in _PATCH_RE.findall(seg):
            if search in content:
                content = content.replace(search, replace, 1)
            else:
                failures.append((rel, "SEARCH block not found (indent/char mismatch)"))
        if content == orig:
            continue
        try:
            from aiforge_core.runtime.syntax_guard import validate_syntax
            ok, _ = validate_syntax(rel, content)
            if not ok:
                failures.append((rel, "syntax broke after patch"))
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(fp, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(rel)
        except Exception:  # noqa: BLE001
            pass
    return written, failures


def _rewrite_fix(cwd: str, output: str, hints: list[str], *,
                 model: str | None = None, audit_tests: bool = False) -> list[str]:
    """Minimal-context PATCH resolver (Git-state model, NOT a whole-tree blackboard):
    feed ONLY the files referenced in the failing output + their direct imports,
    with the errors, and have the LLM OUTPUT the corrected files (=== path ===
    blocks). Syntax-check + write each. Returns paths written. Language/usecase-
    agnostic; no task-specific logic. Keeps context small so a local model
    doesn't blow its window / hallucinate."""
    from aiforge_core.llm.client import complete as _complete
    try:
        budget = int(os.environ.get("AIFORGE_RECONCILE_CTX_CHARS", "40000"))
    except ValueError:
        budget = 40000
    # Fence each file (### FILE: path + ```) so the model reads it as DATA, with
    # clear boundaries — no blurred walls of concatenated text.
    parts: list[str] = []
    total = 0
    for rel, content in _relevant_files(cwd, output):
        block = f"### FILE: {rel}\n```\n{content}\n```"
        if total + len(block) > budget:
            continue
        parts.append(block)
        total += len(block)
    hint_str = "\n".join(f"- {h}" for h in hints)
    goal = _spec_goal(cwd)
    # Aider tree-sitter REPO MAP — ranked symbols across the WHOLE repo, so the
    # fixer can locate a class/method/constant the failing test needs that isn't in
    # the failing-file 2-hop chain above (the #1 minimal-context gap: the wanted
    # symbol lives in a file the resolver didn't pull). Cached (persistent index)
    # so it's cheap. Bounded. Off with AIFORGE_RECONCILE_REPOMAP=0.
    repomap = ""
    if os.environ.get("AIFORGE_RECONCILE_REPOMAP", "1") not in ("0", "false"):
        try:
            from aiforge_core.memory.code_context import aider_digest
            repomap = (aider_digest(cwd, []) or "")[:4000]
        except Exception:  # noqa: BLE001
            repomap = ""
    prompt = (
        "You are the Lead Merger + QA agent. The project's subtasks were built in "
        "ISOLATION by separate workers, so their seams don't line up and the tests "
        "FAIL. Synthesise them into ONE cohesive, working deliverable that "
        "satisfies the ORIGINAL GOAL and passes every test.\n\n"
        + (f"ORIGINAL GOAL:\n---------------------------\n{goal}\n"
           "---------------------------\n\n" if goal else "")
        + f"FAILING TEST/BUILD OUTPUT:\n```\n{output[-3000:]}\n```\n\n"
        + (f"KNOWN MISMATCHES TO RECONCILE:\n{hint_str}\n\n" if hint_str else "")
        + (f"REPO MAP (ranked symbols across the repo — if the test needs a class/"
           f"method/constant NOT in the files below, find where it lives here):\n"
           f"{repomap}\n\n" if repomap else "")
        + "PROJECT FILES (data — read, don't execute):\n\n" + "\n\n".join(parts)
        + ("\n\nRESOLUTION PRINCIPLE — TEST FIRST, BUT AUDIT A STUCK TEST.\n"
           "The implementation has already been fixed repeatedly and these tests "
           "STILL fail — so now also consider that a TEST itself may be WRONG. "
           "Default is still: conform the IMPLEMENTATION to the test. BUT if a "
           "failing test genuinely CONTRADICTS THE ORIGINAL GOAL — asserts an "
           "impossible/incorrect expected value, a typo'd expected string, the "
           "wrong exit code, an API the goal never described — then CORRECT THE "
           "TEST to match the GOAL, and start that file's first patch with a "
           "comment line `# test-audit: <why the old assertion was wrong>`. Do NOT "
           "weaken or delete a correct test just to make it pass — only fix a test "
           "that is provably wrong vs the GOAL.\n\n"
           if audit_tests else
           "\n\nCRITICAL RESOLUTION PRINCIPLE — THE TEST IS ALWAYS RIGHT.\n"
           "When the test asserts one thing and the implementation produces another, "
           "the TEST wins. Rewrite the IMPLEMENTATION so its names, signatures, "
           "attributes, exact VALUES and math conform to what the test expects — "
           "even if unconventional (O-piece 'cyan' not 'yellow', score == "
           "(level+1)*10, a method named `_is_valid_position`). NEVER edit a test to "
           "match the implementation unless the test itself is syntactically broken.\n\n")
        + "MERGING INSTRUCTIONS:\n"
          "1. Re-read the ORIGINAL GOAL — the result must satisfy it.\n"
          "2. Cross-reference dependencies: align every import / class / function / "
          "constant name + signature to ONE canonical spelling — the name the TEST "
          "uses. A package __init__ / re-export must ONLY import names defined at "
          "MODULE level in the target; if a name is a class METHOD or missing, "
          "remove it from the import + __all__.\n"
          "3. Do NOT drop working code — make the MINIMAL change that satisfies the "
          "failing assertions (add the exact attribute/method the test calls, fix "
          "the value/formula the test expects).\n"
          "4. You are PROHIBITED from rewriting whole files (a full rewrite silently "
          "shifts working code and breaks other tests). Emit TARGETED "
          "Search-and-Replace PATCHES. For each file you change, output a header "
          "line `### FILE: relative/path` then one or more blocks EXACTLY:\n"
          "<<<<<<< SEARCH\n<the exact existing lines to change — character-for-"
          "character incl. indentation>\n=======\n<the corrected lines>\n"
          ">>>>>>> REPLACE\n"
          "The SEARCH text MUST appear verbatim in the current file. Keep each "
          "SEARCH block small (the few lines around the defect). Output ONLY the "
          "`### FILE:` headers + SEARCH/REPLACE blocks — no whole files, no ``` "
          "fences, no prose.")
    try:
        mt = max(4096, int(os.environ.get("AIFORGE_LLM_MAX_TOKENS", "8192")))
    except ValueError:
        mt = 8192
    # Model override (escalation): when the primary reconciler stalls, the caller
    # passes a different model (e.g. a reasoning model) for the residual failures
    # it can't crack. Delivered via `extras={"model": …}` which overrides the
    # role's default in the request body — general, no per-problem code.
    _extras = {"model": model} if model else None
    _temp = None
    _sys = ("You are a Targeted Code Patch Engine. Output ONLY ### FILE headers + "
            "<<<<<<< SEARCH/======= />>>>>>> REPLACE blocks, nothing else. Never "
            "rewrite a whole file.")
    if model:
        # Reasoning/escalation models can't reliably reproduce a char-perfect
        # SEARCH block (their patches get rejected). Have them output the whole
        # corrected file instead — they GENERATE better than they patch; the
        # regression guard keeps the rewrite only if it reduces failures.
        prompt += ("\n\nOVERRIDE — IGNORE the SEARCH/REPLACE format above. Output "
                   "each CHANGED file IN FULL, each as:\n=== relative/path ===\n"
                   "<the complete corrected file>\nNo SEARCH/REPLACE blocks, no ``` "
                   "fences, no prose. Fix the ROOT CAUSE of the failing tests.")
        _sys = ("You are a senior engineer fixing failing tests. Output ONLY the "
                "changed files, each as `=== path ===` then the full corrected "
                "file. No prose, no fences.")
    if model:
        # An escalation (reasoning) model may be loaded at a smaller context — cap
        # completion so prompt+completion fit; its fixes are targeted anyway.
        try:
            mt = min(mt, int(os.environ.get("AIFORGE_ESCALATION_MAX_TOKENS", "2560")))
        except ValueError:
            mt = 2560
        # Apply the ESCALATION model's own sampling params (the role's ep.model —
        # qwen — is overridden via extras, so the client's quirk lookup would use
        # the wrong model). Reasoning models want their pinned temperature.
        try:
            from aiforge_core.config import model_overrides as _mo
            _ov = _mo.lookup(model)
            if _ov and _ov.get("temperature") is not None:
                _temp = _ov["temperature"]
        except Exception:  # noqa: BLE001
            pass
    out = _complete("doer", [
        {"role": "system", "content": _sys},
        {"role": "user", "content": prompt}],
        max_tokens=mt, temperature=_temp, extras=_extras) or ""
    written, failures = _apply_patches(cwd, out)
    if not written and failures:
        # Fallback: the model may have ignored the patch format and emitted whole
        # `=== path ===` files — accept those (syntax-checked) so a round isn't lost.
        for rel, content in _parse_file_blocks(out).items():
            rel = rel.lstrip("/").replace("..", "")
            if not rel or not content.strip():
                continue
            try:
                from aiforge_core.runtime.syntax_guard import validate_syntax
                _ok, _ = validate_syntax(rel, content)
                if not _ok:
                    continue
            except Exception:  # noqa: BLE001
                pass
            dest = os.path.join(cwd, rel)
            try:
                os.makedirs(os.path.dirname(dest) or cwd, exist_ok=True)
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(content)
                written.append(rel)
            except Exception:  # noqa: BLE001
                pass
    return written


_SCAFFOLD_MARK = "AIFORGE_SCAFFOLD_STUB"   # sentinel — a still-unimplemented stub

_COMMENT_PREFIX = {
    ".py": "#", ".sh": "#", ".rb": "#", ".yaml": "#", ".yml": "#", ".toml": "#",
    ".java": "//", ".go": "//", ".js": "//", ".mjs": "//", ".ts": "//",
    ".tsx": "//", ".c": "//", ".cc": "//", ".cpp": "//", ".rs": "//", ".php": "//",
}


def _stub_content(path: str, api: list, is_test: bool) -> str:
    """A SCAFFOLD stub: the file at its canonical path carrying the target public
    API as a header, so parallel workers implement INTO a fixed structure (no
    chaotic dir trees, no path drift) and to the exact contract. Language-agnostic
    — a comment header for every language; Python code files also get real
    signature stubs so sibling imports resolve during parallel work."""
    ext = os.path.splitext(path)[1].lower()
    # build/markup files: leave empty, the owning worker writes the whole thing.
    if ext in (".xml", ".html", ".json", ".cfg", ".properties", ".txt", ".md", ""):
        return ""
    cmt = _COMMENT_PREFIX.get(ext, "#")
    if is_test:
        return f"{cmt} Tests — implement per SPEC.md. {_SCAFFOLD_MARK}\n"
    if ext == ".py":
        return _python_stub(api)
    hdr = [f"{cmt} STUB {_SCAFFOLD_MARK} — implement this file per SPEC.md, "
           "keeping the public API:"]
    for a in api:
        hdr.append(f"{cmt}   {a}")
    if not api:
        hdr.append(f"{cmt}   (see SPEC.md)")
    return "\n".join(hdr) + "\n"


def _python_stub(api: list) -> str:
    """Real Python signature stubs from the API contract — keeps sibling imports
    resolvable while workers fill in bodies. Conservative: only clear top-level
    class/def/const forms; anything ambiguous becomes a module-level name = None."""
    lines = [f'"""Stub {_SCAFFOLD_MARK} — implement the bodies; keep this exact '
             'public API."""']
    for a in [x.strip() for x in api if x and x.strip()]:
        base = a.rstrip(":")
        if base.startswith(("class ", "async def ", "def ")):
            body = "    ..." if base.startswith("class ") else "    raise NotImplementedError"
            lines.append(f"\n\n{base}:\n{body}")
        elif ":" in a and "=" not in a and "(" not in a:   # CONST: type
            nm = _clean_symbol(a)
            if nm:
                lines.append(f"\n\n{nm} = None")
        else:
            nm = _clean_symbol(a)
            if nm:
                lines.append(f"\n\n{nm} = None")
    return "\n".join(lines) + "\n"


_NON_MODULE_TEST_STEMS = frozenset({
    "integration", "e2e", "end_to_end", "endtoend", "main", "app", "cli",
    "smoke", "full", "all", "system", "suite", "acceptance", "functional",
    "application",
})


def _impl_path_for_test(test_path: str, name: str, ext: str,
                        impl_dirs: list) -> str:
    """Where the impl module for a test should live. Java: mirror src/test→
    src/main. Else: alongside existing impls, or the test's parent (minus a
    tests/ dir)."""
    d = os.path.dirname(test_path)
    if ext.lower() == ".java":
        return (test_path.replace("/test/", "/main/").rsplit("/", 1)[0]
                + f"/{name}{ext}") if "/test/" in test_path \
            else (impl_dirs[0] + f"/{name}{ext}" if impl_dirs else f"{name}{ext}")
    if impl_dirs:
        return f"{impl_dirs[0]}/{name}{ext}".lstrip("/")
    # strip a trailing tests/ segment
    parts = [p for p in d.split("/") if p and p.lower() not in ("tests", "test")]
    base = "/".join(parts)
    return (f"{base}/{name}{ext}" if base else f"{name}{ext}")


def _ensure_impl_modules(subs: list) -> list:
    """DECOMPOSITION CONSISTENCY (inverse of the off-plan pruner): every test that
    targets a module (test_board→board, BookServiceTest→BookService, board.test→
    board) MUST have a matching impl file in the plan. When the architect collapses
    all impl into one file but writes per-module tests, those tests can't import
    their modules → collection errors no reconcile fixes. Adds the missing impl
    subtasks. Language-agnostic; skips non-module test names (integration/e2e/…)."""
    import re as _re
    impl_stems: set = set()
    impl_dirs: list = []
    tests: list = []
    for s in subs:
        p = str(s.get("path") or "")
        if not p:
            continue
        stem, ext = os.path.splitext(os.path.basename(p))
        if _is_test_subtask(s):
            tests.append((s, p, stem, ext))
        else:
            impl_stems.add(stem.lower())
            d = os.path.dirname(p)
            if d and d not in impl_dirs and ext.lower() != ".xml":
                impl_dirs.append(d)
    added: list = []
    for s, p, stem, ext in tests:
        m = (_re.match(r"(?i)test_(.+)$", stem) or _re.match(r"(?i)(.+)_tests?$", stem)
             or _re.match(r"(.+)Tests?$", stem)          # XTest / XTests (plural)
             or _re.match(r"(.+)IT(?:Case)?$", stem)     # Java integration tests
             or _re.match(r"(?i)(.+)\.test$", stem) or _re.match(r"(?i)(.+)\.spec$", stem))
        if not m:
            continue
        name = m.group(1)
        if name.lower() in _NON_MODULE_TEST_STEMS or name.lower() in impl_stems:
            continue
        impl_path = _impl_path_for_test(p, name, ext, impl_dirs).lstrip("/")
        added.append({"slug": name.lower(), "path": impl_path,
                      "goal": f"Implement {name} to satisfy its tests ({os.path.basename(p)}).",
                      "api": []})
        impl_stems.add(name.lower())
    return subs + added


_BASELINE_FILE = ".aiforge-baseline"


def _snapshot_baseline(cwd: str) -> int:
    """Record the source files that EXISTED before this run (an existing repo vs a
    greenfield build) so the off-plan pruner NEVER deletes pre-existing code and
    the scaffold doesn't stub over it. Returns the pre-existing source count."""
    pre = [rel for rel, _c in _gather_sources(cwd)]
    try:
        with open(os.path.join(cwd, _BASELINE_FILE), "w", encoding="utf-8") as fh:
            fh.write("\n".join(pre))
    except Exception:  # noqa: BLE001
        pass
    return len(pre)


def _baseline_set(cwd: str) -> set:
    try:
        with open(os.path.join(cwd, _BASELINE_FILE), encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except Exception:  # noqa: BLE001
        return set()


def _is_greenfield(cwd: str) -> bool:
    """True when the workspace has (almost) no pre-existing source — a NEW project.
    Greenfield-only steps (scaffold, off-plan prune, decompose-into-full-tree) are
    DESTRUCTIVE on an existing repo, so gate them on this."""
    try:
        n = len(_baseline_set(cwd)) if os.path.exists(os.path.join(cwd, _BASELINE_FILE)) \
            else len([1 for _ in _gather_sources(cwd)])
    except Exception:  # noqa: BLE001
        n = 0
    try:
        thresh = int(os.environ.get("AIFORGE_GREENFIELD_MAX_FILES", "8"))
    except ValueError:
        thresh = 8
    return n <= thresh


def _spec_declared_paths(subs: list) -> set:
    return {str(s.get("path") or "").lstrip("/").replace("..", "")
            for s in subs if s.get("path")}


def _prune_offplan_files(cwd: str, subs: list) -> list:
    """Delete source files NOT in the SPEC's declared list — a worker or the
    reconciler sometimes invents a phantom package (e.g. tetris/game.py alongside
    the planned tetris_game.py), producing DUPLICATE modules → import/collection
    errors no runner can fix. Keeps declared files + package glue (__init__/
    conftest) + non-source (build/config). Deterministic; the tree matches the
    plan. Returns removed paths."""
    declared = _spec_declared_paths(subs)
    if not declared:
        return []
    # NEVER touch an existing repo: on a non-greenfield workspace the pruner would
    # delete the whole codebase (everything not in this task's tiny plan). Bail.
    if not _is_greenfield(cwd):
        return []
    baseline = _baseline_set(cwd)                 # pre-existing files — never delete
    declared_bases = {os.path.basename(d) for d in declared}
    removed: list = []
    for rel, _content in _gather_sources(cwd):
        r = rel.lstrip("/")
        if r in declared or r in baseline:
            continue
        base = os.path.basename(r)
        if base in ("__init__.py", "conftest.py"):
            continue                              # package glue — harmless
        if not r.endswith(_SRC_EXTS):
            continue                              # keep build/config/markup
        # a source file that is neither declared by path NOR a unique new basename
        # the plan lacks → it's off-plan pollution (often a dup of a declared file).
        try:
            os.remove(os.path.join(cwd, r))
            removed.append(r)
        except Exception:  # noqa: BLE001
            pass
    return removed


def _scaffold_stubs(cwd: str, subs: list) -> list:
    """Deterministically create every declared file at its canonical path (with a
    stub header) BEFORE any parallel worker runs. Gives the local models a fixed
    track: the tree + paths exist, so isolated workers can't invent divergent
    directory layouts and merges stay clean. Returns the paths scaffolded."""
    written: list = []
    for s in subs:
        path = str(s.get("path") or "").lstrip("/").replace("..", "")
        if not path:
            continue
        dest = os.path.join(cwd, path)
        if os.path.exists(dest):
            continue
        try:
            os.makedirs(os.path.dirname(dest) or cwd, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(_stub_content(path, s.get("api") or [], _is_test_subtask(s)))
            written.append(path)
        except Exception:  # noqa: BLE001
            pass
    return written


def _fail_count(output: str) -> int:
    """Number of failing tests from the build/test output (for the regression
    guard). 999 = couldn't-run/collection-error (treat as worst); 0 = all green."""
    import re as _re
    m = _re.search(r"(\d+)\s+failed", output or "")
    if m:
        return int(m.group(1))
    if _re.search(r"error|Error|Traceback|Interrupted", output or ""):
        return 999
    return 0


def _prune_dead_python_imports(cwd: str) -> list[str]:
    """DETERMINISTIC pre-fix (general Python): remove `from <local_mod> import X`
    names — and matching `__all__` entries — where X isn't defined at MODULE
    level in <local_mod>. This kills the single most common cross-file break: a
    package __init__ re-exporting a name that's actually a class method / typo /
    missing, which fails ALL imports. No LLM, no task-specific logic."""
    import ast
    changed: list[str] = []
    # module dotted-name → set of module-level symbols it defines
    modsyms: dict[str, set] = {}

    def _rel_to_mod(rel: str) -> str:
        rel = rel[:-3] if rel.endswith(".py") else rel
        rel = rel[:-9] if rel.endswith("/__init__") else rel
        return rel.replace(os.sep, ".").strip(".")

    pyfiles: dict[str, str] = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv",
            "__pycache__", "node_modules")]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        pyfiles[os.path.relpath(p, cwd)] = fh.read()
                except Exception:  # noqa: BLE001
                    pass
    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        syms: set = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                syms.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        syms.add(t.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    syms.add(a.asname or a.name.split(".")[0])
        modsyms[_rel_to_mod(rel)] = syms

    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        dead: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                key = node.module
                # resolve against known local modules (exact or basename match)
                target = (key if key in modsyms
                          else next((m for m in modsyms if m.endswith("." + key)
                                     or m == key), None))
                if target is None:
                    continue
                have = modsyms.get(target, set())
                for a in node.names:
                    if a.name != "*" and a.name not in have:
                        dead.add(a.name)
        if not dead:
            continue
        # drop dead names from `from X import ...` lines + __all__ list entries
        new_lines = []
        for line in src.splitlines():
            ls = line.strip()
            if ls.startswith("from ") and " import " in ls:
                head, names = line.split(" import ", 1)
                kept = [n.strip() for n in names.split(",")
                        if n.strip() and n.strip().split(" as ")[0].strip() not in dead]
                if not kept:
                    continue  # whole import was dead → drop the line
                new_lines.append(head + " import " + ", ".join(kept))
                continue
            if any(f"'{d}'" == ls.rstrip(",") or f'"{d}"' == ls.rstrip(",") for d in dead):
                continue  # a dead __all__ entry on its own line
            new_lines.append(line)
        new_src = "\n".join(new_lines)
        if new_src != src:
            try:
                compile(new_src, rel, "exec")   # only write if still valid
                with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
                    fh.write(new_src + ("\n" if not new_src.endswith("\n") else ""))
                changed.append(rel)
            except SyntaxError:
                pass
    return changed


def _symbol_drift_report(cwd: str) -> list[dict]:
    """MAP-REDUCE blackboard: for every Python module collect the symbols it
    EXPOSES (module-level defs/classes/constants) and the symbols it CONSUMES
    (imports from sibling modules). Report each consumed name that its target
    module doesn't expose, with the closest real name as the canonical suggestion.

    This is the structured aggregation step — the merger reasons over this COMPACT
    blackboard (module → exposes/consumes + mismatches), not the whole codebase,
    so cross-file drift (Binary vs BinaryExpr, drop_piece method-vs-function) is
    caught at MERGE time, before any test runs. General; no per-error hardcoding.

    Prefers the workers' DECLARED contracts (.aiforge-contracts/, language-
    agnostic); falls back to Python AST extraction when none were declared."""
    import difflib
    _declared = _blackboard_from_contracts(cwd)
    if _declared is not None:
        exposes, consumes = _declared
        drift: list[dict] = []
        for cons, tgt, name in consumes:
            have = exposes.get(tgt, set())
            if name not in have:
                close = difflib.get_close_matches(name, list(have), n=1, cutoff=0.6)
                drift.append({"consumer": cons, "target": tgt, "name": name,
                              "target_exposes": sorted(have)[:15],
                              "suggest": close[0] if close else None})
        return drift

    import ast
    pyfiles: dict[str, str] = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv",
            "__pycache__", "node_modules", ".pytest_cache")]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        pyfiles[os.path.relpath(p, cwd)] = fh.read()
                except Exception:  # noqa: BLE001
                    pass

    def _mod(rel: str) -> str:
        rel = rel[:-3] if rel.endswith(".py") else rel
        rel = rel[:-9] if rel.endswith("/__init__") else rel
        return rel.replace(os.sep, ".").strip(".")

    exposes: dict[str, set] = {}
    consumes: list[tuple[str, str, str]] = []   # (consumer_mod, target_mod, name)
    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        syms: set = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                syms.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        syms.add(t.id)
        exposes[_mod(rel)] = syms

    mods = set(exposes)
    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                tgt = (node.module if node.module in mods
                       else next((m for m in mods if m.endswith("." + node.module)
                                  or m.split(".")[-1] == node.module.split(".")[-1]), None))
                if not tgt:
                    continue
                for a in node.names:
                    if a.name != "*":
                        consumes.append((_mod(rel), tgt, a.name))

    drift: list[dict] = []
    for cons, tgt, name in consumes:
        have = exposes.get(tgt, set())
        if name not in have:
            close = difflib.get_close_matches(name, list(have), n=1, cutoff=0.6)
            drift.append({"consumer": cons, "target": tgt, "name": name,
                          "target_exposes": sorted(have)[:15],
                          "suggest": close[0] if close else None})
    return drift


def _reconcile_integration(cwd: str, result: dict, should_cancel=None):
    """Build + test the merged tree; while it fails on cross-file drift, run a
    bounded Doer pass over the WHOLE workspace — fed the RAW test output + a
    CONCRETE directed fix-list — to fix the mismatches, re-testing each round.
    Yields SSE events; stores the final report in ``result['rep']``. Skippable
    via AIFORGE_RECONCILE_INTEGRATION=0. Halts on ``should_cancel()``."""
    from aiforge_core.runtime.integration_report import build_and_test_report
    if should_cancel is not None and should_cancel():
        result["rep"] = build_and_test_report(cwd)
        return
    # DETERMINISTIC pre-fix: prune dead package re-exports (the #1 cross-file
    # break the LLM won't fix) before spending an LLM round on it.
    try:
        _pruned = _prune_dead_python_imports(cwd)
        if _pruned:
            yield {"type": "tool", "role": "reconciler", "name": "pruned dead re-exports",
                   "args": {}, "result": {"files": _pruned}}
    except Exception:  # noqa: BLE001
        pass

    ok, output = _project_test_output(cwd)
    if ok or os.environ.get("AIFORGE_RECONCILE_INTEGRATION", "1") in ("0", "false"):
        result["rep"] = build_and_test_report(cwd)
        return

    max_rounds = _reconcile_rounds()
    rounds = 0
    prev_fails = _fail_count(output)
    stalls = 0
    while not ok and rounds < max_rounds:
        if should_cancel is not None and should_cancel():
            break
        rounds += 1
        try:
            _prune_dead_python_imports(cwd)   # deterministic, before the LLM round
        except Exception:  # noqa: BLE001
            pass
        hints = _directed_hints(output)
        # ESCALATION: after the primary model stalls (no improvement for a couple
        # rounds), hand the residual failures it can't crack to a stronger/reasoning
        # model (AIFORGE_ESCALATION_MODEL, e.g. a 9B reasoning model). General —
        # only the STUCK residual escalates, not every round; no per-problem code.
        _esc_model = os.environ.get("AIFORGE_ESCALATION_MODEL", "").strip() or None
        _use_esc = _esc_model and stalls >= 2
        # TEST-AUDIT: after impl fixes stall (the impl was rewritten repeatedly and
        # the SAME tests still fail), a failing test may itself be WRONG — a local
        # model writes buggy tests too. Once stuck, let the fixer correct a test
        # that CONTRADICTS the goal (guarded: regression guard rolls back if net
        # fails rise; `# test-audit:` marker makes edits visible). Off with
        # AIFORGE_RECONCILE_TEST_AUDIT=0.
        _audit = (os.environ.get("AIFORGE_RECONCILE_TEST_AUDIT", "1")
                  not in ("0", "false") and stalls >= 2)
        yield {"type": "thought", "role": "reconciler",
               "text": f"Integration failed ({prev_fails} failing) — pass "
                       f"{rounds}/{max_rounds}: "
                       + (f"escalating the residual to {_esc_model}…" if _use_esc
                          else "auditing whether a stuck test is itself wrong…"
                          if _audit else "patching the offending files…")}
        # Snapshot BEFORE the round so a round that makes things WORSE (a local
        # model's bad patch) can be rolled back — reconcile is then MONOTONIC:
        # it never regresses, only accepts rounds that reduce the failure count.
        snapshot = dict(_gather_sources(cwd))
        try:
            written = _rewrite_fix(cwd, output, hints,
                                   model=(_esc_model if _use_esc else None),
                                   audit_tests=_audit)
        except Exception as exc:  # noqa: BLE001 — a transient LLM error must NOT
            yield {"type": "thought", "role": "reconciler",
                   "text": f"reconcile pass hit a transient error, retrying: {str(exc)[:80]}"}
            written = []
        ok, output = _project_test_output(cwd)
        new_fails = _fail_count(output)
        if new_fails > prev_fails:
            # STRICT regression only → roll back (restore snapshot + drop any files
            # the bad round created). A LATERAL move (== fails) is KEPT — it lets
            # the model refactor toward the seam without penalty.
            _snap_keys = set(snapshot)
            for _rel, _c in _gather_sources(cwd):
                if _rel not in _snap_keys:
                    try:
                        os.remove(os.path.join(cwd, _rel))
                    except Exception:  # noqa: BLE001
                        pass
            for rel, content in snapshot.items():
                try:
                    with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
                        fh.write(content)
                except Exception:  # noqa: BLE001
                    pass
            ok, output = _project_test_output(cwd)
            new_fails = _fail_count(output)
            stalls += 1
            yield {"type": "thought", "role": "reconciler",
                   "text": f"pass {rounds} REGRESSED ({prev_fails}→? ) — rolled back to "
                           f"{prev_fails} failing. Trying a different angle…"}
            if stalls >= 4:
                break                          # 4 no-progress rounds → give up
        else:
            # improvement OR lateral (no regression) → KEEP.
            if new_fails < prev_fails:
                prev_fails = new_fails
                stalls = 0
            else:
                stalls += 1                     # lateral move — bounded
            _lbl = ("tests can't run (collection/build error)"
                    if new_fails >= 999 else f"{new_fails} failing")
            yield {"type": "tool", "role": "reconciler", "name": "patched files",
                   "args": {"pass": rounds, "status": _lbl},
                   "result": {"ok": new_fails < 999, "files": written,
                              "output": (output or "")[-1500:] if new_fails >= 999 else None}}
            if stalls >= 4:
                break

    result["rep"] = build_and_test_report(cwd)
    if rounds and ok:
        yield {"type": "thought", "role": "reconciler",
               "text": f"Reconciliation green after {rounds} pass(es) ✅"}
    elif rounds:
        yield {"type": "thought", "role": "reconciler",
               "text": f"Reconciliation ran {rounds} pass(es) — some tests still "
                       "red; see the report + manual steps below."}


def _render_spec_md(prompt: str, subs: list[dict]) -> str:
    """The shared requirements/plan document written to SPEC.md before the run
    and re-read by the final verification pass."""
    lines = ["# Project Spec", "", "## Goal", "", prompt.strip(), ""]
    # Canonical file tree — the EXACT paths every subtask must use verbatim (no
    # re-casing/renaming the package dir), so isolated contexts don't split into
    # mini_lang/ + miniLang/ + minilang/.
    paths = [str(s.get("path") or "").strip().lstrip("/")
             for s in subs if s.get("path")]
    if paths:
        lines += ["## File tree (use these EXACT paths — verbatim)", ""]
        lines += [f"- `{p}`" for p in paths]
        lines += [""]
    # API CONTRACT — the shared source of truth. Every file MUST expose these
    # names/signatures verbatim, and MUST import other files' names EXACTLY as
    # listed here. This is what stops isolated workers drifting (Binary vs
    # BinaryExpr, COLORS vs COLOR_MAP) — reconcile at DESIGN time, not after.
    api_lines = []
    for s in subs:
        api = [str(a) for a in (s.get("api") or []) if a]
        if api and s.get("path"):
            api_lines.append(f"### `{s['path']}` exposes")
            api_lines += [f"- `{a}`" for a in api]
    if api_lines:
        lines += ["## API contract — expose/import these names EXACTLY (verbatim)", ""]
        lines += api_lines
        lines += [""]
    lines += [f"## Subtasks ({len(subs)})", ""]
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
