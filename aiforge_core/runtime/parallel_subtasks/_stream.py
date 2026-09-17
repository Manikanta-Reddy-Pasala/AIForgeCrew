"""stream_parallel_team chat driver, change emission, test-coverage helpers.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os
import threading

from aiforge_core.runtime import review_gates
from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS

from ._stream_changes import (  # noqa: F401  # re-exported
    _CHANGES_HIDE,
    _CODE_EXTS,
    _SPEC_MD,
    _STATUS_WORD,
    _changed_file,
    _emit_changes,
    _ensure_test_coverage,
    _numstat_counts,
    _test_path_for,
    _to_int,
)
from ._stream_prepare import (  # noqa: F401  # re-exported
    _announce_execution,
    _plan_subtasks,
    _prepare_tree,
    _write_spec,
)
from ._stream_steer import (  # noqa: F401  # re-exported
    _USER_MANDATES,
    _append_spec_mandate,
    _apply_steer,
    _arm_session,
    _cancel_checker_for,
    _pin_to_subtask,
    _steer_headings,
    _steering_drain,
)


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
            p = os.path.join(cwd, _SPEC_MD)
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


def _outcome_verdict(agg: dict, ok, cwd: str) -> str:
    """The verdict line, honest about subtasks that never landed. A run whose
    every subtask failed (the model endpoint dropped mid-run) reported
    "0/5 subtasks built + merged. ✅ Built — all tests pass": the test runner
    found nothing to fail on an empty tree, and nothing said so."""
    done, total = int(agg.get("done") or 0), int(agg.get("total") or 0)
    if total and done == 0:
        return ("❌ **Nothing was built** — every subtask failed, so there is no "
                "code to test. Check that the model endpoint stayed reachable, "
                "then run the request again.")
    verdict = _build_verdict(ok, cwd)
    if total and done < total:
        return (f"⚠️ **{total - done} of {total} subtasks failed** and are not "
                f"in the tree. " + verdict.replace("✅ ", ""))
    return verdict


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
    build_verdict = _outcome_verdict(agg, ok, cwd)
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

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._contracts import _CONTRACT_DIR, _is_test_subtask, _matching_tests_for, _merge_aggs
from ._orchestrate import _run_sequential, run_parallel
from ._planning import _commit_turn_baseline, _ensure_git_workspace
from ._reconcile import (
    _enforce_disjoint_files,
    _ensure_impl_modules,
    _prune_offplan_files,
    _reconcile_integration,
    _render_spec_md,
    _route_steering,
    _scaffold_stubs,
    _snapshot_baseline,
    _verify_against_spec,
)
from ._runners import _default_subtask_runner
from ._worktree import (
    _dirty_warning,
    _git,
    _max_workers,
    _slugify,
    default_integration_test,
    default_validate_one,
    log,
)


def _decompose(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._decompose(*a, **k)
def _is_greenfield(*a, **k):  # live forwarder — honours monkeypatch on the package
    from aiforge_core.runtime import parallel_subtasks as _pkg
    return _pkg._is_greenfield(*a, **k)
