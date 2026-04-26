"""IntentAgent — stage 0 of the ADK pipeline.

Runs BEFORE Planner. Translates the ticket's plain-language body into
a structured ``Intent`` + ``EnrichedTicket`` and persists the result
into ``ticket.metadata.enrichment`` so every downstream stage reads
the same bundle (no re-classify, no re-fanout).

Idempotent — if the ticket already carries a fresh enrichment, the
agent yields the cached summary instead of re-running. ``force=true``
in payload metadata bypasses the cache.

Skips silently when ``AIFORGE_INTENT_AGENT=0``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types as genai_types

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.logging_setup import emit, get_logger

# State keys + helpers shared with adk_workflow (avoid circular import
# by inlining the few constants we need; both modules write them).
S_TICKET_ID = "aiforge.ticket_id"
S_INTENT_DONE = "aiforge.intent_done"
S_ENRICHMENT = "aiforge.enrichment"


def _ticket_from_state(state: dict):
    tid = state.get(S_TICKET_ID)
    if tid is None:
        return None
    return tickets_mod.get(int(tid))


def _yield(author: str, text: str, invocation_id: str,
           extra_actions: EventActions | None = None) -> Event:
    return Event(
        invocation_id=invocation_id,
        author=author,
        content=genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=text[:4000])],
        ),
        actions=extra_actions or EventActions(),
    )


def _persist_enrichment(ticket_id: int, enrichment: dict) -> None:
    """Merge enrichment into ``tickets.metadata`` (Postgres jsonb)."""
    try:
        with tickets_mod._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET "
                "  metadata = COALESCE(metadata, '{}'::jsonb) "
                "             || jsonb_build_object('enrichment', %s::jsonb), "
                "  updated_at = NOW() "
                "WHERE id = %s",
                (json.dumps(enrichment), ticket_id),
            )
            conn.commit()
    except Exception:
        # Telemetry / cache layer — never block pipeline.
        pass


def _existing_enrichment(ticket: Any) -> dict | None:
    md = getattr(ticket, "metadata", None) or {}
    enr = md.get("enrichment") if isinstance(md, dict) else None
    if isinstance(enr, dict) and enr.get("intent"):
        return enr
    return None


class AiForgeIntentAgent(BaseAgent):
    """Stage 0. Plain-text ticket → structured EnrichedTicket."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        t_stage_start = time.time()
        state = ctx.session.state
        ticket = _ticket_from_state(state)
        log = get_logger("adk.intent")
        if ticket is None:
            yield _yield(self.name, "[intent] no ticket in state",
                         ctx.invocation_id)
            return

        # Kill-switch + force-refresh.
        if os.environ.get("AIFORGE_INTENT_AGENT", "1") != "1":
            yield _yield(self.name, "[intent] disabled by env",
                         ctx.invocation_id,
                         EventActions(state_delta={S_INTENT_DONE: True}))
            return

        force = bool((getattr(ticket, "metadata", {}) or {})
                     .get("force_intent_refresh"))
        existing = None if force else _existing_enrichment(ticket)
        if existing:
            emit(log, "adk.intent.cached",
                 ticket=ticket.identifier,
                 entity=existing.get("intent", {}).get("entity"))
            yield _yield(
                self.name,
                f"[intent] cached enrichment "
                f"(action={existing.get('intent', {}).get('action')}, "
                f"entity={existing.get('intent', {}).get('entity')}, "
                f"sources={', '.join(existing.get('sources_used') or [])})",
                ctx.invocation_id,
                EventActions(state_delta={
                    S_INTENT_DONE: True,
                    S_ENRICHMENT: existing,
                }),
            )
            return

        emit(log, "adk.intent.start", ticket=ticket.identifier)
        text = f"{ticket.title or ''}\n\n{ticket.body or ''}".strip()

        loop = asyncio.get_event_loop()
        try:
            from aiforge_core.intent import enrich
            et = await asyncio.wait_for(
                loop.run_in_executor(None, enrich, text),
                timeout=int(os.environ.get("AIFORGE_INTENT_TIMEOUT_S", "60")),
            )
        except asyncio.TimeoutError:
            emit(log, "adk.intent.timeout", ticket=ticket.identifier)
            yield _yield(self.name, "[intent] timeout — proceeding without enrichment",
                         ctx.invocation_id,
                         EventActions(state_delta={S_INTENT_DONE: True}))
            return
        except Exception as exc:
            emit(log, "adk.intent.exception",
                 ticket=ticket.identifier, error=str(exc)[:300])
            yield _yield(self.name, f"[intent] error: {exc}",
                         ctx.invocation_id,
                         EventActions(state_delta={S_INTENT_DONE: True}))
            return

        enrichment = {
            "intent": {
                "action": et.intent.action,
                "entity": et.intent.entity,
                "reference_pattern": et.intent.reference_pattern,
                "repo_hint": et.intent.repo_hint,
                "keywords": et.intent.keywords,
            },
            "focal_files": et.allowed_files[:12],
            "reference_files": et.reference_files[:6],
            "similar_tickets": et.similar_tickets[:5],
            "t3_recipes": et.t3_recipes[:6],
            "commands": et.commands,
            "acceptance": et.acceptance[:10],
            "repo": et.repo,
            "sources_used": et.sources_used,
            "errors": et.errors,
            "enriched_at": int(time.time()),
        }
        _persist_enrichment(ticket.id, enrichment)
        tickets_mod.add_event(
            ticket.id, "intent", "stage_done",
            body=(f"intent done in {round(time.time() - t_stage_start, 2)}s | "
                  f"action={et.intent.action} entity={et.intent.entity!r} "
                  f"ref={et.intent.reference_pattern!r} "
                  f"focal_files={len(et.allowed_files)} "
                  f"similar={len(et.similar_tickets)} "
                  f"t3={len(et.t3_recipes)} "
                  f"sources={','.join(et.sources_used)}"),
            metadata={
                "stage": "intent",
                "duration_s": round(time.time() - t_stage_start, 2),
                "action": et.intent.action,
                "entity": et.intent.entity,
                "sources_used": et.sources_used,
            },
        )
        emit(log, "adk.intent.done",
             ticket=ticket.identifier,
             action=et.intent.action,
             entity=et.intent.entity,
             sources=len(et.sources_used))

        summary_text = (
            f"[intent] action={et.intent.action} "
            f"entity={et.intent.entity!r} "
            f"reference={et.intent.reference_pattern!r} "
            f"repo={et.repo or '?'} "
            f"focal_files={len(et.allowed_files)} "
            f"similar_tickets={len(et.similar_tickets)} "
            f"t3_recipes={len(et.t3_recipes)} "
            f"sources={', '.join(et.sources_used)}"
        )
        yield _yield(
            self.name, summary_text, ctx.invocation_id,
            EventActions(state_delta={
                S_INTENT_DONE: True,
                S_ENRICHMENT: enrichment,
            }),
        )


__all__ = ["AiForgeIntentAgent", "S_INTENT_DONE", "S_ENRICHMENT"]
