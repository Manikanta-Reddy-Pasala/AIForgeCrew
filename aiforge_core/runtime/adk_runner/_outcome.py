"""After a run: the first verdict, validator output, PR demotions, auto-merge,
live verification, CI grading and the status metadata."""
from __future__ import annotations

import os

from ._base import _VERDICT_TO_STATUS, log


def _pkg():
    """The parent module, looked up on each call so a name patched there is the
    one used here."""
    import aiforge_core.runtime.adk_runner._orchestrate as package
    return package


    # External-ref ingestion backend removed — nothing to persist to.


class _Verdict:
    """The ticket's outcome as the post-run gates keep revising it."""

    __slots__ = ("outcome", "status", "reason")

    def __init__(self, outcome: str, reason: str) -> None:
        self.outcome = outcome
        self.reason = reason
        self.status = _VERDICT_TO_STATUS.get(outcome, "blocked")

    def demote(self, outcome: str, status: str, reason: str | None = None) -> None:
        self.outcome = outcome
        self.status = status
        if reason is not None:
            self.reason = reason


def _initial_verdict(ticket, state) -> _Verdict:
    """The pipeline's verdict, with the Enhancer's block sentinel honoured.

    Tickets are unattended — the Enhancer's "too vague to act on" sentinel (its
    stand-in for a clarifying question) is caught BEFORE it silently flows into
    the Planner/Doer as if it were a real brief, burning a full pipeline run on
    garbage and risking a PR from whatever the Doer made of it. This sentinel
    was documented in the prompt but never actually checked anywhere — a dead
    contract.
    """
    pkg = _pkg()
    outcome = pkg._extract_verdict(state)
    block_reason = pkg._enhancer_block_reason(state)
    if block_reason is not None:
        log.warning("ticket=%s enhancer blocked: %s",
                    ticket.identifier, block_reason)
        return _Verdict("fail", block_reason)
    return _Verdict(outcome, pkg._extract_reason(state, outcome))


def _validator_out(state):
    out = state.get("validator_verdict") if state else None
    if not isinstance(out, str):
        return out
    try:
        import json as _json
        return _json.loads(out)
    except Exception:  # noqa: BLE001
        return {"raw": out[:400]}


def _apply_pr_demotions(ticket, v: _Verdict, pr_meta: dict) -> None:
    """The two ground-truth checks git can make that the model's prose can't."""
    if pr_meta.get("pr_skip_reason") == "test_only_diff":
        # git_pr rejected the push because the Doer only wrote tests / fixtures
        # with no edit to src/main. Demote so the ticket lands blocked (not
        # done) and the operator sees a clear reason instead of an empty PR.
        log.warning("ticket=%s test_only_diff — demoting verdict to fail. "
                    "Doer wrote: %s", ticket.identifier,
                    ", ".join(pr_meta.get("test_only_files", [])[:5]))
        v.demote("fail", "blocked")
        return
    # Empty-diff false pass (root cause of "done but nothing changed"): the Doer
    # changed NO files (clean tree → pr_skip_reason='no_changes', no PR) yet the
    # verdict came back non-fail. It narrated an edit it never wrote —
    # feedback/validator trusted the prose, not ground truth (git diff). A pass
    # with zero file changes is never a real pass. (ONE-163/164: both "done",
    # both empty, no commit.) Escape hatch: AIFORGE_ALLOW_EMPTY_PASS=1 keeps the
    # old trust-the-narration path. NB: the verdict_attempt row was already
    # recorded; don't re-record (the test_only_diff demotion doesn't either).
    if (pr_meta.get("pr_skip_reason") == "no_changes"
            and not pr_meta.get("pr_url")
            and v.outcome not in ("scope_violation", "fail")
            and os.environ.get("AIFORGE_ALLOW_EMPTY_PASS", "0")
                not in ("1", "true")):
        log.warning(
            "ticket=%s verdict=%s but clean tree (no_changes) — Doer wrote "
            "nothing; demoting to blocked (false pass on empty diff).",
            ticket.identifier, v.outcome)
        v.demote("fail", "blocked",
                 "empty diff: Doer reported success but changed no files "
                 "(no edit reached the worktree). Not actually done.")
        return
    # Committed-but-partial: the Doer plateaued / hit its budget but DID land a
    # reviewable diff (PR opened). Route to in_review so a human reviews the
    # partial PR, rather than blocked. This is the terminal for the
    # plateau/replan cap (see graph_pipeline._validator_gate): finished-but-
    # imperfect work stops churning and waits at the gate. With no PR there is
    # nothing to review, so partial stays blocked (the _VERDICT_TO_STATUS
    # default).
    if v.outcome == "partial" and pr_meta.get("pr_url"):
        v.demote("partial", "in_review")
        log.info("ticket=%s partial+PR → in_review (plateau cap; no replan of "
                 "finished work)", ticket.identifier)


def _auto_merge(ticket, pr_url: str, pr_meta: dict) -> None:
    """live_verifier validated the behaviour → merge the PR. For
    deploy_target=qa/prod the deploy recipe already merged it, so merge_pr
    reports already_merged — still surfaced so the operator sees the final
    state."""
    try:
        from ..git_pr import merge_pr
        merge_meta = merge_pr(pr_url)
        pr_meta["pr_merged"] = merge_meta.get("merged")
        pr_meta["pr_merge_reason"] = merge_meta.get("reason")
        log.info("ticket=%s auto-merge merged=%s reason=%s", ticket.identifier,
                 merge_meta.get("merged"), merge_meta.get("reason"))
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-merge failed: %s", exc)


def _live_verify(ticket, pr_meta: dict, v: _Verdict) -> dict | None:
    """Runs HERE (post-PR) so its deploy recipe has a real PR_URL to merge +
    roll out before testing. Only when the PR actually opened and the verdict is
    otherwise a pass. A failing live verify flips the ticket to blocked so the
    operator knows the merged/worktree fix didn't actually hold."""
    if not (pr_meta.get("pr_url") and v.outcome == "pass"
            and os.environ.get("AIFORGE_LIVE_VERIFIER", "1") in {"1", "true"}):
        return None
    try:
        lv = _pkg()._run_live_verifier(ticket, pr_meta["pr_url"])
    except Exception as exc:  # noqa: BLE001
        log.warning("live_verifier standalone failed: %s", exc)
        return None
    if lv is None:
        return None
    if lv.get("ok") is False:
        rationale = (lv.get("rationale") or "")
        v.demote("fail", "blocked", f"live_verifier rejected: {rationale[:300]}")
        log.warning("ticket=%s live_verifier ok=false: %s",
                    ticket.identifier, rationale[:200])
    elif lv.get("ok") is True and os.environ.get(
            "AIFORGE_AUTO_MERGE_ON_VALIDATE", "1") in {"1", "true"}:
        _auto_merge(ticket, pr_meta["pr_url"], pr_meta)
    return lv


def _grade_ci(pr_meta: dict) -> dict:
    """C1: grade the PR's CI runs once the push is in. Empty PR metadata (no
    diff to ship) skips this. Soft-fail: any ``gh`` error lands in pr_meta as
    ``ci_*`` keys for an operator to inspect, never blocks the status update."""
    if not (pr_meta.get("pr_url")
            and os.environ.get("AIFORGE_CI_GRADE", "1") in {"1", "true"}):
        return {}
    try:
        from aiforge_core.runtime.ci_feedback import grade_and_react
        return grade_and_react(
            pr_meta["pr_url"],
            poll_seconds=int(os.environ.get("AIFORGE_CI_POLL_S", "30")))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def _review_pr_meta(ticket, pr_meta: dict) -> dict:
    """C2: second-agent PR review pass. Posts a structured verdict comment so a
    human (or follow-up agent) can react. Soft-fail."""
    if not pr_meta.get("pr_url"):
        return {}
    try:
        from aiforge_core.runtime.pr_reviewer import review_pr
        return review_pr(pr_meta["pr_url"], ticket.title or "",
                         ticket.body or "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def _status_metadata(state, v: _Verdict, pr_meta: dict, ci_meta: dict,
                     review_meta: dict, validator_out, lv) -> dict:
    return {
        "feedback_verdict": v.outcome,
        "verifier_verdict": _pkg()._extract_verifier(state),
        **pr_meta,
        **({"ci_status": ci_meta.get("status"),
            "ci_rolled_back": ci_meta.get("rolled_back", False),
            "ci_checks": ci_meta.get("checks") or []} if ci_meta else {}),
        **({"review_verdict": review_meta.get("verdict"),
            "review_axes": review_meta.get("axes") or {}}
           if review_meta and review_meta.get("ok") else {}),
        **({"validator_verdict": (validator_out or {}).get("verdict"),
            "validator_rationale": (validator_out or {}).get("rationale"),
            "validator_scope_ok": (validator_out or {}).get("scope_ok"),
            "validator_regression_risk":
                (validator_out or {}).get("regression_risk")}
           if validator_out else {}),
        # Provenance: which path finished the ticket. "pipeline" = the
        # configured-model pipeline (+ retry chain) cleared it.
        "handled_by": "pipeline",
        **({"live_verifier_ok": (lv or {}).get("ok"),
            "live_verifier_rationale": (lv or {}).get("rationale"),
            "live_handoff": (lv or {}).get("live_handoff", False),
            "handoff_brief": (lv or {}).get("handoff_brief")} if lv else {}),
    }
