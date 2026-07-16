"""The bounded reconcile/integration loop, SPEC render, and spec verification.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os

from ._drift import _prune_dead_python_imports
from ._rewrite import _rewrite_fix
from ._sources import _change_in_error, _gather_sources
from ._testrun import (
    _broken_project_config,
    _directed_hints,
    _escalation_model,
    _fail_count,
    _is_hard_residual,
    _project_test_output,
    _reconcile_rounds,
)


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
        result["ok"] = ok            # authoritative (matches the test runner)
        return

    # PRE-EXISTING-FAILURE GATE: the harness ERRORED before any test ran
    # (0 parsed failures — a collection/import error), the config in THIS tree
    # parses fine, and NONE of this turn's changed files appear in the error.
    # That failure is pre-existing / environmental (e.g. an unrelated module the
    # repo can't import), NOT caused by the change — so the 12-round repair loop
    # would churn qwen against something it can't fix and that isn't the change's
    # fault. Stop: report ok=None (not a regression, not verified). Opt out with
    # AIFORGE_RECONCILE_SKIP_PREEXISTING=0.
    if os.environ.get("AIFORGE_RECONCILE_SKIP_PREEXISTING", "1") not in ("0", "false") \
            and _fail_count(output) in (0, 999) and not _broken_project_config(cwd) \
            and not _change_in_error(cwd, output):
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
    _cfg_err = _broken_project_config(cwd)
    if _cfg_err:
        yield {"type": "thought", "role": "reconciler",
               "text": f"⚠ project config invalid — {_cfg_err}. Fixing it "
                       "first; every test/build run is blocked by it."}
        output = (f"CONFIG ERROR — fix this FIRST, nothing can run until it "
                  f"parses: {_cfg_err}\n\n{output}")

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
        _esc_model = _escalation_model()
        # TRIAGE: a structurally-hard residual (cross-file import/signature/
        # attribute mismatch) that the coder+repo-map didn't crack in ONE round
        # escalates to the reasoning model early — don't burn a 2nd stall round on
        # it. A plain logic/value fail keeps the coder for 2 rounds first.
        _hard = _is_hard_residual(output)
        _use_esc = _esc_model and stalls >= (1 if _hard else 2)
        # TEST-AUDIT: after impl fixes stall (the impl was rewritten repeatedly and
        # the SAME tests still fail), a failing test may itself be WRONG — a local
        # model writes buggy tests too. Once stuck, let the fixer correct a test
        # that CONTRADICTS the goal (guarded: regression guard rolls back if net
        # fails rise; `# test-audit:` marker makes edits visible). Off with
        # AIFORGE_RECONCILE_TEST_AUDIT=0.
        _audit = (os.environ.get("AIFORGE_RECONCILE_TEST_AUDIT", "1")
                  not in ("0", "false") and stalls >= 2)
        # "0 failing" with a red run = the run ERRORED before tests executed
        # (config/collection) — say so instead of the contradictory count.
        _fail_desc = (f"{prev_fails} failing" if prev_fails
                      else "run ERRORED before tests executed — config/collection")
        yield {"type": "thought", "role": "reconciler",
               "text": f"Integration failed ({_fail_desc}) — pass "
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
