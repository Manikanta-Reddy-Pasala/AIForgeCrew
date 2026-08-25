"""stream_parallel_team chat driver, change emission, test-coverage helpers.

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

def _pin_to_subtask(subs: list, target: str, text: str,
                    note: str) -> tuple[str, str] | None:
    """Attach the mandate to ONE subtask. None when that subtask is gone (the
    caller then treats it as a global steer)."""
    hit = next((s for s in subs if s.get("slug") == target), None)
    if hit is None:
        return None
    mandate = f"\n[MANDATORY user instruction — MUST satisfy]: {text}"
    hit["goal"] = (hit.get("goal") or "") + mandate
    hit["_user_mandate"] = (hit.get("_user_mandate") or []) + [text]
    label = hit.get("path") or target
    return (f"## ⚙ User instruction (MANDATORY) → {label}",
            f"✅ Got it — treating as a **must** for **{label}**"
            + (f" — {note}" if note else "")
            + ". Pinned to that subtask + SPEC; it rebuilds until satisfied.")


def _steer_headings(target: str, note: str) -> tuple[str, str]:
    """``(SPEC heading, user-facing confirmation)`` for a non-subtask steer."""
    if target == "new":
        return ("## ⚙ User instruction (MANDATORY — NEW requirement)",
                "✅ Got it — new **must-have** requirement"
                + (f" — {note}" if note else "")
                + ". Pinned to SPEC; the reconcile pass builds + verifies it.")
    return ("## ⚙ User instruction (MANDATORY — whole build)",
            "✅ Got it — treating as a **must** across the whole build"
            + (f" — {note}" if note else "")
            + ". Pinned to SPEC; every remaining subtask + the reconcile must "
              "satisfy it.")


def _append_spec_mandate(cwd: str, heading: str, text: str) -> None:
    try:
        with open(os.path.join(cwd, "SPEC.md"), "a", encoding="utf-8") as fh:
            fh.write(f"\n\n{heading}\n- **MUST:** {text}\n")
    except Exception:  # noqa: BLE001
        pass
    # Record globally so the reconcile prompt can re-assert it.
    try:
        _USER_MANDATES.setdefault(cwd, []).append(text)
    except Exception:  # noqa: BLE001
        pass


def _apply_steer(text: str, subs: list, cwd: str) -> str:
    """Route ONE steer to its target and pin it. Returns the confirmation."""
    route = _route_steering(text, subs)
    target, note = route["target"], route["note"]
    # A user comment is a MANDATORY requirement, not a hint — the subtask build
    # and the final reconcile MUST satisfy it. A steer naming a subtask that no
    # longer exists falls back to a global one.
    pinned = (_pin_to_subtask(subs, target, text, note)
              if target not in ("global", "new") else None)
    heading, feedback = pinned or _steer_headings(
        "new" if target == "new" else "global", note)
    _append_spec_mandate(cwd, heading, text)
    return feedback


def _cancel_checker_for(session_id):
    def _cancelled() -> bool:
        if session_id is None:
            return False
        try:
            from aiforge_core.runtime import chat_cancel
            return chat_cancel.is_cancelled(session_id)
        except Exception:  # noqa: BLE001
            return False
    return _cancelled


def _steering_drain(session_id, subs: list, cwd: str):
    """Fold any mid-run steering comment into the run — but first ANALYSE it:
    which subtask/topic it targets (or a global change, or an entirely NEW
    requirement) — tell the user how it was read, then route it (annotate that
    subtask's goal + the right SPEC.md section) so the remaining subtasks +
    reconcile pick it up. Yields feedback events."""
    if session_id is None:
        return
    try:
        from aiforge_core.runtime import chat_interject, chat_steer
        if not chat_interject.pending(session_id):
            return
        for raw in chat_interject.drain(session_id):
            text = (raw or "").strip()
            if not text:
                continue
            # Echo the user's steer TEXT (role:steer) so it shows + persists in
            # the UI for team mode too — same as the simple/plan loop.
            yield chat_steer.steer_event(text)
            yield {"type": "thought", "role": "planner",
                   "text": _apply_steer(text, subs, cwd)}
    except Exception:  # noqa: BLE001
        return


def _arm_session(session_id) -> None:
    """Accept mid-run steering for this run, and bind its subprocesses
    (integration build/pytest) to the session so Stop kills them."""
    if session_id is None:
        return
    for module, fn, args in (("chat_interject", "set_steerable",
                              (session_id, True)),
                             ("chat_cancel", "set_active", (session_id,))):
        try:
            mod = __import__(f"aiforge_core.runtime.{module}", fromlist=[fn])
            getattr(mod, fn)(*args)
        except Exception:  # noqa: BLE001
            pass


def _plan_subtasks(prompt: str, subtasks, state: dict):
    """Decompose (if needed) and repair the plan. Leaves the final list in
    ``state['subs']``; an empty list means the caller should bail out."""
    subs = subtasks
    if not subs:
        yield {"type": "thought", "role": "planner",
               "text": "Decomposing into parallel subtasks…"}
        subs = _decompose(prompt)
    if len(subs) < 2:
        # The caller normally falls back to sequential team mode before reaching
        # here; this is the last-resort guard.
        yield {"type": "message", "text":
               "Couldn't split this into parallel subtasks — running normally."}
        state["subs"] = []
        return
    # Backstop: guarantee test coverage so the build can be verified +
    # self-healed even when the planner omitted tests.
    subs = _ensure_test_coverage(subs)
    # Decomposition consistency: every per-module test needs a matching impl file
    # (test_board→board, BookServiceTest→BookService). When the architect
    # collapses impl into one file but writes per-module tests, add the missing
    # impl modules.
    before = len(subs)
    subs = _ensure_impl_modules(subs)
    if len(subs) > before:
        added = [s.get("path") for s in subs[before:]]
        yield {"type": "thought", "role": "planner",
               "text": f"Decomposition fix — {len(subs) - before} test(s) target "
                       f"modules with no impl file; added: {', '.join(added)}"}
    # FILE-OWNERSHIP ENFORCEMENT (don't trust the plan — check with code). Two
    # subtasks owning the SAME file = two agents editing it in parallel = the #1
    # cause of worktree merge conflicts. Fold duplicates into one owner so each
    # file has exactly one author.
    subs, dupes = _enforce_disjoint_files(subs)
    if dupes:
        yield {"type": "thought", "role": "planner",
               "text": f"File-ownership check — folded {dupes} overlapping "
                       "subtask(s) so no two agents edit the same file (conflict "
                       "prevention)."}
    # PLAN REVIEW — a different model checks the file manifest for typos
    # (kvdakade→kvfacade), near-duplicate/missing modules, scope creep BEFORE any
    # code is built (a patch-reconcile can't fix a structural naming error later).
    try:
        subs, note = review_gates.review_plan(prompt, subs)
        if note:
            yield {"type": "thought", "role": "reviewer", "text": f"🔍 {note}"}
    except Exception as exc:  # noqa: BLE001
        log.debug("plan review skipped: %s", exc)
    state["subs"] = subs
    yield {"type": "subtasks", "items": [
        {"slug": s.get("slug") or f"sub-{i+1}",
         "goal": s.get("goal") or "", "status": "pending"}
        for i, s in enumerate(subs)]}


def _write_spec(prompt: str, subs: list, cwd: str, state: dict):
    """Requirements/plan document: persist the enhanced spec + the subtask
    breakdown to SPEC.md in the workspace BEFORE any subtask runs. It's the
    single source of truth — fed into every per-subtask fresh context (so each
    isolated context knows the overall goal) and re-read by the final
    verification pass to confirm nothing was dropped."""
    spec_md = _render_spec_md(prompt, subs)
    # SPEC REVIEW — check the spec before any code is built (contradictions,
    # ambiguity, missing cases, scope creep). Refines it if needed.
    try:
        spec_md, note = review_gates.review_spec(prompt, spec_md)
        if note:
            yield {"type": "thought", "role": "reviewer", "text": f"🔍 {note}"}
    except Exception as exc:  # noqa: BLE001
        log.debug("spec review skipped: %s", exc)
    state["spec_md"] = spec_md
    try:
        with open(os.path.join(cwd, "SPEC.md"), "w", encoding="utf-8") as fh:
            fh.write(spec_md)
        yield {"type": "thought", "role": "planner",
               "text": f"Wrote SPEC.md ({len(subs)} subtasks) — the shared "
                       "requirements doc each subtask builds against."}
    except Exception as exc:  # noqa: BLE001 — must be VISIBLE, not a debug log
        # A silent skip here is how runs ended up spec-less with no trace
        # (unwritable cwd etc.) — surface it so the operator can fix the cause.
        log.warning("SPEC.md write failed in %s: %s", cwd, exc)
        yield {"type": "thought", "role": "planner",
               "text": f"⚠ SPEC.md write failed ({exc}) — subtasks still get "
                       "the spec in-context, but nothing is persisted to disk."}


def _prepare_tree(cwd: str, subs: list):
    """Record the pre-existing code so greenfield-only steps (scaffold, off-plan
    prune) never touch an EXISTING repo — on a real repo they'd delete the whole
    codebase (everything not in this task's small plan) — then scaffold when the
    tree really is empty."""
    preexisting = _snapshot_baseline(cwd)
    if not _is_greenfield(cwd):
        yield {"type": "thought", "role": "system",
               "text": f"Existing repo ({preexisting} source files) — editing in "
                       "place; skipping scaffold + off-plan prune (greenfield-only)."}
        return
    # SCAFFOLD — deterministically create every file at its canonical path (stub
    # + API-contract header) BEFORE parallelizing, then commit to base so
    # worktrees branch from a fixed tree. GREENFIELD ONLY (stubbing over an
    # existing repo is wrong). Gated (default on).
    if os.environ.get("AIFORGE_SCAFFOLD", "1") in ("0", "false"):
        return
    try:
        stubs = _scaffold_stubs(cwd, subs)
    except Exception as exc:  # noqa: BLE001
        log.debug("scaffold skipped: %s", exc)
        return
    if stubs:
        yield {"type": "tool", "role": "planner", "name": "scaffolded project",
               "args": {}, "result": {"files": stubs}}


def _announce_execution(subs: list):
    """OBSERVABILITY — surface the effective execution config so a regression is
    VISIBLE (e.g. a stray AIFORGE_SEQUENTIAL=1 forcing 1-at-a-time, or the
    reviewer model missing). Silent config drift is what made "why only 1?" hard."""
    sequential = os.environ.get("AIFORGE_SEQUENTIAL", "0") not in ("0", "false")
    mode = ("SEQUENTIAL (1 at a time)" if sequential
            else f"parallel, up to {_max_workers()} at once")
    try:
        reviewer = (review_gates.pick_reviewer_model()
                    or "same model (no 2nd model loaded)")
    except Exception:  # noqa: BLE001
        reviewer = "?"
    yield {"type": "thought", "role": "system",
           "text": f"Running {len(subs)} subtasks — each in its OWN fresh context "
                   f"+ git worktree · execution: {mode} · reviewer: {reviewer}."}


def _spec_runner(cwd: str, spec_md: str):
    """Spec-bound per-subtask runner: every fresh subtask context is handed the
    shared SPEC.md so it builds a coherent slice, without inheriting the other
    subtasks' conversation (that's what keeps each context small)."""
    base_run_one = _default_subtask_runner()

    def _run(subtask, worktree):
        # Re-read SPEC.md from disk so any mid-run steering appended to it is
        # seen by subtasks that start AFTER the steer (sequential on local).
        spec = spec_md
        try:
            p = os.path.join(cwd, "SPEC.md")
            if os.path.isfile(p):
                with open(p, encoding="utf-8", errors="replace") as fh:
                    spec = fh.read()
        except Exception:  # noqa: BLE001
            pass
        try:
            return base_run_one(subtask, worktree, spec_md=spec)
        except TypeError:
            # A custom runner that doesn't accept spec_md — call it plainly.
            return base_run_one(subtask, worktree)
    return _run


def _review_written_tests(cwd: str, spec_md: str, put) -> None:
    """The tests are now written; review them against the SPEC and fix
    provably-wrong ones (contradictions, scope creep, impossible values) BEFORE
    any impl is built to them, so the impl targets clean tests instead of the
    reconcile burning rounds on impossible-to-satisfy assertions. Fixes are
    committed to base so the impl worktrees branch from the cleaned tests."""
    try:
        changed, note = review_gates.review_tests(cwd, spec_md)
        if note:
            put({"type": "thought", "role": "reviewer", "text": f"🔍 {note}"})
        if changed:
            _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
            _git(["commit", "-m", "test-review fixes"], cwd)
    except Exception:  # noqa: BLE001
        pass


def _run_test_first(cwd, base, subs, run_one, on_status, cancelled, spec_md,
                    put) -> dict:
    """TEST-FIRST: build the tests first (they pin behaviour from the API
    contract), merge them into base, then build each impl in a worktree that HAS
    the tests + is fed its own test content — so the impl is functionally
    correct, not just linking."""
    test_subs = [s for s in subs if _is_test_subtask(s)]
    impl_subs = [s for s in subs if not _is_test_subtask(s)]
    put({"type": "thought", "role": "system",
         "text": f"Test-first: writing {len(test_subs)} test file(s), "
                 f"then {len(impl_subs)} module(s) built to pass them…"})
    agg_tests = run_parallel(cwd, base, None, test_subs, run_one,
                             validate_one=None, integration_test=None,
                             on_status=on_status, should_cancel=cancelled)
    if cancelled():
        return agg_tests
    _review_written_tests(cwd, spec_md, put)
    for s in impl_subs:
        s["_tests"] = _matching_tests_for(cwd, s.get("path") or "")
    agg_impl = run_parallel(cwd, base, None, impl_subs, run_one,
                            validate_one=default_validate_one,
                            integration_test=default_integration_test,
                            on_status=on_status, should_cancel=cancelled)
    return _merge_aggs(agg_tests, agg_impl)


def _make_runner(cwd, base, subs, run_one, on_status, cancelled, spec_md, q,
                 result: dict):
    def _runner():
        try:
            # SEQUENTIAL mode: single branch, each subtask sees the REAL prior
            # committed files (no isolated interface-guessing), commit-or-revert
            # per step. Right for tightly-coupled projects.
            if os.environ.get("AIFORGE_SEQUENTIAL", "0") not in ("0", "false"):
                result["agg"] = _run_sequential(
                    cwd, base, subs, run_one, on_status=on_status,
                    should_cancel=cancelled, emit=q.put)
                return
            test_first = (os.environ.get("AIFORGE_TEST_FIRST", "1")
                          not in ("0", "false"))
            has_both = (any(_is_test_subtask(s) for s in subs)
                        and any(not _is_test_subtask(s) for s in subs))
            if test_first and has_both:
                result["agg"] = _run_test_first(
                    cwd, base, subs, run_one, on_status, cancelled, spec_md,
                    q.put)
            else:
                result["agg"] = run_parallel(
                    cwd, base, None, subs, run_one,
                    validate_one=default_validate_one,
                    integration_test=default_integration_test,
                    on_status=on_status, should_cancel=cancelled)
        except Exception as exc:  # noqa: BLE001
            result["err"] = str(exc)
        finally:
            q.put(None)
    return _runner


def _prune_offplan(cwd: str, subs: list):
    """Strip off-plan phantom files (a worker/reconciler-invented package that
    duplicates declared modules → collection errors) BEFORE integration."""
    try:
        off = _prune_offplan_files(cwd, subs)
    except Exception as exc:  # noqa: BLE001
        log.debug("off-plan prune skipped: %s", exc)
        return
    if off:
        yield {"type": "thought", "role": "system",
               "text": f"Removed {len(off)} off-plan file(s) not in the plan "
                       f"(kept the tree matching SPEC): {', '.join(off[:6])}"}


def _clean_contract_sidecars(cwd: str) -> None:
    """Clean the merger's blackboard sidecars from the delivered workspace."""
    try:
        import shutil as _sh
        _sh.rmtree(os.path.join(cwd, _CONTRACT_DIR), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _detected_stacks(cwd: str) -> list:
    try:
        from aiforge_core.runtime.tools.project_runner import detect as _detect
        return (_detect(cwd) or {}).get("stacks") or []
    except Exception:  # noqa: BLE001
        return []


def _build_verdict(ok, cwd: str) -> str:
    """Honest verdict — ``ok`` is True (green) / False (some tests fail) / None
    (couldn't run tests here). A False is NOT necessarily a code defect: a local
    model also writes buggy tests, which the reviewer/audit flags + fixes; say so
    rather than a bare "failed"."""
    if ok is True:
        return "✅ **Built — all tests pass.**"
    if ok is False:
        return ("⚠️ **Built — some tests still fail.** This may not be a code "
                "defect: a local model sometimes writes incorrect tests, which "
                "the reviewer flags + fixes where it can. Check the remaining "
                "failing assertions against the intent before treating them as "
                "bugs — the implementation may be right.")
    # ok is None — the reconcile didn't produce a clear pass/fail. Don't lie "no
    # toolchain" when one IS installed and the build simply errored (a compile
    # error, a malformed pom): detect the stack + say the truth.
    stacks = _detected_stacks(cwd)
    if stacks:
        return ("⚠️ **Built — but the build/tests did NOT pass cleanly** ("
                + ", ".join(stacks) + "). The toolchain ran and reported "
                "errors (a compile error or a broken build file) — see the "
                "integration report below for the exact error.")
    return ("ℹ️ **Built.** Couldn't run the tests on this host (no matching "
            "toolchain) — the code is written; run the suite where the "
            "toolchain is available.")


def _finalize(cwd: str, subs: list, spec_md: str, agg: dict, start_sha: str,
              cancelled):
    """Verify against SPEC, reconcile the merged tree, and report."""
    # Final verification pass — a FRESH context reads SPEC.md + the produced tree
    # and confirms every requirement was addressed (the "close the loop against
    # the original requirement file" step). Best-effort; never blocks the result.
    yield {"type": "thought", "role": "verifier",
           "text": "Verifying the merged result against SPEC.md…"}
    try:
        verdict = _verify_against_spec(cwd, spec_md)
        if verdict:
            yield {"type": "thought", "role": "verifier", "text": verdict[:1500]}
    except Exception as exc:  # noqa: BLE001
        log.debug("spec verification skipped: %s", exc)

    yield from _prune_offplan(cwd, subs)
    # Compile + end-to-end test the merged result (any language). Subtasks are
    # built in ISOLATION, so the tree can fail to link on cross-file drift (a
    # test imports a name a module spelled differently). A bounded RECONCILIATION
    # pass over the whole merged tree fixes those mismatches until green.
    integ_md = ""
    res: dict = {}
    rep: dict = {}
    try:
        yield from _reconcile_integration(cwd, res, should_cancel=cancelled)
        rep = res.get("rep") or {}
        if rep.get("md"):
            integ_md = "\n\n---\n\n" + rep["md"]
    except Exception as exc:  # noqa: BLE001
        log.debug("integration report skipped: %s", exc)
    _clean_contract_sidecars(cwd)

    # Authoritative outcome from the reconcile's own test runner (matches
    # pytest); the report's ok can disagree — it uses a separate runner that may
    # miss deps.
    ok = res.get("ok") if "ok" in res else rep.get("ok")
    build_verdict = _build_verdict(ok, cwd)
    # Only attach the detailed integration report when it AGREES with the
    # authoritative verdict — otherwise it contradicts (e.g. "✅ all tests pass"
    # followed by "❌ tests failed" from a different runner that missed a dep).
    show_report = integ_md and not (ok is True and rep.get("ok") is not True)
    # SHOW CHANGES — the parallel agents committed to base in isolated worktrees
    # (no per-edit approval gate), so surface the full diff of what they built vs
    # the pre-run baseline, rendered like a PR.
    try:
        yield from _emit_changes(cwd, start_sha)
    except Exception as exc:  # noqa: BLE001
        log.debug("changes diff skipped: %s", exc)
    yield {"type": "message", "text":
           f"**Pipeline complete** — {agg.get('done', 0)}/{agg.get('total', 0)} "
           f"subtasks built + merged. {build_verdict}\n\nSPEC.md holds the "
           "requirements each subtask built against."
           + (integ_md if show_report else "")}


def _drain_run(q, session_id, subs: list, cwd: str, cancelled):
    """Stream the runner's events, folding mid-run steering into SPEC.md."""
    while True:
        item = q.get()
        if item is None:
            return
        yield item
        yield from _steering_drain(session_id, subs, cwd)
        if cancelled():
            # Stop pressed: drain no further. The runner sees should_cancel and
            # winds down (stops launching new subtasks); we just quit streaming.
            return


def stream_parallel_team(prompt: str, cwd: str, subtasks: list[dict] | None = None,
                         enhanced: bool = False, session_id: int | None = None):
    """Chat 'parallel team' mode: run the (pre-decomposed) subtasks CONCURRENTLY
    in isolated worktrees under ``cwd``, streaming live status. If ``subtasks``
    isn't supplied, decompose here. Yields SSE-ready dicts.

    ``session_id`` wires the Stop button through: the per-subtask dispatch stops
    launching new subtasks, the reconciliation loop halts, and the run's own
    build/test subprocesses are killed."""
    import queue as _queue

    cancelled = _cancel_checker_for(session_id)
    _arm_session(session_id)
    if cancelled():
        yield {"type": "message", "text": "Stopped before the run started."}
        return
    if enhanced:
        # Show the layer-1 spec (analyze → enhance) the planner split.
        yield {"type": "thought", "role": "enhancer", "text": prompt[:800]}

    state: dict = {}
    yield from _plan_subtasks(prompt, subtasks, state)
    subs = state.get("subs") or []
    if not subs:
        return
    yield from _write_spec(prompt, subs, cwd, state)
    spec_md = state["spec_md"]
    yield from _prepare_tree(cwd, subs)
    yield from _announce_execution(subs)

    base = _ensure_git_workspace(cwd)
    # Baseline commit — snapshot the CURRENT tree (incl. any leftover files from
    # a prior run in a reused workspace) BEFORE any subtask runs, so the final
    # "Changes" diff shows exactly what THESE agents built/changed vs the start,
    # never a previous ticket's edits.
    start_sha = _commit_turn_baseline(cwd) or (
        _git(["rev-parse", "HEAD"], cwd).stdout or "").strip()
    # B3 — surface a dirty-cwd warning before merging into it.
    warn = _dirty_warning(cwd)
    if warn:
        yield {"type": "thought", "role": "system", "text": "⚠ " + warn}

    q: "_queue.Queue" = _queue.Queue()
    result: dict = {}

    def on_status(slug, status, files=None):
        q.put({"type": "subtask_update", "slug": slug, "status": status})
        if files:   # show what the worker produced (expandable action)
            q.put({"type": "tool", "role": slug, "name": "wrote files",
                   "args": {"subtask": slug}, "result": {"files": files}})

    threading.Thread(
        target=_make_runner(cwd, base, subs, _spec_runner(cwd, spec_md),
                            on_status, cancelled, spec_md, q, result),
        name="parallel-chat", daemon=True).start()
    yield from _drain_run(q, session_id, subs, cwd, cancelled)

    agg = result.get("agg") or {}
    if cancelled():
        yield {"type": "message", "text":
               f"**Stopped** — {agg.get('done', 0)}/{len(subs)} subtasks finished "
               "before you hit Stop. Their work is committed in the workspace; "
               "verification + integration were skipped."}
        return
    if result.get("err"):
        yield {"type": "message", "text": f"Parallel run error: {result['err']}"}
        return
    yield from _finalize(cwd, subs, spec_md, agg, start_sha, cancelled)


_STATUS_WORD = {"A": "added", "M": "modified", "D": "deleted",
                "R": "renamed"}


def _numstat_counts(numstat: str) -> dict:
    """``{path: (adds, dels)}`` from ``git diff --numstat``."""
    counts: dict = {}
    for ln in numstat.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            counts[parts[2]] = (parts[0], parts[1])
    return counts


def _changed_file(name_status_line: str, counts: dict, ref: list, cwd: str,
                  cap: int) -> dict | None:
    """One ``--name-status`` line as a change entry, or None to skip it."""
    parts = name_status_line.split("\t")
    if len(parts) < 2:
        return None
    status, path = parts[0][:1], parts[-1]
    if any(h in path for h in _CHANGES_HIDE):
        return None
    adds, dels = counts.get(path, ("0", "0"))
    fdiff = _git(["diff", *ref, "--", path], cwd).stdout or ""
    truncated = len(fdiff) > cap
    return {"path": path, "status": _STATUS_WORD.get(status, "changed"),
            "additions": _to_int(adds), "deletions": _to_int(dels),
            "diff": fdiff[:cap] + ("\n… (truncated)" if truncated else "")}


def _emit_changes(cwd: str, start_sha: str, include_worktree: bool = False):
    """Yield a STRUCTURED ``changes`` event — one entry per changed file with its
    status, +/- line counts, and unified diff — so the UI renders a clean PR-style
    view (file list + expandable colored diffs), not a raw blob. Used after BOTH
    the parallel pipeline (committed to base → diff ``start..HEAD``) and a
    single-agent simple run (uncommitted working tree → ``include_worktree``:
    intent-add untracked, diff ``start``)."""
    if not start_sha:
        return
    try:
        cap = int(os.environ.get("AIFORGE_CHANGES_FILE_DIFF_MAX", "8000"))
    except ValueError:
        cap = 8000
    if include_worktree:
        # make untracked files appear in the diff without staging their content
        _git(["add", "-N", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
        ref = [start_sha]
    else:
        ref = [f"{start_sha}..HEAD"]
    counts = _numstat_counts(_git(["diff", "--numstat", *ref], cwd).stdout or "")
    name_status = _git(["diff", "--name-status", *ref], cwd).stdout or ""
    files = [f for f in (_changed_file(ln, counts, ref, cwd, cap)
                         for ln in name_status.splitlines()) if f]
    if not files:
        return
    total_add = sum(f["additions"] for f in files)
    total_del = sum(f["deletions"] for f in files)
    yield {"type": "changes", "files": files,
           "summary": {"files": len(files), "additions": total_add,
                       "deletions": total_del}}


def _to_int(s: str) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


# Mid-run user instructions per cwd — MANDATORY constraints re-asserted into the
# reconcile prompt so a user's "must" survives every rebuild/fix pass.
_USER_MANDATES: dict[str, list[str]] = {}


# Generated / build / cache artifacts — never "real" source, skip in the Changes
# list across languages. Substring-matched against each changed path.
_CHANGES_HIDE = (
    # aiforge internals
    "SPEC.md", ".aiforge-venv", ".aiforge-contracts", ".aiforge-baseline",
    ".aiforge-worktrees",
    # python
    "__pycache__", ".pyc", ".pyo", ".egg-info", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".tox/", ".coverage",
    # js / ts
    "node_modules/", "/dist/", "/.next/", "/.nuxt/", ".min.js", ".map",
    # jvm
    ".class", "/target/", "/.gradle/", "/out/",
    # go / rust / c / native
    "/vendor/", ".rlib", "/Cargo.lock", ".o", ".obj", ".a", ".so", ".dll",
    ".dylib", ".exe",
    # generic build/cache/vcs junk
    "/build/", "/bin/", "/.cache/", ".DS_Store", ".log", ".lock", ".tmp",
    ".git/",
)


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

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._contracts import _CONTRACT_DIR, _is_test_subtask, _matching_tests_for, _merge_aggs
from ._orchestrate import _run_sequential, run_parallel
from ._planning import _commit_turn_baseline, _ensure_git_workspace
from ._reconcile import (_enforce_disjoint_files, _ensure_impl_modules,
                         _prune_offplan_files, _reconcile_integration, _render_spec_md,
                         _route_steering, _scaffold_stubs, _snapshot_baseline, _verify_against_spec)
from ._runners import _default_subtask_runner
from ._worktree import (_dirty_warning, _git, _max_workers, _slugify, default_integration_test,
                        default_validate_one, log)
def _decompose(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._decompose(*a, **k)
def _is_greenfield(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._is_greenfield(*a, **k)
