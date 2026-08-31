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

def _doer_message(subtask: dict, spec_md: str, path: str, goal: str) -> str:
    accept = subtask.get("acceptance") or []
    scope = subtask.get("scope_allowlist_globs") or []
    # Hard path pin: every subtask runs in its OWN fresh context, so without an
    # exact-path command each one re-guesses the package dir casing (minilang/
    # vs mini_lang/ vs miniLang/) and the merge ends up with 3 split dirs.
    path_pin = (
        f"TARGET FILE (create EXACTLY this path, byte-for-byte — do NOT rename, "
        f"re-case, or re-spell the directory or file; other subtasks use the "
        f"SAME paths from SPEC.md): {path}\n" if path else "")
    msg = (
        (f"PROJECT SPEC (shared context — build YOUR slice to fit it; use the "
         f"EXACT file/dir paths it lists, verbatim):\n"
         f"{spec_md.strip()[:6000]}\n\n---\n\n"
         if spec_md and spec_md.strip() else "")
        + f"Implement this subtask, then build + test it.\n\n{path_pin}GOAL: {goal}\n"
        + ("ACCEPTANCE:\n" + "\n".join(f"- {a}" for a in accept) + "\n"
           if accept else "")
        + ("SCOPE (only touch these): " + ", ".join(scope) + "\n" if scope else "")
        + "Keep the change focused on THIS subtask only; other subtasks handle "
          "the rest."
        + ("\n\n⚠ The previous attempt ran out of budget before finishing — it was "
           "too big for one pass. This time build the CORE first: the smallest "
           "COMPLETE, working, testable slice of the goal. Get that green, THEN "
           "add extras only if you have room. Do not start broad and leave "
           "everything half-done." if subtask.get("_too_big") else ""))
    retry_err = str(subtask.get("_retry_error") or "").strip()
    if retry_err:
        msg += (f"\n\n⚠ YOUR PREVIOUS ATTEMPT FAILED with:\n{retry_err[:800]}\n"
                "Fix exactly that this time.")
    return msg


def _drive_doer(msg: str, worktree: str, own_scope, complete_fn) -> dict:
    """Run the chat loop; ``{ok}`` or ``{ok: False, error}``."""
    from aiforge_core.runtime.chat_agent import run_chat_agent
    ok = False
    try:
        for ev in run_chat_agent([{"role": "user", "content": msg}],
                                 cwd=worktree, role="doer",
                                 complete_fn=complete_fn,
                                 scope_globs=own_scope, strict_finish=True):
            if ev.get("type") == "error":
                return {"ok": False, "error": ev.get("text")}
            if ev.get("type") == "message" and not ev.get("awaiting_input"):
                # The runaway-safety-cap stop also emits a plain message — that
                # is a FAILURE (the Doer thrashed without finishing), not success.
                ok = not (ev.get("text") or "").startswith("(stopped:")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": ok}


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
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"import: {exc}"}
    path = str(subtask.get("path") or "").strip().lstrip("/")
    goal = subtask.get("goal") or subtask.get("slug") or "implement the subtask"
    msg = _doer_message(subtask, spec_md, path, goal)
    # HARD file ownership: restrict this subtask's WRITES to the file(s) it owns
    # so two subtasks can NEVER edit the same file — the reconcile is then a
    # trivial disjoint union, not a same-file merge. An explicit
    # scope_allowlist_globs wins, else the subtask's single target ``path``; a
    # phase/decompose subtask with neither stays unscoped (nothing to pin).
    own_scope = (subtask.get("scope_allowlist_globs")
                 or ([path] if path else None)) or None
    res = _drive_doer(msg, worktree, own_scope,
                      lambda role, convo: _complete(role, convo))
    # Code-level path enforcement — the prompt pin isn't 100% on a local model,
    # so if the agent wrote the file at a re-cased/renamed path (miniLang/…,
    # mini_lang/…, pysyntax/…) instead of the exact target, MOVE it to the
    # canonical path here. Guarantees every subtask's file lands where SPEC.md
    # + the other subtasks expect, so the merge never splits into variant dirs.
    if path:
        _enforce_target_path(worktree, path)
    return res


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
    for m in re.finditer(r"^===\s*([^\n=]+?)\s*===\n(.*?)(?=(?:^===\s*[^\n=]+?\s*===)|\Z)",
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


import re as _re

# Test-helper / framework / builtin names that are NOT part of the impl's API —
# a `.assertEqual(...)` or `.push_back(...)` on a stdlib type must not be demanded
# of the unit under test.
_TEST_CALL_NOISE = frozenset({
    # test-framework assertions / lifecycle (called on self / the test object)
    "assert", "asserttrue", "assertfalse", "assertequal", "assertnotequal",
    "assertraises", "assertisnone", "assertisnotnone", "assertin", "assertnotin",
    "assertis", "assertalmostequal", "assertgreater", "assertless", "assertthat",
    "expect", "should", "setup", "teardown", "before", "after", "beforeeach",
    "aftereach", "fail",
    # output / language builtins (never the unit's own API)
    "print", "println", "printf", "format", "fmt",
    # Object/base methods every class already inherits — don't demand them
    "tostring", "hashcode", "equals", "clone", "getclass",
})


def _required_api_from_tests(tests_src: str) -> list:
    """Method/function names the TEST source CALLS — the exact surface the impl
    must expose so the test compiles. Language-agnostic: pulls ``.method(`` calls
    and bare ``Name(`` calls, drops the test-framework/builtin noise. Best-effort;
    caps the list so the prompt stays small."""
    if not tests_src:
        return []
    names: list[str] = []
    seen: set = set()
    # dotted method calls (obj.method(...)) — the class's own API
    for m in _re.finditer(r"\.\s*([A-Za-z_]\w*)\s*\(", tests_src):
        nm = m.group(1)
        if nm.lower() not in _TEST_CALL_NOISE and nm not in seen:
            seen.add(nm)
            names.append(nm)
    return names[:24]


_CPP_EXTS_R = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".c++")


def _lang_rules(path: str) -> str:
    """Language-specific coding rules for the target file that a local model
    reliably gets wrong (injected into the impl prompt). C++ TEMPLATES are the
    big one — a template body in a .cpp causes redefinition / undefined-reference
    link errors (observed: DynamicArray<T> split .h + .cpp → won't build)."""
    pl = (path or "").lower()
    if pl.endswith(_CPP_EXTS_R):
        return ("C++ RULE — any TEMPLATE class/function MUST be fully defined in a "
                "HEADER (declaration + method bodies together in the .hpp/.h); do "
                "NOT put template method bodies in a .cpp (it causes redefinition / "
                "undefined-reference link errors). A single self-contained header "
                "for a templated type is correct.\n\n")
    return ""


_TEST_IS_SPEC = (
    "CRITICAL PRINCIPLE — THE TEST IS THE SPECIFICATION.\n"
    "The test file below is the ABSOLUTE GROUND TRUTH for method/attribute "
    "NAMES (incl. leading underscores like `_is_valid_position`), signatures, "
    "return types, exact VALUES and math. Your code MUST make EVERY assertion "
    "pass. Even if a rule looks unconventional (e.g. the test says an O-piece "
    "is 'cyan' not 'yellow', or score == (level+1)*10), match it EXACTLY — "
    "NEVER 'correct'/'standardize' a value the test asserts. If the test calls "
    "`x._foo(a, b)` you define `_foo(self, a, b)`; if it asserts "
    "`grid[0][3] == COLORS['cyan']` your code must produce cyan.\n\n")

_CONTRACT_RULES = (
    "CONTRACT: expose the PUBLIC API listed for your file in the spec's "
    "'API contract' section EXACTLY (same names, signatures, constants), and "
    "when you import/call another file, use the EXACT names it exposes there. "
    "Do not invent variant names — the other files are written to this same "
    "contract.\n\n"
    "Output ONLY the file(s), each as:\n=== relative/path.ext ===\n"
    "<full file content>\n\nNo prose, no explanation.")


def _tests_block(tests_src: str) -> str:
    """The ground-truth tests, plus the API the test CALLS made explicit.

    Test-first divergence guard: the impl gets the test as ground truth, but a
    local model still MISSES a method the test calls (observed: a Java Stack
    test called a method the impl never defined → uncompilable test).
    """
    if not tests_src:
        return ""
    req = _required_api_from_tests(tests_src)
    req_block = (("REQUIRED API — the test CALLS every one of these; your code "
                  "MUST define ALL of them with matching names/arity (missing "
                  "one = the test won't compile):\n" + ", ".join(req) + "\n\n")
                 if req else "")
    return (_TEST_IS_SPEC + f"TESTS (ground truth):\n{tests_src[:6000]}\n\n"
            + req_block + "---\n\n")


def _subtask_prompt(subtask: dict, spec_md: str, path: str, goal: str) -> str:
    retry_err = str(subtask.get("_retry_error") or "").strip()
    existing = subtask.get("_existing_files")
    return (
        (f"⚠ YOUR PREVIOUS ATTEMPT FAILED with:\n{retry_err[:800]}\nFix that this "
         f"time — re-read the SPEC, emit correct, complete code.\n\n---\n\n"
         if retry_err else "")
        + _tests_block(str(subtask.get("_tests") or "").strip())
        # Language rules for the target file (e.g. C++ templates are header-only).
        + _lang_rules(path)
        + (f"EXISTING PROJECT FILES already on disk (REAL, committed — import from "
           f"these using their EXACT class/function/constant names + signatures; do "
           f"NOT guess or invent variant names):\n{existing}\n\n---\n\n"
           if existing else "")
        + (f"PROJECT SPEC (shared — build YOUR slice to fit it; use the EXACT "
           f"file/dir paths it lists):\n{spec_md.strip()[:5000]}\n\n---\n\n"
           if spec_md and spec_md.strip() else "")
        + "Implement this subtask as COMPLETE, runnable file(s) in the language "
          "the target path implies (.py→Python, .java→Java, .go→Go, .ts→"
          "TypeScript, .c/.cpp→C/C++, .rs→Rust, .sh→shell, …).\n\n"
        + (f"TARGET FILE (emit EXACTLY this path, verbatim — do not re-case or "
           f"rename the directory): {path}\n\n" if path else "")
        + f"SUBTASK: {goal}\n\n" + _CONTRACT_RULES)


def _remap_to_canonical(files: dict, path: str) -> dict:
    """This subtask owns exactly ONE file (``path``), so ignore whatever dir the
    model labelled and force the content to the exact target — the block whose
    basename matches, else the first/only block."""
    base = os.path.basename(path)
    chosen = next((c for p, c in files.items()
                   if os.path.basename(p.strip().lstrip("/")) == base), None)
    return {path: chosen if chosen is not None else next(iter(files.values()))}


def _syntax_rejection(rel: str, content: str) -> str | None:
    """Syntax gate (LANGUAGE-AGNOSTIC): lightweight writes files DIRECTLY (no
    file_write / no syntax_guard), and an isolated subtask worktree has no build
    marker so build-validation is skipped — a truncated/broken file (any
    language) would sail through per-subtask and only blow up at the post-merge
    build/test. The guard must never crash the runner."""
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, err = validate_syntax(rel, content)
        return None if ok else f"{rel}: {err}"
    except Exception:  # noqa: BLE001
        return None


def _inside(worktree: str, rel: str) -> bool:
    """Does ``rel`` actually land inside the worktree?

    Deleting ".." from the string is not containment: "/../etc/passwd.py"
    becomes "/etc/passwd.py", which ``os.path.join`` returns UNCHANGED because
    it is absolute — so a path the MODEL labelled its block with could
    overwrite a file outside the subtask's worktree entirely. Check where it
    lands, not how it looks.
    """
    try:
        root = os.path.realpath(worktree)
        dest = os.path.realpath(os.path.join(worktree, rel))
        return os.path.commonpath([root, dest]) == root
    except (ValueError, OSError):  # different drives / unresolvable
        return False


def _write_subtask_files(files: dict, worktree: str, scope: list):
    """``(written_files, rejected, syntax_error)``.

    A scope allowlist REJECTS writes whose relative path matches no glob, so
    out-of-scope files never land; no allowlist preserves current behaviour.
    """
    written_files: list[str] = []
    rejected: list[str] = []
    for rel, content in files.items():
        rel = rel.lstrip("/").replace("..", "")
        if not _inside(worktree, rel):
            rejected.append(rel)
            continue
        if scope and not _in_scope(rel, scope):
            rejected.append(rel)
            continue
        bad = _syntax_rejection(rel, content)
        if bad:
            return written_files, rejected, bad
        dest = os.path.join(worktree, rel)
        try:
            os.makedirs(os.path.dirname(dest) or worktree, exist_ok=True)
            with open(dest, "w") as f:
                f.write(content)
            written_files.append(rel)
        except OSError:
            continue
    return written_files, rejected, None


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
    path = str(subtask.get("path") or "").strip().lstrip("/")
    goal = subtask.get("goal") or subtask.get("slug") or "implement the subtask"
    prompt = _subtask_prompt(subtask, spec_md, path, goal)
    # Generous output budget — a hardcoded 2048 TRUNCATED big files (e.g. a
    # thorough test file) mid-string, landing a SyntaxError that only surfaced at
    # the post-merge integration test. Use the configured cap (default 8192).
    try:
        max_tokens = max(2048, int(os.environ.get("AIFORGE_LLM_MAX_TOKENS",
                                                  "8192")))
    except ValueError:
        max_tokens = 8192
    try:
        out = _complete("doer", [
            {"role": "system", "content": "You are a senior engineer. Output "
             "complete, working code files only, in the === path === format."},
            {"role": "user", "content": prompt}], max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    files = _parse_file_blocks(out or "")
    if not files:
        return {"ok": False, "error": "no file blocks produced"}
    if path:
        files = _remap_to_canonical(files, path)
    written_files, rejected, bad = _write_subtask_files(
        files, worktree, subtask.get("scope_allowlist_globs") or [])
    if bad:
        return {"ok": False, "error": bad}
    if rejected and not written_files:
        return {"ok": False,
                "error": "all writes out of scope: " + ", ".join(rejected),
                "rejected": rejected}
    res = {"ok": bool(written_files), "files": written_files}
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


def _load_or_decompose(ticket, tid, _st) -> list:
    """The ticket's subtasks, decomposing on demand: a fresh ticket has none
    yet, so its title+body is split so "Run in parallel" works straight from
    `todo`."""
    subs = _st.get_subtasks(tid)
    if subs:
        return subs
    prompt = (f"{getattr(ticket, 'title', '')}\n\n"
              f"{getattr(ticket, 'body', '')}").strip()
    decomposed = _decompose(prompt)
    if len(decomposed) >= 2:
        return _st.set_subtasks(tid, decomposed, role="planner")
    return []


def _set_status(_store, tid, status: str) -> None:
    try:
        _store.update_status(tid, status, role="doer")
    except Exception:  # noqa: BLE001
        pass


def _run_workspace(ticket, tid) -> tuple[str, str]:
    """``(worktree, base_branch)``. A ticket that targets a real repo merges
    into its working branch; a standalone ticket gets a per-ticket git
    workspace so the parallel run still works end-to-end."""
    from aiforge_core.runtime.workspace import ensure_branch_and_worktree
    wt = ensure_branch_and_worktree(ticket)
    if wt:
        cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
        return wt, (cur.stdout or "").strip() or "HEAD"
    from aiforge_core.config.paths import config_dir
    ident = getattr(ticket, "identifier", str(tid))
    wt = os.path.join(str(config_dir()), "ticket-workspaces", ident)
    return wt, _ensure_git_workspace(wt)


def run_subtasks_parallel(ticket, *, run_one=None) -> dict:
    """Entry point: decompose-aware parallel run for one ticket. Loads its
    subtasks + working branch, fans them out concurrently, merges. Operator-
    triggered (and gated by AIFORGE_PARALLEL_SUBTASKS for the auto path) so the
    default single-Doer pipeline is never disturbed."""
    from aiforge_core.tickets import store as _store
    from aiforge_core.tickets import subtasks as _st
    tid = getattr(ticket, "id", ticket)
    subs = _load_or_decompose(ticket, tid, _st)
    if not subs:
        return {"ok": True, "total": 0,
                "note": "could not decompose into subtasks"}
    # Guard against a second parallel run for the SAME ticket (concurrent POSTs
    # would collide on the per-slug worktree paths).
    with _INFLIGHT_LOCK:
        if tid in _INFLIGHT:
            return {"ok": False, "error": "already running for this ticket"}
        _INFLIGHT.add(tid)
    # Move the ticket into the working state so its lifecycle status reflects
    # the run (todo → in_progress → done/blocked).
    _set_status(_store, tid, "in_progress")
    try:
        wt, base_branch = _run_workspace(ticket, tid)
        # NOTE: we do NOT touch the process-global AIFORGE_CURRENT_TICKET here.
        # That env is shared across the whole process, so setting it would let a
        # second (different-ticket) concurrent run clobber it and mis-route
        # subtask updates. The orchestrator tracks each subtask's status with an
        # EXPLICIT ticket_id (run_parallel arg → _update) — thread-safe, no
        # global state. The per-subtask Doer's focused prompt has no subtickets
        # array, so it never calls the env-based subtask_update tool.
        agg = run_parallel(wt, base_branch, getattr(ticket, "id", None),
                           subs, run_one or _default_subtask_runner(),
                           validate_one=default_validate_one,
                           integration_test=default_integration_test)
        _emit(getattr(ticket, "id", None), "*", "parallel_review",
              agg.get("review", ""),
              {k: agg.get(k) for k in ("total", "done", "validated", "failed",
                                       "merged", "conflicts")})
        _set_status(_store, tid, "done" if agg.get("ok") else "blocked")
        return agg
    except Exception:
        _set_status(_store, tid, "blocked")
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
