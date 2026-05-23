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

from . import memory_block
from .git_pr import commit_push_open_pr
from .pipeline import build_pipeline, set_force_provider
from .researcher_routing import should_skip_researcher


def _setup_ticket_workspace(ticket) -> tuple[str | None, str | None]:
    """Resolve per-ticket worktree and pin ``AIFORGE_REPO_ROOT`` to it.

    The Doer's sandboxed file tools (:mod:`sandbox.root`) and
    :mod:`git_pr` both read ``AIFORGE_REPO_ROOT`` to choose the working
    directory. Without this hook every ticket lands on whatever the
    operator's systemd EnvironmentFile pinned — usually a stale repo.

    Returns ``(worktree_path, prior_env)``. Caller MUST pass
    ``prior_env`` back to :func:`_restore_env` in a finally block.
    """
    from aiforge_core.runtime.workspace import ensure_branch_and_worktree

    prior = os.environ.get("AIFORGE_REPO_ROOT")
    worktree = ensure_branch_and_worktree(ticket)
    if worktree:
        os.environ["AIFORGE_REPO_ROOT"] = worktree
        log.info("ticket=%s workspace=%s", ticket.identifier, worktree)
    else:
        log.warning(
            "ticket=%s no worktree (project=%r) — falling back to env",
            ticket.identifier, ticket.project,
        )
    return worktree, prior


def _restore_env(prior: str | None) -> None:
    if prior is None:
        os.environ.pop("AIFORGE_REPO_ROOT", None)
    else:
        os.environ["AIFORGE_REPO_ROOT"] = prior


def _persist_ticket_media(ticket) -> None:
    """Gap-10 wire-in: stash image attachments as an AFM
    ``Observation_v2`` with ``media_refs`` populated.

    Vision sub #6 attaches images for the run, but the bytes vanished
    once the ADK session torn down. Capturing the paths here gives a
    durable record so future tickets can recall "we saw screenshot X
    last time" via the same memory_block path the Doer already reads.

    Soft-fail — never raises into the ticket loop. ``AIFORGE_VISION_PERSIST=0``
    opts out.
    """
    if os.environ.get("AIFORGE_VISION_PERSIST", "1") in ("0", "false", ""):
        return
    md = ticket.metadata or {}
    files = md.get("attached_files") or []
    media_paths = [
        str(f.get("path", "")) for f in files
        if isinstance(f, dict)
        and str(f.get("name", "")).lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp"),
        )
    ]
    media_paths = [p for p in media_paths if p]
    if not media_paths:
        return
    if not ticket.project:
        return
    try:
        from neo4j import GraphDatabase

        from aiforge_memory.features.memory.store import upsert_observation
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
        log.debug("vision persist driver fail: %s", exc)
        return
    try:
        try:
            from aiforge_core.tickets.store import get as ticket_get
            t = ticket_get(ticket.identifier)
            created_at = getattr(t, "created_at", None)
        except Exception:
            created_at = None
        event_time = None
        if created_at is not None:
            try:
                event_time = created_at.timestamp()
            except Exception:
                event_time = None
        upsert_observation(
            drv, repo=ticket.project,
            text=(
                f"Ticket {ticket.identifier} included "
                f"{len(media_paths)} image attachment(s): "
                + ", ".join(p.rsplit("/", 1)[-1] for p in media_paths)
            ),
            kind="attachment",
            author="adk_runner",
            tags=[f"ticket:{ticket.identifier}", "kind:vision"],
            media_refs=media_paths,
            event_time=event_time,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("vision persist failed: %s", exc)
    finally:
        try:
            drv.close()
        except Exception:
            pass


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
    strategy = os.environ.get("AIFORGE_CONDENSER_STRATEGY", "").strip()
    custom = None
    if strategy:
        # Sub #4: optional aggressive condenser layered over the
        # invocation-keep trim. ``amortized`` compresses oldest half
        # into one synthetic block; ``recent`` is keep-tail only.
        from aiforge_core.runtime.condensers import condense

        def _adk_custom_filter(contents):
            events = [
                {"type": "content", "role": getattr(c, "role", ""),
                 "text": " ".join(
                     getattr(p, "text", "") for p in getattr(c, "parts", [])
                     if getattr(p, "text", None)
                 )}
                for c in contents
            ]
            condensed = condense(events, strategy)
            # ADK custom_filter must return list[Content]; map back by
            # keeping the original Content objects when index survives,
            # otherwise drop (amortized prepends one synthetic block we
            # let through as-is via a fresh Content).
            from google.genai import types as gtypes
            # Heuristic: align tail-N of condensed to tail-N of contents.
            keep_n = len(condensed) - (1 if condensed and condensed[0].get(
                "role") == "condenser" else 0)
            tail = contents[-keep_n:] if keep_n > 0 else []
            if condensed and condensed[0].get("role") == "condenser":
                summary_part = gtypes.Part.from_text(
                    text=condensed[0]["text"],
                )
                summary = gtypes.Content(role="user", parts=[summary_part])
                return [summary] + list(tail)
            return list(tail)

        custom = _adk_custom_filter
        log.info("context_filter: enabled keep=%d invocations + "
                 "condenser=%s", keep, strategy)
    else:
        log.info("context_filter: enabled keep=%d invocations", keep)
    return [ContextFilterPlugin(
        num_invocations_to_keep=keep, custom_filter=custom,
    )]


async def _run_pipeline(prompt: str, *, skip_researcher: bool = False,
                        ticket=None) -> dict:
    """Drive one ADK pipeline run and return the final session state.

    ``skip_researcher`` lets the caller drop the Researcher step for
    greenfield tickets (see :mod:`researcher_routing`). Passed through
    to :func:`build_pipeline` so the SequentialAgent skips assembling
    that LlmAgent — saves ~5 LM calls and ~4 minutes wall-clock when
    the Researcher would have found nothing relevant.

    ``ticket`` (optional) seeds session.state with identifier + project
    so the Learner's after-callback can write Observation_v2 nodes
    keyed back to the ticket. None = test path.
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
    initial_state: dict[str, Any] = {}
    if ticket is not None:
        initial_state["ticket_identifier"] = getattr(ticket, "identifier", "") or ""
        initial_state["ticket_project"] = getattr(ticket, "project", "") or ""
    session = await session_svc.create_session(
        app_name="aiforge", user_id="aiforge-runner",
        state=initial_state or None,
    )
    content = gtypes.Content(
        role="user", parts=[gtypes.Part.from_text(text=prompt)],
    )
    # Sub #6 follow-up: inject multimodal image parts when ticket has
    # image attachments AND the Doer model supports vision.
    if ticket is not None:
        try:
            from aiforge_core.config.agent_config import get_config
            from aiforge_core.runtime.vision_adk import inject_image_parts

            cfg = get_config()
            doer_model = (cfg.get("doer", {}) or {}).get("model", "")
            md = ticket.metadata or {}
            images = [
                str(f.get("path", "")) for f in (md.get("attached_files") or [])
                if isinstance(f, dict)
                and str(f.get("name", "")).lower().endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp"),
                )
                and f.get("path")
            ]
            if images:
                injected = inject_image_parts([content], doer_model, images)
                if injected and injected[0] is not content:
                    content = injected[0]
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.debug("vision_adk.inject failed: %s", exc)
    try:
        async for event in runner.run_async(
            user_id="aiforge-runner",
            session_id=session.id, new_message=content,
        ):
            if event.is_final_response():
                pass  # session.state mutated; drained for completeness
        session = await session_svc.get_session(
            app_name="aiforge", user_id="aiforge-runner",
            session_id=session.id,
        )
        return dict(session.state or {})
    finally:
        # Best-effort tmux session cleanup for the Doer's persistent bash
        # (sub-project #1, see runtime/tools/bash.py). Failure is swallowed
        # so the runner still returns even when tmux isn't installed.
        try:
            from aiforge_core.runtime.tools.bash import destroy_session
            destroy_session(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("bash.destroy_session failed: %s", exc)
        try:
            from aiforge_core.runtime.tools.browser import (
                destroy_context as destroy_browser,
            )
            destroy_browser(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("browser.destroy_context failed: %s", exc)
        try:
            from aiforge_core.runtime.tools.ipython_kernel import (
                destroy_kernel,
            )
            destroy_kernel(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("ipython.destroy_kernel failed: %s", exc)
        try:
            from aiforge_core.runtime.docker_sandbox import (
                destroy_container,
            )
            destroy_container(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("docker_sandbox.destroy_container failed: %s", exc)
        # Sub #15: dump session trajectory for replay-style debugging.
        # Gap-11 (2026-05-23): also index a one-line-per-event summary
        # into AFM as a queryable ``Note_v2`` so future tickets can
        # rerank "have we run something like this before?" against past
        # runs without re-reading raw JSON.
        if os.environ.get("AIFORGE_TRAJECTORY_DUMP", "1") in ("1", "true"):
            try:
                from aiforge_core.runtime.trajectory import (
                    dump_trajectory, index_trajectory_to_memory,
                )
                ticket_id = (initial_state.get("ticket_identifier")
                             if initial_state else None) or "unknown"
                events = list(getattr(session, "events", []) or [])
                dump_out = dump_trajectory(
                    ticket_id, session.id,
                    events, dict(session.state or {}),
                )
                if dump_out.get("ok") and ticket is not None and ticket.project:
                    idx = index_trajectory_to_memory(
                        trajectory_path=dump_out["path"],
                        repo=ticket.project,
                        ticket_identifier=ticket_id,
                    )
                    if not idx.get("ok"):
                        log.debug(
                            "trajectory.index_skipped: %s",
                            idx.get("error", "unknown"),
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.debug("trajectory.dump_failed: %s", exc)


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
    if memory_md:
        out += "\n" + memory_md

    # Microagent injection (sub #5). Match against ticket title + body and
    # prepend matched playbooks. Best-effort: parse failures swallowed.
    try:
        from aiforge_core.runtime.microagents import (
            load_microagents, match, render_injection,
        )
        agents = load_microagents()
        hay = f"{ticket.title or ''} {ticket.body or ''}"
        matched = match(hay, agents)
        if matched:
            out = render_injection(matched) + "\n\n" + out
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("microagents.inject failed: %s", exc)

    # Vision attach hint (sub #6). When the ticket has image attachments
    # AND the active Doer model supports vision, list them with a flag so
    # the model knows it can request a multimodal turn. Actual content-
    # block conversion lives in vision.attach_image; wiring it through
    # ADK's LlmRequest.contents shape is a follow-up.
    try:
        from aiforge_core.runtime.vision import supports_vision
        from aiforge_core.config.agent_config import get_config
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
        from neo4j import GraphDatabase

        from aiforge_memory.features.external_ingest import (
            ingest_external_source,
        )
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

    # Gap-10 wire-in: persist any image attachments as an
    # Observation_v2 with ``media_refs`` so future tickets can recall
    # "this ticket had screenshots X / Y" — even before the vision
    # embedder lands. Soft-fails on any backend error.
    _persist_ticket_media(ticket)

    # C5: spec → failing-test scaffold. Parses ticket body's
    # "Acceptance" bullets and writes a per-language test file under
    # ``tests/aiforge_spec/``. Doer's run_tests then has a TDD target.
    if os.environ.get("AIFORGE_SPEC_TO_TESTS", "1") in {"1", "true"}:
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

    try:
        state = asyncio.run(_run_pipeline(
            prompt, skip_researcher=skip_researcher, ticket=ticket,
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
        _restore_env(prior_env)
    return True


def main() -> int:
    """Single-shot: claim one ticket, run it, exit."""
    if _process_one_ticket():
        return 0
    time.sleep(int(os.environ.get("AIFORGE_POLL_IDLE_S", "10")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
