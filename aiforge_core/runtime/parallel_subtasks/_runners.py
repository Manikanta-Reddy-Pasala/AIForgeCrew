"""Per-subtask executors (default/lightweight run_one) + run_subtasks_parallel.

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
        + "Keep the change focused on THIS subtask only; other subtasks handle the rest."
        + ("\n\n⚠ The previous attempt ran out of budget before finishing — it was "
           "too big for one pass. This time build the CORE first: the smallest "
           "COMPLETE, working, testable slice of the goal. Get that green, THEN add "
           "extras only if you have room. Do not start broad and leave everything half-done."
           if subtask.get("_too_big") else ""))
    _retry_err = str(subtask.get("_retry_error") or "").strip()
    if _retry_err:
        msg += (f"\n\n⚠ YOUR PREVIOUS ATTEMPT FAILED with:\n{_retry_err[:800]}\n"
                "Fix exactly that this time.")

    def complete_fn(role, convo):
        return _complete(role, convo)

    ok = False
    try:
        for ev in run_chat_agent([{"role": "user", "content": msg}], cwd=worktree,
                                 role="doer", complete_fn=complete_fn,
                                 strict_finish=True):
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

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._orchestrate import run_parallel
from ._planning import _ensure_git_workspace
from ._worktree import _emit, _git, default_integration_test, default_validate_one, log
def _decompose(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._decompose(*a, **k)
