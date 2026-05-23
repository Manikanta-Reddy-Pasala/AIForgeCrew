"""Claude-side targeted fix when the local pipeline lands but the
Validator (or in-loop Feedback) rejects.

Runs AFTER the SequentialAgent exits. Reads:
  * the operator-enhanced ticket body
  * the local Doer's ``file_diffs``
  * the in-loop ``feedback_verdict`` + ``refiner_verdict``
  * the Validator's ``validator_verdict`` (rationale + scope_ok + ...)
  * the same memory bundle the runner fed the pipeline

Builds a single Claude turn that says "here's what local did, here's
what Validator flagged, write the targeted fix using these tools" and
runs a minimal ADK SequentialAgent[Doer, Validator] under
``set_force_provider("claude_local")``. Doer writes the corrective
edits on the same worktree → ``commit_push_open_pr`` later folds them
into the same branch → the existing PR picks up the new commit.

After Claude lands a fix, a ``Decision_v2`` is persisted via the
Learner's path so the next ticket retrieving similar memory hits sees
the recipe ("when X happens, do Y") not just the failure.

Pattern: 2026-05-23 design call — Claude rewrites the ticket, local
Doer attempts, Claude validates, Claude fixes on reject, learner
keeps the recipe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

log = logging.getLogger("aiforge.claude_fix")


def _validator_reject(validator_verdict: Any) -> tuple[bool, str]:
    """Return ``(should_fix, rationale)`` from the validator output."""
    if not validator_verdict:
        return False, ""
    if isinstance(validator_verdict, str):
        try:
            validator_verdict = json.loads(validator_verdict)
        except Exception:
            return False, ""
    if not isinstance(validator_verdict, dict):
        return False, ""
    verdict = (validator_verdict.get("verdict") or "").lower()
    rationale = (validator_verdict.get("rationale") or "")[:280]
    if verdict in {"request_changes", "abstain"}:
        return True, rationale
    return False, rationale


def _build_fix_prompt(
    *,
    ticket,
    enhanced_body: str,
    file_diffs: Any,
    feedback_verdict: str,
    validator_rationale: str,
    memory_md: str,
) -> str:
    diffs_repr = ""
    if isinstance(file_diffs, list):
        for entry in file_diffs[:20]:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path") or entry.get("file") or "?"
            diff = (entry.get("diff") or entry.get("patch") or "")[:1500]
            diffs_repr += f"\n--- {path} ---\n{diff}\n"
    elif isinstance(file_diffs, str):
        diffs_repr = file_diffs[:6000]

    return (
        "# Targeted fix pass (Claude)\n\n"
        "The local Doer landed a diff but the Validator rejected it. "
        "Your job: write the corrective edit using the editor / bash / "
        "run_tests tools. Stay inside the ticket's scope_allowlist_globs. "
        "Do NOT redo work the local Doer already got right — only fix "
        "what the Validator called out.\n\n"
        f"## Ticket {ticket.identifier}\n"
        f"### Title\n{ticket.title}\n\n"
        f"### Enhanced body\n{enhanced_body or ticket.body or '(none)'}\n\n"
        f"### Local Doer's in-loop verdict\n{feedback_verdict}\n\n"
        f"### Validator rationale\n{validator_rationale}\n\n"
        f"### Local Doer's diffs (truncated)\n{diffs_repr or '(none)'}\n\n"
        f"### Memory hits\n{memory_md or '(none)'}\n\n"
        "## How to finish\n"
        "1. Identify the SINGLE root cause Validator named.\n"
        "2. Apply the smallest patch that addresses it.\n"
        "3. Re-run `run_tests` and `typecheck` to confirm.\n"
        "4. Call `finish` with a one-line summary of what changed.\n"
    )


def _persist_fix_recipe(
    ticket,
    *,
    fix_summary: str,
    validator_rationale: str,
    feedback_verdict: str,
) -> None:
    """Write a Decision_v2 capturing the failure → fix pair so the
    next ticket retrieving similar memory hits sees the recipe.

    Uses the existing :func:`learner_persist.persist_facts` path via
    its ``DECISION:`` prefix convention. Soft-fail."""
    try:
        from aiforge_core.runtime.learner_persist import persist_facts
    except Exception as exc:  # noqa: BLE001
        log.debug("learner_persist import failed: %s", exc)
        return
    text = (
        f"DECISION: When local Doer ships a diff that the Validator "
        f"rejects with '{validator_rationale[:120]}', apply this fix "
        f"recipe: {fix_summary[:300]}. (in-loop verdict was "
        f"{feedback_verdict or 'unknown'}.)"
    )
    try:
        persist_facts(
            facts=[{"text": text, "tags": ["claude_fix_recipe"]}],
            repo=ticket.project or "",
            ticket_identifier=ticket.identifier,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("recipe persist failed: %s", exc)


def attempt_fix(
    *,
    ticket,
    pipeline_state: dict,
    memory_md: str,
    runner_module,
    skip_researcher: bool,
) -> dict:
    """Re-run a minimal Sequential[Doer, Validator] under claude_local
    with a targeted fix prompt. Returns ``{ok, attempted, new_verdict,
    summary}``.

    ``runner_module`` = the live ``aiforge_core.runtime.adk_runner``
    module — we re-use its ``_run_pipeline`` helper rather than
    duplicating the ADK wiring. Same SequentialAgent shape, just
    fed a different seed prompt.
    """
    if os.environ.get("AIFORGE_CLAUDE_FIX", "1") in {"0", "false", ""}:
        return {"ok": False, "attempted": False, "reason": "disabled"}

    state = pipeline_state or {}
    validator_verdict = state.get("validator_verdict")
    should_fix, rationale = _validator_reject(validator_verdict)
    if not should_fix:
        return {"ok": False, "attempted": False,
                "reason": "validator_did_not_reject"}

    enhanced_body = state.get("enhanced_body") or ""
    file_diffs = state.get("file_diffs")
    feedback_verdict = state.get("feedback_verdict") or ""

    prompt = _build_fix_prompt(
        ticket=ticket,
        enhanced_body=enhanced_body,
        file_diffs=file_diffs,
        feedback_verdict=feedback_verdict,
        validator_rationale=rationale,
        memory_md=memory_md,
    )

    # Pin the run to claude_local for the entire fix attempt.
    set_force_provider = getattr(runner_module, "set_force_provider", None)
    if set_force_provider is not None:
        set_force_provider("claude_local")
    try:
        new_state = asyncio.run(runner_module._run_pipeline(
            prompt, skip_researcher=skip_researcher, ticket=ticket,
        ))
    except Exception as exc:  # noqa: BLE001
        log.warning("claude_fix pipeline failed: %s", exc)
        return {"ok": False, "attempted": True,
                "reason": f"pipeline_exc: {exc}"}
    finally:
        if set_force_provider is not None:
            set_force_provider(None)

    new_verdict = (new_state or {}).get("validator_verdict") or {}
    if isinstance(new_verdict, str):
        try:
            new_verdict = json.loads(new_verdict)
        except Exception:
            new_verdict = {"raw": new_verdict[:200]}
    new_status = (new_verdict or {}).get("verdict", "unknown")

    # Summarise what changed for the Decision recipe.
    new_diffs = (new_state or {}).get("file_diffs")
    fix_summary = ""
    if isinstance(new_diffs, list) and new_diffs:
        paths = [d.get("path") or d.get("file") or "?"
                 for d in new_diffs if isinstance(d, dict)]
        fix_summary = f"touched {len(paths)} file(s): {', '.join(paths[:5])}"

    if new_status == "approve":
        _persist_fix_recipe(
            ticket,
            fix_summary=fix_summary or "see PR diff",
            validator_rationale=rationale,
            feedback_verdict=feedback_verdict,
        )

    return {
        "ok": new_status == "approve",
        "attempted": True,
        "new_verdict": new_status,
        "summary": fix_summary,
    }


__all__ = ["attempt_fix"]
