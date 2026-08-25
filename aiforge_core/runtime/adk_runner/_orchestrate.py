"""Ticket-loop orchestration — the thin driver that ties the package
together.

Composes the seed prompt, resolves per-ticket overrides, claims + runs
one ticket end-to-end (workspace → pipeline → verdict → PR → status),
and exposes :func:`main`, the single-shot systemd entrypoint.
"""
from __future__ import annotations

import contextlib
import asyncio
import os
import time
from typing import Any

from .. import memory_block
from ..git_pr import commit_push_open_pr
from ..pipeline import set_force_provider
from ..researcher_routing import should_skip_researcher
from ._base import _VERDICT_TO_STATUS, log, tickets_mod
from ._pipeline import _run_live_verifier, _run_pipeline
from ._verdict import (
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


def _operator_comments_block(ticket) -> str:
    """Operator follow-up comments: anything the human added via
    POST /api/tickets/{id}/comments after ticket creation.

    The Enhancer would otherwise never see this signal — it only reads
    ``ticket.body``. Folded in chronological order; bot/agent comments are
    excluded so the Doer doesn't loop on its own past commentary.
    """
    try:
        evts = tickets_mod.comments(ticket.id) or []
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("comment_fold_failed: %s", exc)
        return ""
    human = [e for e in evts
             if e.get("kind") == "comment"
             and (e.get("agent_role") or "").lower() == "human"
             and (e.get("body") or "").strip()]
    if not human:
        return ""
    out = ("\n## Operator follow-up comments\n"
           "These were posted on the ticket AFTER it was opened. "
           "Treat them as authoritative extensions of the body.\n\n")
    for c in human:
        out += (f"- _{str(c.get('created_at') or '')[:19]}_:\n"
                f"  {(c.get('body') or '').strip()}\n")
    return out


def _attachments_block(ticket) -> str:
    """List attachment paths so the Doer can ``file_read`` them via its file
    tools. The files are materialized into the worktree (see
    _materialize_attachments_in_worktree); each entry is the workspace-relative
    path the API persisted."""
    files = (ticket.metadata or {}).get("attached_files") or []
    rows = [f for f in files if isinstance(f, dict) and f.get("path")]
    if not rows:
        return ""
    out = "\n## Attached files (read these BEFORE you start)\n"
    for f in rows:
        out += (f"- `{f.get('path', '')}` ({f.get('name', '')}, "
                f"{f.get('size', '?')} bytes)\n")
    return out + ("\nThese files were uploaded by the operator with the "
                  "ticket. Use `file_read` to load them — their context is "
                  "REQUIRED for the change.\n")


def _playbook_prefix(hay: str, repo_cwd) -> tuple[str, list, list]:
    """``(prefix, skill names, workflow names)``.

    Relevance-searches the skill registry (SKILL.md playbooks, incl. ones the
    Doer authored via learn_skill) + always-on repo skills, and the workflow
    registry, keyed on ticket title + body. Best-effort: parse failures
    swallowed.
    """
    prefix = ""
    used_skills: list = []
    used_workflows: list = []
    try:
        from aiforge_core.runtime import skills as _skills
        block = _skills.auto_context(hay, repo_cwd)
        if block:
            prefix = block + "\n\n"
            used_skills = _skills.selected_names(hay, repo_cwd)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("skills.inject failed: %s", exc)
    try:
        from aiforge_core.runtime import workflows as _workflows
        block = _workflows.auto_context(hay, repo_cwd)
        if block:
            prefix = block + "\n\n" + prefix
            used_workflows = _workflows.selected_names(hay, repo_cwd)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("workflows.inject failed: %s", exc)
    return prefix, used_skills, used_workflows


def _emit_context_injected(ticket, skills: list, workflows: list) -> None:
    """Workflow-transparency: record which skills/workflows this run pulled in,
    so the Workflow UI can show it on the graph. Never blocks."""
    try:
        from aiforge_core.runtime import observability as _obs
        tid = getattr(ticket, "id", None)
        if tid is not None and (skills or workflows):
            _obs.emit_context_injected(ticket_id=tid, agent_role="pipeline",
                                       skills=skills, workflows=workflows)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("context_injected.emit (skills/workflows) failed: %s", exc)


def _vision_block(ticket) -> str:
    """Vision attach hint (sub #6). When the ticket has image attachments AND
    the active Doer model supports vision, list them with a flag so the model
    knows it can request a multimodal turn. Actual content-block conversion
    lives in vision.attach_image; wiring it through ADK's LlmRequest.contents
    shape is a follow-up."""
    try:
        from aiforge_core.config.agent_config import load_all as get_config
        from aiforge_core.runtime.vision import supports_vision
        doer_model = (get_config().get("doer", {}) or {}).get("model", "")
        if not supports_vision(doer_model):
            return ""
        images = [f for f in ((ticket.metadata or {}).get("attached_files") or [])
                  if isinstance(f, dict)
                  and str(f.get("name", "")).lower().endswith(
                      (".png", ".jpg", ".jpeg", ".gif", ".webp"))]
        if not images:
            return ""
        out = ("\n## Multimodal images (vision-enabled model)\n"
               "These attachments are images. Call `vision.attach_image`\n"
               "to convert each into multimodal content blocks.\n")
        return out + "".join(f"- `{img.get('path','')}`\n" for img in images)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("vision.attach_hint failed: %s", exc)
        return ""


def _build_prompt(ticket, memory_md: str) -> str:
    """Compose the seed prompt for the SequentialAgent.

    NOTE: memory_md is NO LONGER appended to the seed. The seed is replayed in
    contents on every chat-mode LLM call (60-120×/ticket ≈ 40-80K tokens of pure
    memory-block repetition). The block now seeds state['memory_brief_md']
    instead → merged once into {context_brief_md?} / {memory_brief_md?}
    instruction injections, which also survive compaction. (Param retained for
    call-site compatibility; the runner routes it into initial_state instead.)
    """
    _ = memory_md
    body = (f"# Ticket {ticket.identifier}\n"
            f"## Title\n{ticket.title}\n\n"
            f"## Body\n{ticket.body or '(no body)'}\n"
            + _operator_comments_block(ticket)
            + _attachments_block(ticket))
    # Pass the ticket's repo root so REPO-SCOPED skills/workflows (in
    # <repo>/.aiforge/…) load too, not just the global ones. cwd=None loaded
    # global-only, so a repo-specific playbook was silently ignored by the
    # pipeline. Falls back to None (global-only) when the worktree isn't set.
    repo_cwd = os.environ.get("AIFORGE_REPO_ROOT") or None
    hay = f"{ticket.title or ''} {ticket.body or ''}"
    prefix, used_skills, used_workflows = _playbook_prefix(hay, repo_cwd)
    _emit_context_injected(ticket, used_skills, used_workflows)
    return prefix + body + _vision_block(ticket)


def _ticket_force_provider(ticket) -> str | None:
    """Per-ticket pipeline override — pin the whole run onto one provider
    via ``ticket.metadata['force_provider']``.

    Only honours providers that still exist in the registry, so a stale
    ticket carrying a retired provider marker is silently
    ignored (the run falls back to the role's configured model) instead
    of crashing the pipeline.
    """
    md = ticket.metadata or {}
    forced = md.get("force_provider")
    if isinstance(forced, str) and forced:
        try:
            from aiforge_core.config.agent_config import PROVIDERS
            if forced in PROVIDERS:
                return forced
        except Exception:  # noqa: BLE001
            pass
    return None


def _external_refs(ticket) -> list[str]:
    """The ticket's external refs. Empty when it has none or no target repo."""
    if not ticket.project:
        return []
    refs = (ticket.metadata or {}).get("external_refs") or []
    return [r for r in refs if isinstance(r, str) and r.strip()]


def _neo4j_driver():
    """A Neo4j driver, or None when the package or the server is unavailable."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD",
                        os.environ.get("NEO4J_PASSWORD", "password"))
    try:
        return GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.debug("external_ingest driver fail: %s", exc)
        return None


def _ingest_ticket_external_refs(ticket) -> None:
    """Gap-9 wire-in: feed ``ticket.metadata.external_refs`` (a list
    of URLs / paths) through :func:`ingest_external_source` so the
    Doer's memory_block sees their content.

    Each ref ingests as one ``Doc_v2`` + chunked ``Note_v2`` keyed on
    the ref URI. AFM dedupes by URI at the Doc level (second ingest of
    the same URL re-uses the node), so the cost of re-ingesting is
    bounded.

    Disable with ``AIFORGE_EXTERNAL_INGEST=0``. Soft-fail on any
    backend error.
    """
    # The egress gate stays HERE, at the call site that reaches the network —
    # tests/python/runtime/test_web_egress_gated.py reads this function's source
    # to prove the control exists where the egress happens.
    if os.environ.get("AIFORGE_EXTERNAL_INGEST", "1") in ("0", "false", ""):
        return
    refs = _external_refs(ticket)
    if not refs:
        return
    try:
        from aiforge_memory.features.external_ingest import (
            ingest_external_source,
        )
    except ImportError:
        return
    drv = _neo4j_driver()
    if drv is None:
        return
    md = ticket.metadata or {}
    try:
        for src in refs[:5]:  # cap fan-out per ticket
            try:
                out = ingest_external_source(
                    drv, source=src, repo=ticket.project,
                    source_type=md.get("external_refs_type", "external"),
                    tags=[f"ticket:{ticket.identifier}"])
                log.info("external_ingest ticket=%s src=%s ok=%s notes=%d",
                         ticket.identifier, src, out.get("ok"),
                         len(out.get("note_ids") or []))
            except Exception as exc:  # noqa: BLE001
                log.debug("external_ingest failed for %s: %s", src, exc)
    finally:
        with contextlib.suppress(Exception):
            drv.close()


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
    if os.environ.get("AIFORGE_SPEC_TO_TESTS", "1") not in {"1", "true"} \
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


def _initial_verdict(ticket, state) -> _Verdict:
    """The pipeline's verdict, with the Enhancer's block sentinel honoured.

    Tickets are unattended — the Enhancer's "too vague to act on" sentinel (its
    stand-in for a clarifying question) is caught BEFORE it silently flows into
    the Planner/Doer as if it were a real brief, burning a full pipeline run on
    garbage and risking a PR from whatever the Doer made of it. This sentinel
    was documented in the prompt but never actually checked anywhere — a dead
    contract.
    """
    outcome = _extract_verdict(state)
    block_reason = _enhancer_block_reason(state)
    if block_reason is not None:
        log.warning("ticket=%s enhancer blocked: %s",
                    ticket.identifier, block_reason)
        return _Verdict("fail", block_reason)
    return _Verdict(outcome, _extract_reason(state, outcome))


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
        lv = _run_live_verifier(ticket, pr_meta["pr_url"])
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
        "verifier_verdict": _extract_verifier(state),
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
    if _clarify_parked(ticket):
        return True
    _probe_local_lm(ticket)

    worktree, prior_env = _setup_ticket_workspace(ticket)
    if not worktree:
        tickets_mod.update_status(ticket.id, "blocked", role="adk_runner",
                                  metadata_patch=_no_repo_metadata(ticket))
        _restore_env(prior_env)
        return True
    _prepare_worktree(ticket, worktree)
    try:
        _run_ticket(ticket, worktree)
    except Exception as exc:  # noqa: BLE001 — a ticket must never kill the runner
        _log_run_failure(ticket, exc)
        rescue_meta = _rescue_partial_work(ticket)
        try:
            tickets_mod.update_status(
                ticket.id, "blocked", role="adk_runner",
                metadata_patch={"error": str(exc)[:500], **rescue_meta})
        except Exception:  # noqa: BLE001
            pass
    finally:
        # Always clear the per-ticket override so the next claim builds against
        # the operator's profile, not the previous ticket's forced provider.
        set_force_provider(None)
        _restore_env(prior_env)
    return True


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
        # so an idle queue (a fresh process every ~10s) doesn't spam the log.
        backends.boot_log()
        return 0
    time.sleep(int(os.environ.get("AIFORGE_POLL_IDLE_S", "10")))
    return 0
