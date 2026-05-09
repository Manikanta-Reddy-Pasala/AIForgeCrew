"""Production ticket processor — single-shot ADK pipeline driver.

Polls Postgres for a ``todo`` ticket via
:func:`aiforge_core.tickets.store.claim_next_any`, runs one pass of the
v6 SequentialAgent (see :mod:`pipeline`), then exits. systemd
``Restart=always RestartSec=10`` keeps the loop polling.

Heavy lifting lives in sibling modules so this file stays a thin
orchestrator:

* :mod:`pipeline`     — agent factory + EscalatingLlm wiring
* :mod:`memory_block` — pre-flight AiForgeMemory recall
* :mod:`git_pr`       — auto-commit + push + open-PR helper
* :mod:`prompts`      — per-archetype instruction strings
* :mod:`doer_tools`   — file_read / file_write / run_shell / …

Invoke::

    python -m aiforge_core.runtime.adk_runner
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from aiforge_core.tickets import store as tickets_mod

from . import memory_block, structural_plan
from .git_pr import commit_push_open_pr
from .pipeline import build_pipeline, set_force_provider
from .researcher_routing import should_skip_researcher


log = logging.getLogger("adk_runner")
logging.basicConfig(
    level=os.environ.get("AIFORGE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# Map Feedback verdict → tickets-store status. ``fail`` and any
# unrecognised value land in ``blocked`` so a human can triage.
# ``partial`` comes from the loop-budget kill switch (see
# :mod:`loop_budget`) — partial work still gets PR-shipped for human
# review, so the status maps to ``blocked`` to flag triage need.
_VERDICT_TO_STATUS: dict[str, str] = {
    "pass": "done",
    "scope_violation": "cancelled",
    "partial": "blocked",
}


# Order matters: ``scope_violation`` is checked before ``fail`` (string
# substring overlap) and ``partial`` is checked before ``pass`` for
# the same reason — neither is a strict substring of the other today
# but the rule keeps future-proofing cheap.
_VERDICT_TOKENS: tuple[str, ...] = (
    "scope_violation", "partial", "pass", "fail",
)

# Cap rationales persisted to ticket_events so a chatty model can't bloat
# the audit trail with a multi-paragraph rant. 300 chars matches the spec
# in the operator-observability ticket.
_REASON_MAX_CHARS = 300
_REASON_DEFAULT_PASS = "no rationale provided"
_REASON_DEFAULT_FAIL = "no rationale provided"


def _extract_reason(state: dict, verdict: str) -> str:
    """Pull the post-verdict rationale line out of the Feedback output.

    The Feedback prompt asks the model to put the verdict token on
    line 1 and a short rationale on line 2+. This function returns
    that rationale flattened to a single line, trimmed to
    :data:`_REASON_MAX_CHARS` so the audit trail can't be bloated.

    Tolerated shapes mirror :func:`_extract_verdict`:
      1. raw string ``"<token>\\n<reason>..."`` → returns reason
      2. dict ``{"verdict": ..., "rationale": "..."}`` → returns rationale
      3. anything else / no rationale → role-appropriate default

    The verdict token itself is stripped from the head so we don't
    double-print it (``"pass: pass — looks good"`` → ``"pass: looks good"``).
    """
    raw = state.get("feedback_verdict")
    text: str | None = None
    if isinstance(raw, dict):
        # Legacy JSON dict — both ``rationale`` and ``reason`` seen in
        # the wild; ``reason`` is the new canonical key (matches the
        # ticket_events column the operators query).
        text = raw.get("rationale") or raw.get("reason")
    elif isinstance(raw, str) and raw.strip():
        s = raw.strip()
        # Legacy JSON string — try once, fall through to plain text on
        # parse fail rather than 500ing.
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                text = obj.get("rationale") or obj.get("reason")
        if text is None:
            # Plain leading-token format: drop line 1, take the rest.
            head = s.lstrip("`*_-> ")
            for token in _VERDICT_TOKENS:
                if head.lower().startswith(token):
                    head = head[len(token):]
                    break
            # Anything after the verdict token is the rationale; flatten
            # newlines and tabs so the audit row is single-line.
            text = head.strip(" :—-\n\t").replace("\n", " ").replace("\t", " ")

    if not text:
        return _REASON_DEFAULT_PASS if verdict == "pass" else _REASON_DEFAULT_FAIL

    # Collapse runs of whitespace so the audit body is compact.
    text = " ".join(text.split())
    if len(text) > _REASON_MAX_CHARS:
        text = text[: _REASON_MAX_CHARS - 1].rstrip() + "…"
    return text


def _record_verdict_event(ticket_id: int, verdict: str, reason: str) -> None:
    """Persist a ``verdict_attempt`` row in ``ticket_events``.

    Called once per ticket after the SequentialAgent run resolves its
    final session state. The row schema is:

      kind        = 'verdict_attempt'
      agent_role  = 'feedback'
      body        = '<verdict>: <reason>'
      metadata    = {'verdict': ..., 'reason': ...}

    Operators query this row to see WHY a Doer-Feedback loop
    converged (or didn't) — the prior ``status_change`` rows only
    captured the eventual ticket status, not the reasoning.

    Failures are swallowed: the audit trail is best-effort, we never
    want a Postgres hiccup to block the runner from finalising the
    ticket status. The exception is logged so an operator can spot a
    persistent DB-grant problem.
    """
    body = f"{verdict}: {reason}"
    try:
        tickets_mod.add_event(
            ticket_id, "feedback", "verdict_attempt", body,
            {"verdict": verdict, "reason": reason},
        )
    except Exception as exc:  # pragma: no cover — best-effort audit
        log.warning("ticket_id=%s failed to persist verdict_attempt: %s",
                    ticket_id, exc)


def _extract_verdict(state: dict) -> str:
    """Pull the Feedback verdict out of pipeline state.

    The new Feedback prompt asks for a leading token (``pass`` /
    ``fail`` / ``scope_violation``) followed by an optional rationale
    line — much more robust than strict JSON for local Claude /
    qwen which routinely wrap responses in prose.

    Tolerated shapes (in order):
      1. raw string starting with one of the tokens
      2. JSON-with-``verdict``-key (legacy — still emitted by some
         models)
      3. anything else → ``fail``

    ``scope_violation`` is checked before ``fail`` because the literal
    string contains ``fail`` as a substring; without the order rule a
    model that emits ``scope_violation`` would be parsed as ``fail``.
    """
    raw = state.get("feedback_verdict")
    if isinstance(raw, dict):
        return str(raw.get("verdict", "fail")).lower()
    if not isinstance(raw, str):
        return "fail"

    text = raw.strip()
    if not text:
        return "fail"

    # Legacy JSON path — kept for tickets ran on older prompt revisions.
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("verdict"):
            return str(obj["verdict"]).lower()

    # New prompt path — leading token decides. Strip any markdown-y
    # wrapping the model might add despite the rules.
    head = text.lstrip("`*_-> ").lower()
    for token in _VERDICT_TOKENS:
        if head.startswith(token):
            return token
    return "fail"


def _extract_verifier(state: dict) -> str | None:
    """Grab the verifier verdict (``pass``/``reject``) when present."""
    raw = state.get("verifier_verdict")
    if isinstance(raw, dict):
        return raw.get("verdict")
    return None


def _build_context_plugins() -> list:
    """Wire ADK's ``ContextFilterPlugin`` so long-running Doer loops
    don't blow past the LM's context window.

    Without this, ADK accumulates every tool result + LLM response in
    session.events and replays the lot on every turn. ONE-117 hit
    MLX GPU OOM (Metal command buffer abort → SIGABRT) after ~140
    LiteLLM calls because the prompt approached 131K tokens and the
    KV cache + 4-way parallel batches exceeded 96GB unified memory.

    ``num_invocations_to_keep`` keeps the last N invocations verbatim
    and drops older ones. An "invocation" = one user turn + the model
    turns it triggered (tool calls, retries). Default 12 is a balance:
    the Doer can still see its last 5-10 file_reads + edits while the
    model summary / oldest tool noise gets evicted.

    Env knobs:
      AIFORGE_CONTEXT_KEEP_INVOCATIONS=12  → invocations to retain
      AIFORGE_CONTEXT_FILTER_DISABLE=1     → opt out (debug only)
    """
    if os.environ.get("AIFORGE_CONTEXT_FILTER_DISABLE", "0") in ("1", "true"):
        return []
    try:
        from google.adk.plugins.context_filter_plugin import ContextFilterPlugin
    except ImportError:
        log.warning("context_filter: ContextFilterPlugin not available — "
                    "ADK older than 2.0b? skipping")
        return []
    keep = int(os.environ.get("AIFORGE_CONTEXT_KEEP_INVOCATIONS", "12"))
    log.info("context_filter: enabled keep=%d invocations", keep)
    return [ContextFilterPlugin(num_invocations_to_keep=keep)]


def _build_run_config():
    """Return an ADK ``RunConfig`` with our LLM-call ceiling.

    ADK's default ``RunConfig.max_llm_calls=500`` is too tight for
    mega-tickets — ONE-1 (audit subsystem, 5K LOC scaffold) hit it
    after ~4 hours when the Doer kept calling tools to chase ``mvn
    test`` failures. We raise to 1500 by default so the soft
    LM-call budget in :mod:`loop_budget` (default 400) is the actual
    limiter — that path commits the work as ``verdict=partial``
    instead of crashing with an unhandled exception.

    Override via ``AIFORGE_ADK_MAX_LLM_CALLS``. Returns ``None`` when
    the ADK ``RunConfig`` isn't importable (very old ADK) so
    ``run_async`` falls back to the framework default.
    """
    try:
        from google.adk.agents.run_config import RunConfig
    except ImportError:
        log.warning("adk_run_config: RunConfig not importable — "
                    "leaving max_llm_calls at framework default")
        return None
    raw = os.environ.get("AIFORGE_ADK_MAX_LLM_CALLS", "1500")
    try:
        cap = int(raw)
    except ValueError:
        log.warning("adk_run_config: bad AIFORGE_ADK_MAX_LLM_CALLS=%r — "
                    "using 1500", raw)
        cap = 1500
    if cap <= 0:
        log.warning("adk_run_config: AIFORGE_ADK_MAX_LLM_CALLS<=0 disables "
                    "the cap; using 1500 instead")
        cap = 1500
    log.info("adk_run_config: max_llm_calls=%d (loop_budget soft cap = %s)",
             cap, os.environ.get("AIFORGE_LOOP_LLM_CALL_BUDGET", "400"))
    return RunConfig(max_llm_calls=cap)


async def _run_pipeline(
    prompt: str,
    *,
    skip_researcher: bool = False,
    seed_state: dict | None = None,
) -> dict:
    """Drive one ADK pipeline run and return the final session state.

    ``skip_researcher`` lets the caller drop the Researcher step for
    greenfield tickets (see :mod:`researcher_routing`). Passed through
    to :func:`build_pipeline` so the SequentialAgent skips assembling
    that LlmAgent — saves ~5 LM calls and ~4 minutes wall-clock when
    the Researcher would have found nothing relevant.

    ``seed_state`` (PR #27): pre-populate ADK session state with
    operator-supplied context. Currently used to inject
    ``structural_plan`` (heuristic file tree from ticket body, see
    :mod:`structural_plan`) so the Doer can look up canonical paths
    via ``state['structural_plan']``.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gtypes

    pipeline = build_pipeline(skip_researcher=skip_researcher)
    session_svc = InMemorySessionService()
    plugins = _build_context_plugins()
    runner = Runner(
        agent=pipeline, app_name="aiforge",
        session_service=session_svc, auto_create_session=True,
        plugins=plugins,
    )
    create_kwargs: dict = {
        "app_name": "aiforge", "user_id": "aiforge-runner",
    }
    if seed_state:
        create_kwargs["state"] = dict(seed_state)
    session = await session_svc.create_session(**create_kwargs)
    content = gtypes.Content(
        role="user", parts=[gtypes.Part.from_text(text=prompt)],
    )
    # Raised LM-call ceiling — see :func:`_build_run_config`. Pass
    # only when non-None so old ADK versions still drive the runner
    # via the kwarg-default path.
    run_kwargs: dict = {
        "user_id": "aiforge-runner",
        "session_id": session.id,
        "new_message": content,
    }
    rc = _build_run_config()
    if rc is not None:
        run_kwargs["run_config"] = rc
    async for event in runner.run_async(**run_kwargs):
        if event.is_final_response():
            pass  # session.state already mutated; drained for completeness
    session = await session_svc.get_session(
        app_name="aiforge", user_id="aiforge-runner",
        session_id=session.id,
    )
    return dict(session.state or {})


def _build_prompt(ticket, memory_md: str) -> str:
    """Compose the seed prompt for the SequentialAgent."""
    out = (
        f"# Ticket {ticket.identifier}\n"
        f"## Title\n{ticket.title}\n\n"
        f"## Body\n{ticket.body or '(no body)'}\n"
    )
    # Ticket attachments — list paths so the Doer (always claude_local
    # when these are present, see _process_one_ticket) can `file_read`
    # them via its native CLI tools. Each entry is the
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
    # PR #27 issue #3: when the ticket body lists explicit source paths,
    # surface them to the Doer as a canonical file tree so it can't
    # drift into similar-looking but different package paths (the
    # ONE-1 ``feature/audit/`` vs ``audit/`` regression).
    plan = structural_plan.build_plan(ticket.body or "")
    if plan is not None:
        out += structural_plan.render_for_prompt(plan)
    if memory_md:
        out += "\n" + memory_md
    return out


def _ticket_force_provider(ticket) -> str | None:
    """Per-ticket pipeline override.

    Today the only path that flips this is "operator uploaded files
    with the ticket" → force ``claude_local`` because only the
    subscription CLI can read attached files via its native filesystem
    tools. Future use: ``provider`` field on the ticket itself.
    """
    md = ticket.metadata or {}
    forced = md.get("force_provider")
    if isinstance(forced, str) and forced:
        return forced
    return None


def _process_one_ticket() -> bool:
    """Claim + run one ticket. Returns True when one ran, False on
    empty queue (caller exits + lets systemd back off)."""
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        return False

    log.info("claimed ticket=%s title=%r", ticket.identifier, ticket.title)
    memory_md = memory_block.fetch(ticket)
    prompt = _build_prompt(ticket, memory_md)

    forced = _ticket_force_provider(ticket)
    set_force_provider(forced)
    if forced:
        log.info("ticket=%s force_provider=%s (attachments present)",
                 ticket.identifier, forced)

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

    # PR #27 issue #3: heuristic structural_plan from ticket body.
    # When the body lists explicit source paths (mega-tickets), seed
    # ADK session state so downstream agents can read
    # state['structural_plan'] for canonical-owner lookups instead of
    # guessing package paths. Returns None for short tickets — that
    # path skips seeding entirely (the Doer falls back to grep_repo).
    seed: dict = {}
    plan_obj = structural_plan.build_plan(ticket.body or "")
    if plan_obj is not None:
        seed["structural_plan"] = plan_obj
        log.info("ticket=%s structural_plan=heuristic paths=%d",
                 ticket.identifier, len(plan_obj.get("tree", [])))

    try:
        state = asyncio.run(_run_pipeline(
            prompt, skip_researcher=skip_researcher,
            seed_state=seed or None,
        ))
        outcome = _extract_verdict(state)
        # Capture the Feedback rationale BEFORE any mutation so an
        # operator scanning ticket_events sees both the verdict and
        # the convergence reason. Best-effort: any persistence error
        # is logged + swallowed inside the helper so the runner still
        # makes forward progress on the ticket itself.
        reason = _extract_reason(state, outcome)
        _record_verdict_event(ticket.id, outcome, reason)

        new_status = _VERDICT_TO_STATUS.get(outcome, "blocked")

        # PR gate: anything that ISN'T an explicit scope_violation is
        # eligible. `commit_push_open_pr` itself short-circuits on a
        # clean tree, so verdict=fail with no edits stays a no-op.
        pr_meta: dict[str, Any] = {}
        if outcome != "scope_violation":
            pr_meta = commit_push_open_pr(ticket)

        tickets_mod.update_status(
            ticket.id, new_status, role="adk_runner",
            metadata_patch={
                "feedback_verdict": outcome,
                "verifier_verdict": _extract_verifier(state),
                **pr_meta,
            },
        )
        log.info("ticket=%s status=%s verdict=%s",
                 ticket.identifier, new_status, outcome)

    except Exception as exc:
        log.exception("ticket=%s failed during ADK run: %s",
                      ticket.identifier, exc)
        # Even on ADK failure, the Doer (especially claude_local using
        # native CLI tools) may have written real files before the
        # orchestrator stalled. Surface that work as a draft PR for
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
    return True


def main() -> int:
    """Single-shot: claim one ticket, run it, exit."""
    if _process_one_ticket():
        return 0
    time.sleep(int(os.environ.get("AIFORGE_POLL_IDLE_S", "10")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
