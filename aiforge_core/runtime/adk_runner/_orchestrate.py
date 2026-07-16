"""Ticket-loop orchestration — the thin driver that ties the package
together.

Composes the seed prompt, resolves per-ticket overrides, claims + runs
one ticket end-to-end (workspace → pipeline → verdict → PR → status),
and exposes :func:`main`, the single-shot systemd entrypoint.
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


def _build_prompt(ticket, memory_md: str) -> str:
    """Compose the seed prompt for the SequentialAgent."""
    out = (
        f"# Ticket {ticket.identifier}\n"
        f"## Title\n{ticket.title}\n\n"
        f"## Body\n{ticket.body or '(no body)'}\n"
    )
    # Operator follow-up comments: anything the human added via
    # POST /api/tickets/{id}/comments after ticket creation. The
    # Enhancer would otherwise never see this signal — it only
    # reads ``ticket.body``. Folded in chronological order; bot/agent
    # comments are excluded so the Doer doesn't loop on its own
    # past commentary.
    try:
        evts = tickets_mod.comments(ticket.id) or []
        human_comments = [
            e for e in evts
            if e.get("kind") == "comment"
            and (e.get("agent_role") or "").lower() == "human"
            and (e.get("body") or "").strip()
        ]
        if human_comments:
            out += "\n## Operator follow-up comments\n"
            out += (
                "These were posted on the ticket AFTER it was opened. "
                "Treat them as authoritative extensions of the body.\n\n"
            )
            for c in human_comments:
                ts = str(c.get("created_at") or "")[:19]
                body = (c.get("body") or "").strip()
                out += f"- _{ts}_:\n  {body}\n"
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("comment_fold_failed: %s", exc)
    # Ticket attachments — list paths so the Doer can `file_read` them
    # via its file tools. The files are materialized into the worktree
    # (see _materialize_attachments_in_worktree). Each entry is the
    # workspace-relative path the API persisted.
    md = ticket.metadata or {}
    files = md.get("attached_files") or []
    if isinstance(files, list) and files:
        out += "\n## Attached files (read these BEFORE you start)\n"
        for f in files:
            if not isinstance(f, dict):
                continue
            path = f.get("path", "")
            size = f.get("size", "?")
            name = f.get("name", "")
            if path:
                out += f"- `{path}` ({name}, {size} bytes)\n"
        out += (
            "\nThese files were uploaded by the operator with the "
            "ticket. Use `file_read` to load them — their context is "
            "REQUIRED for the change.\n"
        )
    # NOTE: memory_md is NO LONGER appended to the seed. The seed is
    # replayed in contents on every chat-mode LLM call (60-120×/ticket ≈
    # 40-80K tokens of pure memory-block repetition). The block now seeds
    # state['memory_brief_md'] instead → merged once into
    # {context_brief_md?} / {memory_brief_md?} instruction injections,
    # which also survive compaction. (Param retained for call-site
    # compatibility; the runner routes it into initial_state instead.)
    _ = memory_md

    # Skill injection. Relevance-search the skill registry (SKILL.md playbooks,
    # incl. ones the Doer authored via learn_skill) + always-on repo skills,
    # keyed on ticket title + body. Best-effort: parse failures swallowed.
    hay = f"{ticket.title or ''} {ticket.body or ''}"
    # Pass the ticket's repo root so REPO-SCOPED skills/workflows (in
    # <repo>/.aiforge/…) load too, not just the global ones. cwd=None loaded
    # global-only, so a repo-specific playbook was silently ignored by the
    # pipeline. Falls back to None (global-only) when the worktree isn't set.
    _repo_cwd = os.environ.get("AIFORGE_REPO_ROOT") or None
    _used_skills: list[dict] = []
    _used_workflows: list[dict] = []
    try:
        from aiforge_core.runtime import skills as _skills
        sk_block = _skills.auto_context(hay, _repo_cwd)
        if sk_block:
            out = sk_block + "\n\n" + out
            _used_skills = _skills.selected_names(hay, _repo_cwd)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("skills.inject failed: %s", exc)

    # Workflows injection — same treatment as skills, so the pipeline agents
    # see relevant reusable end-to-end procedures (parity with the chat agent).
    try:
        from aiforge_core.runtime import workflows as _workflows
        wf_block = _workflows.auto_context(hay, _repo_cwd)
        if wf_block:
            out = wf_block + "\n\n" + out
            _used_workflows = _workflows.selected_names(hay, _repo_cwd)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("workflows.inject failed: %s", exc)

    # Workflow-transparency: record which skills/workflows this run pulled in,
    # so the Workflow UI can show it on the graph. Best-effort, never blocks.
    try:
        from aiforge_core.runtime import observability as _obs
        _tid = getattr(ticket, "id", None)
        if _tid is not None and (_used_skills or _used_workflows):
            _obs.emit_context_injected(
                ticket_id=_tid, agent_role="pipeline",
                skills=_used_skills, workflows=_used_workflows)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("context_injected.emit (skills/workflows) failed: %s", exc)

    # Vision attach hint (sub #6). When the ticket has image attachments
    # AND the active Doer model supports vision, list them with a flag so
    # the model knows it can request a multimodal turn. Actual content-
    # block conversion lives in vision.attach_image; wiring it through
    # ADK's LlmRequest.contents shape is a follow-up.
    try:
        from aiforge_core.config.agent_config import load_all as get_config
        from aiforge_core.runtime.vision import supports_vision
        cfg = get_config()
        doer_model = (cfg.get("doer", {}) or {}).get("model", "")
        if supports_vision(doer_model):
            md = ticket.metadata or {}
            images = [
                f for f in (md.get("attached_files") or [])
                if isinstance(f, dict)
                and str(f.get("name", "")).lower().endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp"),
                )
            ]
            if images:
                out += (
                    "\n## Multimodal images (vision-enabled model)\n"
                    "These attachments are images. Call `vision.attach_image`\n"
                    "to convert each into multimodal content blocks.\n"
                )
                for img in images:
                    out += f"- `{img.get('path','')}`\n"
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("vision.attach_hint failed: %s", exc)
    return out


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
    if os.environ.get("AIFORGE_EXTERNAL_INGEST", "1") in ("0", "false", ""):
        return
    md = ticket.metadata or {}
    refs = md.get("external_refs") or []
    refs = [r for r in refs if isinstance(r, str) and r.strip()]
    if not refs:
        return
    if not ticket.project:
        return
    try:
        from aiforge_memory.features.external_ingest import (
            ingest_external_source,
        )
        from neo4j import GraphDatabase
    except ImportError:
        return
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.debug("external_ingest driver fail: %s", exc)
        return
    try:
        for src in refs[:5]:  # cap fan-out per ticket
            try:
                out = ingest_external_source(
                    drv,
                    source=src,
                    repo=ticket.project,
                    source_type=md.get("external_refs_type", "external"),
                    tags=[f"ticket:{ticket.identifier}"],
                )
                log.info(
                    "external_ingest ticket=%s src=%s ok=%s notes=%d",
                    ticket.identifier, src, out.get("ok"),
                    len(out.get("note_ids") or []),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("external_ingest failed for %s: %s", src, exc)
    finally:
        try:
            drv.close()
        except Exception:
            pass


def _process_one_ticket() -> bool:
    """Claim + run one ticket. Returns True when one ran, False on
    empty queue (caller exits + lets systemd back off)."""
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        return False

    log.info("claimed ticket=%s title=%r", ticket.identifier, ticket.title)

    # Interactive (chat) runs may pause to ask clarifying questions before
    # any pipeline work. Static tickets skip this entirely. Returns True
    # when it parked the ticket awaiting the user's answer.
    try:
        from ..clarify import maybe_clarify
        if maybe_clarify(ticket):
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("clarify gate skipped: %s", exc)

    # LM Studio liveness check + opportunistic tunnel restart. ONLY
    # relevant when the Doer points at the loopback mlx-lm box
    # (127.0.0.1:1234) — if the operator configured a remote
    # OpenAI-compatible endpoint, that local box is irrelevant and we must
    # NOT probe it. ``AIFORGE_LM_HEALTH=0`` opts out entirely. When the
    # endpoint is unreachable the per-call EscalatingLlm retry chain
    # surfaces the error — no whole-pipeline force needed here.
    try:
        from aiforge_core.config import agent_config as _acfg
        _doer_base = (_acfg.get("doer") or {}).get("base_url") or ""
    except Exception:  # noqa: BLE001
        _doer_base = ""
    _doer_is_loopback = (
        not _doer_base
        or "127.0.0.1:1234" in _doer_base
        or "localhost:1234" in _doer_base
    )
    if (_doer_is_loopback
            and os.environ.get("AIFORGE_LM_HEALTH", "1") in {"1", "true"}):
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

    worktree, prior_env = _setup_ticket_workspace(ticket)
    if not worktree:
        tickets_mod.update_status(
            ticket.id, "blocked", role="adk_runner",
            metadata_patch={
                "error": (
                    f"no target repo for project={ticket.project!r}; "
                    "set ticket.project to a directory under "
                    f"AIFORGE_WORKTREE_ROOT ({os.environ.get('AIFORGE_WORKTREE_ROOT', '~/codeRepo')})"
                )[:500],
            },
        )
        _restore_env(prior_env)
        return True

    # Pull operator-uploaded files into the per-ticket worktree so the
    # Doer can ``file_read`` them by the same relative path the Doer
    # prompt references.
    _materialize_attachments_in_worktree(ticket, worktree)

    # Gap-10 wire-in: persist any image attachments as an
    # Observation_v2 with ``media_refs`` so future tickets can recall
    # "this ticket had screenshots X / Y" — even before the vision
    # embedder lands. Soft-fails on any backend error.
    try:
        _persist_ticket_media(ticket)
    except Exception as exc:  # noqa: BLE001 — vision persist must never break the ticket loop
        log.debug("vision persist wrapper caught: %s", exc)

    # C5: spec → failing-test scaffold. Parses ticket body's
    # "Acceptance" bullets and writes a per-language test file under
    # ``tests/aiforge_spec/``. Doer's run_tests then has a TDD target.
    # SKIP for a read-only / analysis / comment-only ticket — otherwise the
    # scaffold writes a test file, dirties the tree, and a ticket that meant only
    # to read/comment gets a spurious PR (or a test_only_diff → blocked).
    if os.environ.get("AIFORGE_SPEC_TO_TESTS", "1") in {"1", "true"} \
            and not _ticket_looks_readonly(ticket):
        try:
            from aiforge_core.runtime.spec_to_tests import write_scaffold
            md = ticket.metadata or {}
            language = md.get("test_language", "python")
            write_scaffold(
                ticket.identifier, ticket.body or "",
                repo_root=worktree, language=language,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("spec_to_tests skipped: %s", exc)

    # Gap-9 wire-in: when the ticket metadata lists external references
    # (Confluence / Slack thread / Jira ticket / plain URLs), pull them
    # into AFM via the external_ingest spine so the Doer's memory_block
    # hit list can include their content. ``external_refs`` is a list
    # of strings (URLs / paths) on ticket.metadata. Skipped silently
    # when absent. Soft-fail on any backend error.
    _ingest_ticket_external_refs(ticket)

    memory_md = memory_block.fetch(ticket)
    # Enhancer + Validator now run as proper ADK LlmAgents inside the
    # SequentialAgent (see :func:`pipeline.build_pipeline`). The
    # enhanced body lands in ``state['enhanced_body']`` for the
    # Planner/Doer; the validator's verdict lands in
    # ``state['validator_verdict']`` for the runner to fold into
    # ticket metadata.
    prompt = _build_prompt(ticket, memory_md)

    forced = _ticket_force_provider(ticket)
    set_force_provider(forced)
    if forced:
        log.info("ticket=%s force_provider=%s", ticket.identifier, forced)

    # Deploy autonomy — when the operator chose 'qa' or 'prod' at
    # ticket-creation time, the deploy recipe will merge the PR + wait
    # for the new SHA. We arm both env knobs in the runner's process
    # so the live_verifier's bash commands see them. Reset to the
    # prior values in the ``finally`` cleanup below (``_restore_env``).
    deploy_target = ((ticket.metadata or {}).get("deploy_target")
                     or "none").lower()
    if deploy_target in {"qa", "prod"}:
        os.environ["AIFORGE_AUTO_MERGE"] = "1"
        os.environ["AIFORGE_DEPLOY_TARGET"] = deploy_target
        log.info(
            "ticket=%s deploy_target=%s (auto-merge armed)",
            ticket.identifier, deploy_target,
        )
    else:
        # Belt-and-braces: a previous run with deploy_target=qa MUST
        # NOT leak its auto-merge env into the next claim.
        os.environ.pop("AIFORGE_AUTO_MERGE", None)
        os.environ.pop("AIFORGE_DEPLOY_TARGET", None)

    # Researcher routing: skip the read-only context gatherer on
    # greenfield tickets where the body has no reference patterns AND
    # the repo's git log doesn't mention the project keyword. Saves
    # ~5 LM calls + ~4min on tickets where Researcher would find
    # nothing relevant. ``AIFORGE_RESEARCHER_FORCE=1`` overrides.
    skip_researcher, skip_reason = should_skip_researcher(
        ticket.title or "", ticket.body or "",
    )
    log.info("ticket=%s researcher=%s reason=%s",
             ticket.identifier,
             "skip" if skip_researcher else "run", skip_reason)

    try:
        state = asyncio.run(_run_pipeline(
            prompt, skip_researcher=skip_researcher, ticket=ticket,
            memory_md=memory_md,
        ))
        outcome = _extract_verdict(state)
        # Tickets are unattended — catch the Enhancer's "too vague to act
        # on" sentinel (its stand-in for a clarifying question) BEFORE it
        # silently flows into the Planner/Doer as if it were a real brief,
        # burning a full pipeline run on garbage and risking a PR from
        # whatever the Doer made of it. This sentinel was documented in the
        # prompt but never actually checked anywhere — dead contract.
        _block_reason = _enhancer_block_reason(state)
        _enhancer_blocked = _block_reason is not None
        if _enhancer_blocked:
            outcome = "fail"
            log.warning("ticket=%s enhancer blocked: %s",
                       ticket.identifier, _block_reason)
        # Capture the Feedback rationale BEFORE any mutation so an
        # operator scanning ticket_events sees both the verdict and
        # the convergence reason. Best-effort: any persistence error
        # is logged + swallowed inside the helper so the runner still
        # makes forward progress on the ticket itself.
        reason = _block_reason if _enhancer_blocked else _extract_reason(state, outcome)
        _record_verdict_event(ticket.id, outcome, reason)

        # live_verifier no longer runs inside the pipeline — it runs
        # standalone AFTER the PR opens (see below) so its deploy recipe
        # has a real PR_URL to merge. ``lv`` is populated there.
        lv: dict | None = None

        new_status = _VERDICT_TO_STATUS.get(outcome, "blocked")

        # Validator (ADK LlmAgent) wrote validator_verdict into the
        # session state; surface it on the ticket so operators see the
        # final pre-PR verdict.
        validator_out: Any = state.get("validator_verdict") if state else None
        if isinstance(validator_out, str):
            try:
                import json as _json
                validator_out = _json.loads(validator_out)
            except Exception:
                validator_out = {"raw": validator_out[:400]}

        # The Doer (running on the operator's configured model + retry
        # chain) is the only path that finishes a ticket now — kept for
        # ops-dashboard / training provenance.
        handled_by = "pipeline"

        # PR gate: anything that ISN'T an explicit scope_violation is
        # eligible. `commit_push_open_pr` itself short-circuits on a
        # clean tree, so verdict=fail with no edits stays a no-op.
        # Enhancer-blocked tickets are excluded outright — never open a PR
        # built from a Doer acting on a too-vague/garbage brief.
        pr_meta: dict[str, Any] = {}
        if outcome != "scope_violation" and not _enhancer_blocked:
            pr_meta = commit_push_open_pr(ticket)

        # Empty-production-diff: git_pr rejected because the Doer only
        # wrote tests / fixtures with no edit to ``src/main``. Demote
        # the verdict so the ticket lands as ``blocked`` (not ``done``)
        # and the operator sees a clear reason instead of an empty PR.
        if pr_meta.get("pr_skip_reason") == "test_only_diff":
            outcome = "fail"
            new_status = "blocked"
            log.warning(
                "ticket=%s test_only_diff — demoting verdict to fail. "
                "Doer wrote: %s",
                ticket.identifier,
                ", ".join(pr_meta.get("test_only_files", [])[:5]),
            )

        # Empty-diff false pass (root cause of "done but nothing changed"):
        # the Doer changed NO files (clean tree → pr_skip_reason='no_changes',
        # no PR) yet the verdict came back non-fail. It narrated an edit it
        # never wrote — feedback/validator trusted the prose, not ground truth
        # (git diff). A pass with zero file changes is never a real pass.
        # Demote to blocked so the operator sees the ticket was NOT done.
        # (ONE-163/164: both "done", both empty, no commit.) Escape hatch:
        # AIFORGE_ALLOW_EMPTY_PASS=1 keeps the old trust-the-narration path.
        if (pr_meta.get("pr_skip_reason") == "no_changes"
                and not pr_meta.get("pr_url")
                and outcome not in ("scope_violation", "fail")
                and os.environ.get("AIFORGE_ALLOW_EMPTY_PASS", "0")
                    not in ("1", "true")):
            log.warning(
                "ticket=%s verdict=%s but clean tree (no_changes) — Doer wrote "
                "nothing; demoting to blocked (false pass on empty diff).",
                ticket.identifier, outcome)
            outcome = "fail"
            new_status = "blocked"
            reason = ("empty diff: Doer reported success but changed no files "
                      "(no edit reached the worktree). Not actually done.")
            # NB: the verdict_attempt row was already recorded above; don't
            # re-record (the sibling test_only_diff demotion doesn't either).
            # new_status/outcome drive the final ticket state.

        # Committed-but-partial: the Doer plateaued / hit its budget but DID
        # land a reviewable diff (PR opened). Route to in_review so a human
        # reviews the partial PR, rather than blocked. This is the terminal
        # for the plateau/replan cap (see graph_pipeline._validator_gate):
        # finished-but-imperfect work stops churning and waits at the gate.
        # With no PR there is nothing to review, so partial stays blocked
        # (the _VERDICT_TO_STATUS default).
        if outcome == "partial" and pr_meta.get("pr_url"):
            new_status = "in_review"
            log.info(
                "ticket=%s partial+PR → in_review (plateau cap; no replan of "
                "finished work)", ticket.identifier,
            )

        # Live verifier — runs HERE (post-PR) so its deploy recipe has a
        # real PR_URL to merge + roll out before testing. Only when the
        # PR actually opened and the verdict is otherwise a pass. A
        # failing live verify flips the ticket to blocked so the operator
        # knows the merged/worktree fix didn't actually hold.
        if (
            pr_meta.get("pr_url")
            and outcome == "pass"
            and os.environ.get("AIFORGE_LIVE_VERIFIER", "1") in {"1", "true"}
        ):
            try:
                lv = _run_live_verifier(ticket, pr_meta["pr_url"])
            except Exception as exc:  # noqa: BLE001
                log.warning("live_verifier standalone failed: %s", exc)
                lv = None
            if lv is not None and lv.get("ok") is False:
                outcome = "fail"
                new_status = "blocked"
                reason = f"live_verifier rejected: {(lv.get('rationale') or '')[:300]}"
                log.warning(
                    "ticket=%s live_verifier ok=false: %s",
                    ticket.identifier, (lv.get("rationale") or "")[:200],
                )
            elif lv is not None and lv.get("ok") is True and os.environ.get(
                "AIFORGE_AUTO_MERGE_ON_VALIDATE", "1"
            ) in {"1", "true"}:
                # live_verifier validated the behaviour → merge the PR.
                # For deploy_target=qa/prod the deploy recipe already
                # merged it, so merge_pr reports already_merged — still
                # surfaced so the operator sees the final state.
                try:
                    from ..git_pr import merge_pr
                    merge_meta = merge_pr(pr_meta["pr_url"])
                    pr_meta["pr_merged"] = merge_meta.get("merged")
                    pr_meta["pr_merge_reason"] = merge_meta.get("reason")
                    log.info(
                        "ticket=%s auto-merge merged=%s reason=%s",
                        ticket.identifier, merge_meta.get("merged"),
                        merge_meta.get("reason"),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("auto-merge failed: %s", exc)

        # C1: grade the PR's CI runs once the push is in. Empty PR
        # metadata (no diff to ship) skips this. Soft-fail: any
        # ``gh`` error lands in pr_meta as ``ci_*`` keys for an
        # operator to inspect, never blocks status update.
        ci_meta: dict[str, Any] = {}
        if pr_meta.get("pr_url") and os.environ.get(
            "AIFORGE_CI_GRADE", "1"
        ) in {"1", "true"}:
            try:
                from aiforge_core.runtime.ci_feedback import grade_and_react
                ci_meta = grade_and_react(
                    pr_meta["pr_url"],
                    poll_seconds=int(
                        os.environ.get("AIFORGE_CI_POLL_S", "30"),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                ci_meta = {"ok": False, "error": str(exc)[:200]}

        # C2: second-agent PR review pass. Posts a structured verdict
        # comment so a human (or follow-up agent) can react. Soft-fail.
        review_meta: dict[str, Any] = {}
        if pr_meta.get("pr_url"):
            try:
                from aiforge_core.runtime.pr_reviewer import review_pr
                review_meta = review_pr(
                    pr_meta["pr_url"],
                    ticket.title or "",
                    ticket.body or "",
                )
            except Exception as exc:  # noqa: BLE001
                review_meta = {"ok": False, "error": str(exc)[:200]}

        tickets_mod.update_status(
            ticket.id, new_status, role="adk_runner",
            metadata_patch={
                "feedback_verdict": outcome,
                "verifier_verdict": _extract_verifier(state),
                **pr_meta,
                **({"ci_status": ci_meta.get("status"),
                    "ci_rolled_back": ci_meta.get("rolled_back", False),
                    "ci_checks": ci_meta.get("checks") or []}
                   if ci_meta else {}),
                **({"review_verdict": review_meta.get("verdict"),
                    "review_axes": review_meta.get("axes") or {}}
                   if review_meta and review_meta.get("ok") else {}),
                **({"validator_verdict": (validator_out or {}).get("verdict"),
                    "validator_rationale": (validator_out or {}).get("rationale"),
                    "validator_scope_ok": (validator_out or {}).get("scope_ok"),
                    "validator_regression_risk":
                        (validator_out or {}).get("regression_risk")}
                   if validator_out else {}),
                # Provenance: which path finished the ticket. "pipeline" =
                # the configured-model pipeline (+ retry chain) cleared it.
                "handled_by": handled_by,
                **({
                    "live_verifier_ok": (lv or {}).get("ok"),
                    "live_verifier_rationale": (lv or {}).get("rationale"),
                    "live_handoff": (lv or {}).get("live_handoff", False),
                    "handoff_brief": (lv or {}).get("handoff_brief"),
                } if lv else {}),
            },
        )
        log.info("ticket=%s status=%s verdict=%s",
                 ticket.identifier, new_status, outcome)

    except Exception as exc:
        # The concrete cause is already surfaced concisely upstream
        # (EscalatingLlm's ``llm.exhausted`` / ``llm.attempt_failed`` lines).
        # A full chained traceback here is redundant noise for the common
        # case (flaky/down model). Log a meaningful one-liner; restore the
        # raw stack with AIFORGE_ADK_TRACEBACKS=1 for novel failures.
        if str(os.environ.get("AIFORGE_ADK_TRACEBACKS", "")).strip().lower() in (
                "1", "true", "yes", "on"):
            log.exception("ticket=%s failed during ADK run: %s",
                          ticket.identifier, exc)
        else:
            log.error("ticket=%s failed during ADK run: %s: %s",
                      ticket.identifier, type(exc).__name__, str(exc)[:400])
        # Even on ADK failure, the Doer may have written real files
        # before the orchestrator stalled. Surface that work as a draft PR for
        # human review instead of dropping it. The function
        # short-circuits with pr_skip_reason=no_changes on a clean tree.
        rescue_meta: dict[str, Any] = {}
        try:
            rescue_meta = commit_push_open_pr(ticket)
            if rescue_meta.get("pr_url"):
                log.info(
                    "ticket=%s rescued partial work as PR despite "
                    "ADK failure: %s", ticket.identifier,
                    rescue_meta["pr_url"],
                )
        except Exception as rescue_exc:
            log.warning("ticket=%s PR rescue also failed: %s",
                        ticket.identifier, rescue_exc)
        try:
            tickets_mod.update_status(
                ticket.id, "blocked", role="adk_runner",
                metadata_patch={"error": str(exc)[:500], **rescue_meta},
            )
        except Exception:
            pass
    finally:
        # Always clear the per-ticket override so the next claim builds
        # against the operator's profile, not the previous ticket's
        # forced provider.
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
