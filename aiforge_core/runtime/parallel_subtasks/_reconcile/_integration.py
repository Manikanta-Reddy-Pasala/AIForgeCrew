"""The bounded reconcile/integration loop, SPEC render, and spec verification.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os

from ._drift import _prune_dead_python_imports
from ._rewrite import _rewrite_fix
from ._sources import _change_in_error, _gather_sources, _is_greenfield
from ._testrun import (
    _broken_project_config,
    _directed_hints,
    _escalation_model,
    _fail_count,
    _is_hard_residual,
    _project_test_output,
    _reconcile_rounds,
)


def _is_preexisting_failure(cwd: str, output: str) -> bool:
    """PRE-EXISTING-FAILURE GATE: the harness ERRORED before any test ran
    (0 parsed failures — a collection/import error), the config in THIS tree
    parses fine, and NONE of this turn's changed files appear in the error.

    That failure is pre-existing / environmental (e.g. an unrelated module the
    repo can't import), NOT caused by the change — so the 12-round repair loop
    would churn qwen against something it can't fix and that isn't the change's
    fault. Opt out with AIFORGE_RECONCILE_SKIP_PREEXISTING=0.
    """
    return (os.environ.get("AIFORGE_RECONCILE_SKIP_PREEXISTING", "1")
            not in ("0", "false")
            and _fail_count(output) in (0, 999)
            and not _broken_project_config(cwd)
            and not _is_greenfield(cwd)
            and not _change_in_error(cwd, output))


def _round_strategy(output: str, stalls: int) -> tuple[str | None, bool]:
    """``(escalation_model_or_None, audit_tests)`` for this repair round.

    ESCALATION: after the primary model stalls (no improvement for a couple of
    rounds), hand the residual failures it can't crack to a stronger/reasoning
    model (AIFORGE_ESCALATION_MODEL). General — only the STUCK residual
    escalates, not every round; no per-problem code. TRIAGE: a structurally-hard
    residual (cross-file import/signature/attribute mismatch) that the
    coder+repo-map didn't crack in ONE round escalates early — don't burn a 2nd
    stall round on it. A plain logic/value fail keeps the coder for 2 rounds.

    TEST-AUDIT: after impl fixes stall (the impl was rewritten repeatedly and
    the SAME tests still fail), a failing test may itself be WRONG — a local
    model writes buggy tests too. Once stuck, let the fixer correct a test that
    CONTRADICTS the goal (guarded: the regression guard rolls back if net fails
    rise; a `# test-audit:` marker makes edits visible). Off with
    AIFORGE_RECONCILE_TEST_AUDIT=0.
    """
    esc_model = _escalation_model()
    needed = 1 if _is_hard_residual(output) else 2
    use_esc = bool(esc_model) and stalls >= needed
    audit = (os.environ.get("AIFORGE_RECONCILE_TEST_AUDIT", "1")
             not in ("0", "false") and stalls >= 2)
    return (esc_model if use_esc else None), audit


def _restore_snapshot(cwd: str, snapshot: dict) -> None:
    """Roll the workspace back: restore every snapshotted file and drop any the
    bad round created."""
    snap_keys = set(snapshot)
    for rel, _ in _gather_sources(cwd):
        if rel not in snap_keys:
            try:
                os.remove(os.path.join(cwd, rel))
            except Exception:  # noqa: BLE001
                pass
    for rel, content in snapshot.items():
        try:
            with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
                fh.write(content)
        except Exception:  # noqa: BLE001
            pass


def _round_plan_text(rounds: int, max_rounds: int, prev_fails: int,
                     esc_model: str | None, audit: bool) -> str:
    # "0 failing" with a red run = the run ERRORED before tests executed
    # (config/collection) — say so instead of the contradictory count.
    fail_desc = (f"{prev_fails} failing" if prev_fails
                 else "run ERRORED before tests executed — config/collection")
    if esc_model:
        what = f"escalating the residual to {esc_model}…"
    elif audit:
        what = "auditing whether a stuck test is itself wrong…"
    else:
        what = "patching the offending files…"
    return (f"Integration failed ({fail_desc}) — pass "
            f"{rounds}/{max_rounds}: {what}")


def _prune_quietly(cwd: str) -> list[str]:
    """Deterministic pre-fix: prune dead package re-exports (the #1 cross-file
    break the LLM won't fix) before spending an LLM round on it."""
    try:
        return _prune_dead_python_imports(cwd)
    except Exception:  # noqa: BLE001
        return []


def _repair_round(cwd: str, output: str, rounds: int, max_rounds: int,
                  prev_fails: int, stalls: int, state: dict):
    """ONE repair pass. Yields SSE events; leaves ``ok``/``output``/
    ``prev_fails``/``stalls`` in ``state``."""
    _prune_quietly(cwd)                    # deterministic, before the LLM round
    esc_model, audit = _round_strategy(output, stalls)
    yield {"type": "thought", "role": "reconciler",
           "text": _round_plan_text(rounds, max_rounds, prev_fails,
                                    esc_model, audit)}
    # Snapshot BEFORE the round so a round that makes things WORSE (a local
    # model's bad patch) can be rolled back — reconcile is then MONOTONIC: it
    # never regresses, only accepts rounds that reduce the failure count.
    snapshot = dict(_gather_sources(cwd))
    try:
        written = _rewrite_fix(cwd, output, _directed_hints(output),
                               model=esc_model, audit_tests=audit)
    except Exception as exc:  # noqa: BLE001 — a transient LLM error must not stop us
        yield {"type": "thought", "role": "reconciler",
               "text": f"reconcile pass hit a transient error, retrying: {str(exc)[:80]}"}
        written = []
    ok, output = _project_test_output(cwd)
    new_fails = _fail_count(output)
    if new_fails > prev_fails:
        # STRICT regression only → roll back. A LATERAL move (== fails) is KEPT
        # — it lets the model refactor toward the seam without penalty.
        _restore_snapshot(cwd, snapshot)
        ok, output = _project_test_output(cwd)
        stalls += 1
        yield {"type": "thought", "role": "reconciler",
               "text": f"pass {rounds} REGRESSED ({prev_fails}→? ) — rolled back to "
                       f"{prev_fails} failing. Trying a different angle…"}
    else:
        # improvement OR lateral (no regression) → KEEP.
        if new_fails < prev_fails:
            prev_fails = new_fails
            stalls = 0
        else:
            stalls += 1                    # lateral move — bounded
        label = ("tests can't run (collection/build error)"
                 if new_fails >= 999 else f"{new_fails} failing")
        yield {"type": "tool", "role": "reconciler", "name": "patched files",
               "args": {"pass": rounds, "status": label},
               "result": {"ok": new_fails < 999, "files": written,
                          "output": (output or "")[-1500:] if new_fails >= 999 else None}}
    state.update(ok=ok, output=output, prev_fails=prev_fails, stalls=stalls)


def _repair_loop(cwd: str, output: str, should_cancel, state: dict):
    """Repair rounds until green, out of rounds, or four no-progress passes."""
    max_rounds = _reconcile_rounds()
    state.update(ok=False, output=output, prev_fails=_fail_count(output),
                 stalls=0, rounds=0)
    while not state["ok"] and state["rounds"] < max_rounds:
        if should_cancel is not None and should_cancel():
            return
        state["rounds"] += 1
        yield from _repair_round(cwd, state["output"], state["rounds"],
                                 max_rounds, state["prev_fails"],
                                 state["stalls"], state)
        if state["stalls"] >= 4:
            return                         # 4 no-progress rounds → give up


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
    pruned = _prune_quietly(cwd)
    if pruned:
        yield {"type": "tool", "role": "reconciler", "name": "pruned dead re-exports",
               "args": {}, "result": {"files": pruned}}

    ok, output = _project_test_output(cwd)
    if ok or os.environ.get("AIFORGE_RECONCILE_INTEGRATION", "1") in ("0", "false"):
        result["rep"] = build_and_test_report(cwd)
        result["ok"] = ok            # authoritative (matches the test runner)
        return

    if _is_preexisting_failure(cwd, output):
        yield {"type": "thought", "role": "reconciler",
               "text": "⚠ tests don't collect on this repo independent of your "
                       "change (pre-existing import/config error, not referenced "
                       "by your edit) — skipping the repair loop; your change is "
                       "not the cause."}
        result["rep"] = build_and_test_report(cwd)
        result["ok"] = None          # pre-existing failure — not a regression
        return

    # CONFIG-VALIDITY GATE (live-e2e finding): ONE unterminated string in a
    # merged pyproject.toml made every pytest/pip run die at CONFIG PARSE —
    # exit != 0 with ZERO parsed failures — so reconcile burned all its
    # passes reporting "failed (0 failing)" while patching the wrong files.
    # Detect a broken config deterministically and point the fixer AT it.
    cfg_err = _broken_project_config(cwd)
    if cfg_err:
        yield {"type": "thought", "role": "reconciler",
               "text": f"⚠ project config invalid — {cfg_err}. Fixing it "
                       "first; every test/build run is blocked by it."}
        output = (f"CONFIG ERROR — fix this FIRST, nothing can run until it "
                  f"parses: {cfg_err}\n\n{output}")

    state: dict = {}
    yield from _repair_loop(cwd, output, should_cancel, state)
    rounds = state.get("rounds", 0)
    ok = state.get("ok", False)

    result["rep"] = build_and_test_report(cwd)
    result["ok"] = ok                # authoritative final state (the test runner)
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
