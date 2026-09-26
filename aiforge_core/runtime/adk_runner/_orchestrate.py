"""Ticket-loop orchestration — the thin driver that ties the package
together.

Claims + runs one ticket end-to-end (workspace → pipeline → verdict → PR →
status) and exposes :func:`main`, the single-shot systemd entrypoint. The seed
prompt and per-ticket overrides live in ``_prompt``; the post-run verdict, PR,
CI and status steps in ``_outcome``.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from .. import memory_block
from ..git_pr import commit_push_open_pr
from ..pipeline import set_force_provider
from ..researcher_routing import should_skip_researcher
from ._base import _VERDICT_TO_STATUS, log, tickets_mod  # noqa: F401  # tests read these here
from ._outcome import (  # noqa: F401  # re-exported
    _apply_pr_demotions,
    _auto_merge,
    _grade_ci,
    _initial_verdict,
    _live_verify,
    _review_pr_meta,
    _status_metadata,
    _validator_out,
    _Verdict,
)
from ._pipeline import _run_live_verifier, _run_pipeline  # noqa: F401  # tests patch it here
from ._prompt import (  # noqa: F401  # re-exported
    _attachments_block,
    _build_prompt,
    _emit_context_injected,
    _external_refs,
    _ingest_ticket_external_refs,
    _operator_comments_block,
    _playbook_prefix,
    _ticket_force_provider,
    _vision_block,
)
from ._verdict import (  # noqa: F401  # the moved code looks some up here
    _enhancer_block_reason,
    _extract_reason,
    _extract_verdict,
    _extract_verifier,
    _record_verdict_event,
    _ticket_looks_readonly,
)
from ._workspace import (
    _materialize_attachments_in_worktree,
    _persist_ticket_media,
    _restore_env,
    _setup_ticket_workspace,
)


def _clarify_parked(ticket) -> bool:
    """Interactive (chat) runs may pause to ask clarifying questions before any
    pipeline work. Static tickets skip this entirely. True when it parked the
    ticket awaiting the user's answer."""
    try:
        from ..clarify import maybe_clarify
        return bool(maybe_clarify(ticket))
    except Exception as exc:  # noqa: BLE001
        log.debug("clarify gate skipped: %s", exc)
        return False


def _doer_is_loopback() -> bool:
    try:
        from aiforge_core.config import agent_config as _acfg
        base = (_acfg.get("doer") or {}).get("base_url") or ""
    except Exception:  # noqa: BLE001
        base = ""
    return (not base or "127.0.0.1:1234" in base or "localhost:1234" in base)


def _probe_local_lm(ticket) -> None:
    """LM Studio liveness check + opportunistic tunnel restart.

    ONLY relevant when the Doer points at the loopback mlx-lm box
    (127.0.0.1:1234) — if the operator configured a remote OpenAI-compatible
    endpoint, that local box is irrelevant and we must NOT probe it.
    ``AIFORGE_LM_HEALTH=0`` opts out entirely. When the endpoint is unreachable
    the per-call EscalatingLlm retry chain surfaces the error — no
    whole-pipeline force needed here.
    """
    if not (_doer_is_loopback()
            and os.environ.get("AIFORGE_LM_HEALTH", "1") in {"1", "true"}):
        return
    try:
        from aiforge_core.runtime.lm_health import check_lm_health
        health = check_lm_health(restart_on_fail=True)
        if not health.get("doer_ok"):
            log.warning(
                "ticket=%s local LM unreachable — pipeline relies on the "
                "configured retry chain (restarted=%s)",
                ticket.identifier, health.get("restarted"))
    except Exception as exc:  # noqa: BLE001
        log.debug("lm_health probe skipped: %s", exc)


def _no_repo_metadata(ticket) -> dict:
    root = os.environ.get("AIFORGE_WORKTREE_ROOT", "~/codeRepo")
    return {"error": (f"no target repo for project={ticket.project!r}; "
                      "set ticket.project to a directory under "
                      f"AIFORGE_WORKTREE_ROOT ({root})")[:500]}


def _write_spec_scaffold(ticket, worktree: str) -> None:
    """C5: spec → failing-test scaffold. Parses the ticket body's "Acceptance"
    bullets and writes a per-language test file under ``tests/aiforge_spec/``,
    so the Doer's run_tests has a TDD target.

    SKIPPED for a read-only / analysis / comment-only ticket — otherwise the
    scaffold writes a test file, dirties the tree, and a ticket that meant only
    to read/comment gets a spurious PR (or a test_only_diff → blocked).
    """
    # Off by default: planting a failing test before the code exists is what
    # makes the doer chase a scaffold it just invented. Opt in with
    # AIFORGE_SPEC_TO_TESTS=1.
    if os.environ.get("AIFORGE_SPEC_TO_TESTS", "0") not in {"1", "true"} \
            or _ticket_looks_readonly(ticket):
        return
    try:
        from aiforge_core.runtime.spec_to_tests import write_scaffold
        write_scaffold(ticket.identifier, ticket.body or "",
                       repo_root=worktree,
                       language=(ticket.metadata or {}).get("test_language",
                                                            "python"))
    except Exception as exc:  # noqa: BLE001
        log.debug("spec_to_tests skipped: %s", exc)


def _prepare_worktree(ticket, worktree: str) -> None:
    """Everything the Doer needs on disk before the pipeline starts."""
    # Pull operator-uploaded files into the per-ticket worktree so the Doer can
    # ``file_read`` them by the same relative path the Doer prompt references.
    _materialize_attachments_in_worktree(ticket, worktree)
    # Gap-10 wire-in: persist any image attachments as an Observation_v2 with
    # ``media_refs`` so future tickets can recall "this ticket had screenshots
    # X / Y" — even before the vision embedder lands.
    try:
        _persist_ticket_media(ticket)
    except Exception as exc:  # noqa: BLE001 — must never break the ticket loop
        log.debug("vision persist wrapper caught: %s", exc)
    _write_spec_scaffold(ticket, worktree)
    # Gap-9 wire-in: when the ticket metadata lists external references
    # (Confluence / Slack thread / Jira ticket / plain URLs), pull them into AFM
    # via the external_ingest spine so the Doer's memory_block hit list can
    # include their content.
    _ingest_ticket_external_refs(ticket)


def _arm_deploy_env(ticket) -> None:
    """Deploy autonomy — when the operator chose 'qa' or 'prod' at
    ticket-creation time, the deploy recipe will merge the PR + wait for the new
    SHA. Both env knobs are armed in the runner's process so the live_verifier's
    bash commands see them; ``_restore_env`` resets them afterwards."""
    target = ((ticket.metadata or {}).get("deploy_target") or "none").lower()
    if target in {"qa", "prod"}:
        os.environ["AIFORGE_AUTO_MERGE"] = "1"
        os.environ["AIFORGE_DEPLOY_TARGET"] = target
        log.info("ticket=%s deploy_target=%s (auto-merge armed)",
                 ticket.identifier, target)
        return
    # Belt-and-braces: a previous run with deploy_target=qa MUST NOT leak its
    # auto-merge env into the next claim.
    os.environ.pop("AIFORGE_AUTO_MERGE", None)
    os.environ.pop("AIFORGE_DEPLOY_TARGET", None)


def _run_ticket(ticket, _worktree: str) -> None:
    """The pipeline run and everything that follows from its verdict."""
    memory_md = memory_block.fetch(ticket)
    # Enhancer + Validator run as proper ADK LlmAgents inside the pipeline (see
    # pipeline.build_pipeline). The enhanced body lands in
    # state['enhanced_body'] for the Planner/Doer; the validator's verdict lands
    # in state['validator_verdict'] for the runner to fold into ticket metadata.
    prompt = _build_prompt(ticket, memory_md)
    forced = _ticket_force_provider(ticket)
    set_force_provider(forced)
    if forced:
        log.info("ticket=%s force_provider=%s", ticket.identifier, forced)
    _arm_deploy_env(ticket)
    # Researcher routing: skip the read-only context gatherer on greenfield
    # tickets where the body has no reference patterns AND the repo's git log
    # doesn't mention the project keyword. Saves ~5 LM calls + ~4min on tickets
    # where Researcher would find nothing relevant. AIFORGE_RESEARCHER_FORCE=1
    # overrides.
    skip_researcher, skip_reason = should_skip_researcher(
        ticket.title or "", ticket.body or "")
    log.info("ticket=%s researcher=%s reason=%s", ticket.identifier,
             "skip" if skip_researcher else "run", skip_reason)

    state = asyncio.run(_run_pipeline(
        prompt, skip_researcher=skip_researcher, ticket=ticket,
        memory_md=memory_md))
    v = _initial_verdict(ticket, state)
    enhancer_blocked = _enhancer_block_reason(state) is not None
    # Capture the Feedback rationale BEFORE any mutation so an operator scanning
    # ticket_events sees both the verdict and the convergence reason.
    _record_verdict_event(ticket.id, v.outcome, v.reason)

    # PR gate: anything that ISN'T an explicit scope_violation is eligible.
    # `commit_push_open_pr` itself short-circuits on a clean tree, so verdict=
    # fail with no edits stays a no-op. Enhancer-blocked tickets are excluded
    # outright — never open a PR built from a Doer acting on a garbage brief.
    pr_meta: dict[str, Any] = {}
    if v.outcome != "scope_violation" and not enhancer_blocked:
        pr_meta = commit_push_open_pr(ticket)
    _apply_pr_demotions(ticket, v, pr_meta)
    lv = _live_verify(ticket, pr_meta, v)

    tickets_mod.update_status(
        ticket.id, v.status, role="adk_runner",
        metadata_patch=_status_metadata(state, v, pr_meta, _grade_ci(pr_meta),
                                        _review_pr_meta(ticket, pr_meta),
                                        _validator_out(state), lv))
    log.info("ticket=%s status=%s verdict=%s", ticket.identifier, v.status,
             v.outcome)


def _rescue_partial_work(ticket) -> dict:
    """Even on ADK failure the Doer may have written real files before the
    orchestrator stalled. Surface that work as a draft PR for human review
    instead of dropping it; commit_push_open_pr short-circuits with
    pr_skip_reason=no_changes on a clean tree."""
    try:
        meta = commit_push_open_pr(ticket)
        if meta.get("pr_url"):
            log.info("ticket=%s rescued partial work as PR despite "
                     "ADK failure: %s", ticket.identifier, meta["pr_url"])
        return meta
    except Exception as exc:  # noqa: BLE001
        log.warning("ticket=%s PR rescue also failed: %s", ticket.identifier, exc)
        return {}


def _log_run_failure(ticket, exc: Exception) -> None:
    """The concrete cause is already surfaced concisely upstream (EscalatingLlm's
    ``llm.exhausted`` / ``llm.attempt_failed`` lines). A full chained traceback
    here is redundant noise for the common case (flaky/down model). Restore the
    raw stack with AIFORGE_ADK_TRACEBACKS=1 for novel failures."""
    if str(os.environ.get("AIFORGE_ADK_TRACEBACKS", "")).strip().lower() in (
            "1", "true", "yes", "on"):
        log.exception("ticket=%s failed during ADK run: %s",
                      ticket.identifier, exc)
    else:
        log.error("ticket=%s failed during ADK run: %s: %s",
                  ticket.identifier, type(exc).__name__, str(exc)[:400])


def _process_one_ticket() -> bool:
    """Claim + run one ticket. Returns True when one ran, False on
    empty queue (caller exits + lets systemd back off)."""
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        return False
    log.info("claimed ticket=%s title=%r", ticket.identifier, ticket.title)
    from aiforge_core.tickets.lease import defer, hold_claim, worktree_lock
    # The claim is renewed for as long as this run lives, so no reaper can
    # requeue it mid-run; siblings sharing the root's worktree take turns.
    root = _root_identifier(ticket)
    with hold_claim(ticket.id), worktree_lock(root) as held:
        if not held:
            log.info("ticket=%s deferred: worktree %s is in use by another run",
                     ticket.identifier, root)
            defer(ticket.id, reason=f"worktree {root} in use by another run")
            return True
        _run_claimed_ticket(ticket)
    return True


def _root_identifier(ticket) -> str:
    try:
        from aiforge_core.runtime.workspace import _root_ticket
        return _root_ticket(ticket).identifier
    except Exception:  # noqa: BLE001
        return ticket.identifier


def _run_workflow_ticket(ticket) -> None:
    """A ticket routed to a named workflow (route='workflow') runs THAT
    workflow's handler — not the code pipeline. The route was stored, shown in
    the UI and validated on save, but nothing ever called
    ``workflows.dispatch``: a Tally trial-balance ticket went through the LLM
    code cascade in a repo worktree instead of its deterministic handler.

    The handler returns a doer-outcome dict. Its report (``udiff`` is the
    Markdown for a report workflow) lands as a comment; material problems block
    the ticket, a clean run completes it. An unknown workflow id raises, and
    the caller blocks the ticket with the error."""
    import dataclasses

    from aiforge_core import workflows
    wf = ticket.route_workflow or ""
    log.info("ticket=%s route=workflow workflow=%s", ticket.identifier, wf)
    out = workflows.dispatch(wf, dataclasses.asdict(ticket), log=log) or {}
    report = str(out.get("udiff") or "")
    if report:
        tickets_mod.add_comment(ticket.id, "workflow", report[:60000],
                                {"workflow": wf, "target": out.get("target")})
    problems = list(out.get("problems") or [])[:20]
    blocked = bool(out.get("blocked_by_detectors"))
    patch = {"workflow": wf, "workflow_target": out.get("target"),
             "workflow_problems": problems}
    if blocked:
        patch["blocked_reason"] = ("; ".join(
            f"{p.get('mode', 'problem')}: {p.get('evidence', '')}"[:300]
            for p in problems if isinstance(p, dict))
            or "the workflow reported material gaps")
    tickets_mod.update_status(ticket.id, "blocked" if blocked else "done",
                              role="workflow", metadata_patch=patch)


def _run_claimed_ticket(ticket) -> None:
    """Everything after the claim, inside ONE try: a failure in workspace setup
    or preparation used to escape it and strand the ticket in_progress until
    the lease lapsed — and then loop back through a reclaim."""
    prior_env = None
    try:
        if getattr(ticket, "route", "code") == "workflow":
            _run_workflow_ticket(ticket)       # deterministic: no brief, no repo
            return
        if _clarify_parked(ticket):
            return
        _probe_local_lm(ticket)
        worktree, prior_env = _setup_ticket_workspace(ticket)
        if not worktree:
            tickets_mod.update_status(ticket.id, "blocked", role="adk_runner",
                                      metadata_patch=_no_repo_metadata(ticket))
            return
        _prepare_worktree(ticket, worktree)
        _run_ticket(ticket, worktree)
    except Exception as exc:  # noqa: BLE001 — a ticket must never kill the runner
        _log_run_failure(ticket, exc)
        # A workflow ticket never had a worktree: the rescue would resolve the
        # runner's DEFAULT repo and commit + push whatever sat uncommitted there.
        rescue_meta = ({} if getattr(ticket, "route", "code") == "workflow"
                       else _rescue_partial_work(ticket))
        try:
            tickets_mod.update_status(
                ticket.id, "blocked", role="adk_runner",
                metadata_patch={"error": str(exc)[:500], **rescue_meta})
        except Exception as exc2:  # noqa: BLE001
            # Not silent: a ticket left in_progress here is reaped, and after
            # AIFORGE_TICKET_MAX_RECLAIMS it is blocked — say why in the log.
            log.error("ticket=%s: could not mark blocked after a failed run: %s",
                      ticket.identifier, exc2)
    finally:
        # Always clear the per-ticket override so the next claim builds against
        # the operator's profile, not the previous ticket's forced provider.
        set_force_provider(None)
        if prior_env is not None:
            _restore_env(prior_env)


def main() -> int:
    """Single-shot: claim one ticket, run it, exit."""
    from aiforge_core.config import backends
    # Hard-fail on a misconfigured data-driven deploy (runs every poll so a
    # broken config never silently writes SQLite). Cheap + silent on success.
    backends.require_data_backends()
    # Requeue tickets orphaned 'in_progress' by a hard-crashed prior runner
    # (OOM / SIGKILL / redeploy) BEFORE we try to claim — otherwise they stay
    # stuck forever (re-claim only selects 'todo'). Cheap + silent on an empty
    # queue; soft-fails so a reaper hiccup never blocks the poll.
    try:
        reaped = tickets_mod.reap_stale_in_progress()
        if reaped:
            log.info("reaped %d stale in_progress ticket(s) -> todo: %s",
                     len(reaped), reaped)
    except Exception as exc:  # noqa: BLE001
        log.debug("stale ticket reaper skipped: %s", exc)
    # Requeue memory sources stuck 'indexing' past their lease (a crashed
    # index thread never clears its own status). Shared SQLite file with the
    # API service; safe to run from here at boot.
    try:
        from aiforge_core.runtime import memory_sources as _ms
        _lease = int(os.environ.get("AIFORGE_INDEX_LEASE_S", "1800"))
        stale_idx = _ms.reap_stale_indexing(_lease)
        if stale_idx:
            log.info("reaped %d stale indexing source(s) -> idle: %s",
                     len(stale_idx), stale_idx)
    except Exception as exc:  # noqa: BLE001
        log.debug("stale index reaper skipped: %s", exc)
    if _process_one_ticket():
        # Announce the resolved backends only on polls that actually did work,
        # so an idle queue (a fresh process every few seconds) doesn't spam
        # the log.
        backends.boot_log()
    else:
        _idle_poll(backends)
    # Always 0: "did work" and "queue was empty" are both successful polls, and
    # the supervising loop (run.sh / docker entrypoint) restarts this process
    # either way. A real failure propagates as an exception, which exits non-0.
    return 0


def _env_s(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _poll_idle_s() -> float:
    """Sleep between polls of an empty queue. 2s (was 10s — and then the
    process exited and the supervisor waited another 10s plus a cold start, so
    a new ticket sat 20s+ before anything happened). AIFORGE_POLL_IDLE_S."""
    return _env_s("AIFORGE_POLL_IDLE_S", 2.0)


def _idle_poll(backends) -> None:
    """Empty queue: keep polling every :func:`_poll_idle_s` for a short
    window (AIFORGE_POLL_IDLE_WINDOW_S, default 30s) before exiting, so a new
    ticket is picked up within ~2s without paying a process respawn — imports
    and all — every 2s. Still one ticket per process: the first claim runs and
    the process exits after it, exactly as a fresh one would."""
    step = _poll_idle_s()
    end = time.monotonic() + _env_s("AIFORGE_POLL_IDLE_WINDOW_S", 30.0)
    while True:
        time.sleep(step)
        if step <= 0 or time.monotonic() >= end:
            return
        if _process_one_ticket():
            backends.boot_log()
            return
