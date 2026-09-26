"""stream_parallel_team chat driver: the verdict, finalising and draining a run.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical).
Steering lives in ``_stream_steer``, run preparation in ``_stream_prepare``, and
change reporting and test coverage in ``_stream_changes``."""
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
from ._stream_guard import bind_plan_to_spec, dirty_overlap_stop, run_note, seal_run
from ._test_evidence import executed_tests, failed_tests, go_packages_passed


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
            from aiforge_core.runtime.team_workspace import spec_path
            p = spec_path(cwd)
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
            # Code first, tests after. A test written in this run must not be
            # the spec the implementation is rewritten to match. Opt back in
            # with AIFORGE_TEST_FIRST=1.
            test_first = (os.environ.get("AIFORGE_TEST_FIRST", "0")
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


def _green_verdict(cwd: str, output) -> str:
    """``ok`` is True: success ONLY with the runner's own count of executed
    tests above zero. A clean exit with nothing collected, no runner, or no
    readable summary is "built, NOT tested" — never ✅."""
    n = executed_tests(output)
    if n:
        return f"✅ **Built — all {n} test{'s' if n != 1 else ''} pass.**"
    pkgs = go_packages_passed(output) if n is None else 0
    if pkgs:
        return (f"✅ **Built — `go test` passed in {pkgs} package"
                f"{'s' if pkgs != 1 else ''}** (it printed no per-test count).")
    if n == 0:
        return (f"⚠️ **Built — NO tests were run.** The test runner found no "
                f"tests in `{cwd}`, so nothing checked this change.")
    return (f"⚠️ **Built — NO tests were run** that the check could count: it "
            f"finished cleanly in `{cwd}` but printed no test summary, so "
            "nothing confirms the change works.")


def _tests_read_only(cwd: str) -> bool:
    from ._protected import TESTS, rules_for
    return TESTS in (rules_for(cwd).get("patterns") or [])


def _build_verdict(ok, cwd: str, output: str | None = None) -> str:
    """Honest verdict — ``ok`` is True (green) / False (some tests fail) / None
    (couldn't run tests here); ``output`` is the final test run's output (the
    evidence for a green verdict). A False is NOT necessarily a code defect: a
    local model also writes buggy tests, which the reviewer/audit flags +
    fixes; say so rather than a bare "failed"."""
    if ok is True:
        return _green_verdict(cwd, output)
    if ok is False:
        n = failed_tests(output)
        head = (f"{n} test{'s' if n != 1 else ''} failed" if n
                else "some tests still fail")
        if _tests_read_only(cwd):
            return (f"⚠️ **Built — {head}.** Your tests were left exactly as "
                    "they were, as you asked. Check whether the failing "
                    "assertions contradict each other or the request — no "
                    "implementation can pass two tests that want different "
                    "results for the same input.")
        return (f"⚠️ **Built — {head}.** This may not be a code "
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


def _outcome_verdict(agg: dict, ok, cwd: str, output: str | None = None) -> str:
    """The verdict line, honest about subtasks that never landed. A run whose
    every subtask failed (the model endpoint dropped mid-run) reported
    "0/5 subtasks built + merged. ✅ Built — all tests pass": the test runner
    found nothing to fail on an empty tree, and nothing said so."""
    done, total = int(agg.get("done") or 0), int(agg.get("total") or 0)
    if total and done == 0:
        return ("❌ **Nothing was built** — every subtask failed, so there is no "
                "code to test. Check that the model endpoint stayed reachable, "
                "then run the request again.")
    verdict = _build_verdict(ok, cwd, output)
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
    verdict = ""
    try:
        verdict = _verify_against_spec(cwd, spec_md) or ""
        if verdict:
            yield {"type": "thought", "role": "verifier", "text": verdict[:1500]}
    except Exception as exc:  # noqa: BLE001
        log.debug("spec verification skipped: %s", exc)
        verdict = ""
    spec_gaps = ""
    low = verdict.lower()
    if verdict and "everything is covered" not in low and (
            "missing" in low or "incomplete" in low):
        spec_gaps = verdict

    yield from _prune_offplan(cwd, subs)
    # Compile + end-to-end test the merged result (any language). Subtasks are
    # built in ISOLATION, so the tree can fail to link on cross-file drift (a
    # test imports a name a module spelled differently). A bounded RECONCILIATION
    # pass over the whole merged tree fixes those mismatches until green.
    integ_md = ""
    res: dict = {}
    rep: dict = {}
    try:
        yield from _reconcile_integration(
            cwd, res, should_cancel=cancelled, spec_gaps=spec_gaps)
        rep = res.get("rep") or {}
        if rep.get("md"):
            integ_md = "\n\n---\n\n" + rep["md"]
    except Exception as exc:  # noqa: BLE001
        log.debug("integration report skipped: %s", exc)
    _clean_contract_sidecars(cwd)
    if (yield from seal_run(cwd, start_sha)) and "ok" in res:
        # A read-only file was put back: the last test run saw the changed
        # copy, so its count would describe tests the user never had.
        from ._reconcile._testrun import _project_test_output
        res["ok"], res["output"] = _project_test_output(cwd)

    # Authoritative outcome from the reconcile's own test runner (matches
    # pytest); the report's ok can disagree — it uses a separate runner that may
    # miss deps.
    ok = res.get("ok") if "ok" in res else rep.get("ok")
    build_verdict = _outcome_verdict(agg, ok, cwd, res.get("output"))
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
           "requirements each subtask built against." + run_note(cwd)
           + (integ_md if show_report else "")}


def _anchor_paths(subs: list, cwd: str):
    """Subtask paths relative to ``cwd``: never a path-shaped copy of an
    absolute folder (see team_target.anchor_subtask_paths)."""
    from aiforge_core.runtime.team_target import anchor_subtask_paths
    try:
        subs, dropped = anchor_subtask_paths(subs, cwd)
    except Exception as exc:  # noqa: BLE001
        log.debug("subtask path anchoring skipped: %s", exc)
        return subs
    if dropped:
        yield {"type": "thought", "role": "planner",
               "text": f"Dropped {len(dropped)} subtask(s) whose file is outside "
                       f"the workspace `{cwd}`: {', '.join(dropped[:4])}"}
        if not subs:
            yield {"type": "message", "text":
                   "Every planned file lies outside the workspace "
                   f"`{cwd}`, so nothing was built. Name the project folder "
                   "in your message and ask again."}
    return subs


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

    state: dict = {"cwd": cwd}
    yield from _plan_subtasks(prompt, subtasks, state)
    subs = state.get("subs") or []
    subs = yield from _anchor_paths(subs, cwd)
    if not subs:
        return
    yield from _write_spec(prompt, subs, cwd, state)
    spec_md = state["spec_md"]
    yield from bind_plan_to_spec(subs, spec_md, cwd, state)
    subs = state.get("subs") or []
    if not subs:
        return
    if (yield from dirty_overlap_stop(cwd, subs)):
        return
    yield from _prepare_tree(cwd, subs)
    yield from _announce_execution(subs)

    base = _ensure_git_workspace(cwd)
    # Baseline commit — snapshot the CURRENT tree (incl. any leftover files from
    # a prior run in a reused workspace) BEFORE any subtask runs, so the final
    # "Changes" diff shows exactly what THESE agents built/changed vs the start,
    # never a previous ticket's edits.
    start_sha = _commit_turn_baseline(cwd) or (
        _git(["rev-parse", "HEAD"], cwd).stdout or "").strip()
    # B3 — surface a dirty-cwd warning before merging into it (a user-repo run
    # works in its own clean worktree, so there is nothing to warn about).
    from aiforge_core.runtime.team_workspace import for_cwd
    warn = None if for_cwd(cwd) else _dirty_warning(cwd)
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
from ._reconcile import (  # noqa: F401  # read via _pkg() or by tests
    _prune_offplan_files,
    _reconcile_integration,
    _render_spec_md,
    _route_steering,
    _scaffold_stubs,
    _snapshot_baseline,
    _verify_against_spec,
)
from ._runners import _default_subtask_runner
from ._worktree import (  # noqa: F401  # read via _pkg() or by tests
    _dirty_warning,
    _git,
    _max_workers,
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
