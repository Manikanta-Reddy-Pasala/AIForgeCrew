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
from .pipeline import build_pipeline


log = logging.getLogger("adk_runner")
logging.basicConfig(
    level=os.environ.get("AIFORGE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# Map Feedback verdict → tickets-store status. ``fail`` and any
# unrecognised value land in ``blocked`` so a human can triage.
_VERDICT_TO_STATUS: dict[str, str] = {
    "pass": "done",
    "scope_violation": "cancelled",
}


def _extract_verdict(state: dict) -> str:
    """Pull ``feedback_verdict.verdict`` out of pipeline state.

    The Feedback agent is asked for STRICT JSON but local models
    occasionally wrap the value in extra prose; tolerate both ``str``
    (parse + fallback) and ``dict`` shapes.
    """
    verdict = state.get("feedback_verdict") or {}
    if isinstance(verdict, str):
        try:
            verdict = json.loads(verdict)
        except json.JSONDecodeError:
            verdict = {}
    if not isinstance(verdict, dict):
        return "fail"
    return verdict.get("verdict", "fail")


def _extract_verifier(state: dict) -> str | None:
    """Grab the verifier verdict (``pass``/``reject``) when present."""
    raw = state.get("verifier_verdict")
    if isinstance(raw, dict):
        return raw.get("verdict")
    return None


async def _run_pipeline(prompt: str) -> dict:
    """Drive one ADK pipeline run and return the final session state."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gtypes

    pipeline = build_pipeline()
    session_svc = InMemorySessionService()
    runner = Runner(
        agent=pipeline, app_name="aiforge",
        session_service=session_svc, auto_create_session=True,
    )
    session = await session_svc.create_session(
        app_name="aiforge", user_id="aiforge-runner",
    )
    content = gtypes.Content(
        role="user", parts=[gtypes.Part.from_text(text=prompt)],
    )
    async for event in runner.run_async(
        user_id="aiforge-runner",
        session_id=session.id, new_message=content,
    ):
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
    if memory_md:
        out += "\n" + memory_md
    return out


def _process_one_ticket() -> bool:
    """Claim + run one ticket. Returns True when one ran, False on
    empty queue (caller exits + lets systemd back off)."""
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        return False

    log.info("claimed ticket=%s title=%r", ticket.identifier, ticket.title)
    memory_md = memory_block.fetch(ticket)
    prompt = _build_prompt(ticket, memory_md)

    try:
        state = asyncio.run(_run_pipeline(prompt))
        outcome = _extract_verdict(state)
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
    return True


def main() -> int:
    """Single-shot: claim one ticket, run it, exit."""
    if _process_one_ticket():
        return 0
    time.sleep(int(os.environ.get("AIFORGE_POLL_IDLE_S", "10")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
