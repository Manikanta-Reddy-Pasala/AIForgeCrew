"""Per-subtask executors (default/lightweight run_one) + run_subtasks_parallel.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import contextlib
import os
import threading

from ._runners_write import (  # noqa: F401  # re-exported
    _CONTRACT_RULES,
    _CPP_EXTS_R,
    _FILE_BLOCK_RE,
    _TEST_CALL_NOISE,
    _TEST_IS_SPEC,
    _enforce_target_path,
    _in_scope,
    _inside,
    _lang_rules,
    _marker_path,
    _marker_sections,
    _parse_file_blocks,
    _remap_to_canonical,
    _required_api_from_tests,
    _subtask_prompt,
    _syntax_rejection,
    _tests_block,
    _write_subtask_files,
)


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
        if subtask.get("_patch_retry"):
            msg += (" The file is already on disk. Do NOT regenerate it. "
                    "Apply a small patch that fixes the error above.")
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
            if ev.get("type") == "stopped" \
                    and ev.get("reason") == "llm_request_fails":
                return {"ok": False, "error": ev.get("error"),
                        "reason": "llm_request_fails"}
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
    # Claim it ATOMICALLY (todo/blocked/… → in_progress): the in-process
    # _INFLIGHT guard above does not stop the runner PROCESS from claiming the
    # same ticket, and a bare status flip left no claim for the reaper to see
    # renewed — it requeued the live run.
    if _store.claim_ticket(tid) is None:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(tid)
        return {"ok": False, "error": "already running (claimed by another run)"}
    from aiforge_core.tickets.lease import hold_claim, worktree_lock
    _stack = contextlib.ExitStack()
    # The claim's `lost` event (cancelled / taken over) is the run's cancel:
    # subtasks stop, and their model waits end, when it fires.
    _lost = _stack.enter_context(hold_claim(tid))
    _root = _root_identifier_of(ticket)
    if not _stack.enter_context(worktree_lock(_root)):
        _stack.close()
        _set_status(_store, tid, "todo")
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(tid)
        return {"ok": False, "error": f"worktree {_root} is in use by another run"}
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
                           integration_test=default_integration_test,
                           should_cancel=_claim_cancel(_lost))
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
        _stack.close()
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(tid)


def _claim_cancel(lost):
    """A should_cancel check from hold_claim's ``lost`` event."""
    if lost is None or not hasattr(lost, "is_set"):
        return None
    return lost.is_set


def _root_identifier_of(ticket) -> str:
    try:
        from aiforge_core.runtime.workspace import _root_ticket
        return _root_ticket(ticket).identifier
    except Exception:  # noqa: BLE001
        return str(getattr(ticket, "identifier", ticket))


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._orchestrate import run_parallel
from ._planning import _ensure_git_workspace
from ._worktree import _emit, _git, default_integration_test, default_validate_one


def _decompose(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._decompose(*a, **k)
