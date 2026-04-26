"""Entry point for the Google ADK orchestrator.

Replaces ``aiforge_core.runtime.graph_runner`` (LangGraph). Single-shot
mode: claim one ticket, run the ADK SequentialAgent against it, map the
final session state to a ticket status, exit. systemd
``Restart=always RestartSec=10`` keeps it polling.

Invoke as:

    python -m aiforge_core.runtime.adk_runner

The same ``aiforge_core.runtime.tickets.claim_next_any`` logic that
graph_runner used drives ticket selection, so concurrent ADK runners
under different ``AIFORGE_TICK_INSTANCE`` ids are race-safe via
``FOR UPDATE SKIP LOCKED``.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.adk_workflow import (
    S_COMPILE_FAIL_COUNT,
    S_FAIL_COUNT,
    S_LAST_DOER_SUMMARY,
    S_TICKET_ID,
    S_VERDICT,
    S_WORKTREE,
    build_aiforge_workflow,
)
from aiforge_core.runtime.logging_setup import emit, get_logger


def _commit_and_push_worktree(
    worktree_path: str | None, ticket_identifier: str, log
) -> None:
    """Commit any uncommitted Doer edits on a pass verdict and push.

    The Doer's edit_block tool never commits — final verdict=pass means
    persist now or the worktree-cleaned step wipes the changes.
    """
    if not worktree_path or not os.path.isdir(worktree_path):
        return
    try:
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path, capture_output=True, text=True,
            timeout=15, check=False,
        )
        if not (status_proc.stdout or "").strip():
            emit(log, "adk_runner.commit_skipped",
                 reason="worktree clean", path=worktree_path)
            return
        subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree_path, capture_output=True, text=True,
            timeout=30, check=False,
        )
        msg = f"aiforge: {ticket_identifier} doer edits (verdict=pass)"
        commit_proc = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=worktree_path, capture_output=True, text=True,
            timeout=30, check=False,
        )
        if commit_proc.returncode != 0:
            emit(log, "adk_runner.commit_failed",
                 stderr=(commit_proc.stderr or "")[:300])
            return
        push_proc = subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=worktree_path, capture_output=True, text=True,
            timeout=60, check=False,
        )
        emit(log, "adk_runner.pushed",
             branch_pushed=push_proc.returncode == 0,
             push_err=(push_proc.stderr or "")[:200]
                     if push_proc.returncode else None)
    except Exception as exc:
        emit(log, "adk_runner.commit_exception", error=str(exc)[:200])


def _cleanup_worktree(worktree_path: str | None, log) -> None:
    if not worktree_path or not os.path.isdir(worktree_path):
        return
    parent_repo = worktree_path
    for _ in range(4):
        parent_repo = os.path.dirname(parent_repo)
        if os.path.isdir(os.path.join(parent_repo, ".git")):
            break
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=parent_repo, capture_output=True, text=True,
            timeout=30, check=False,
        )
        emit(log, "adk_runner.worktree_cleaned", path=worktree_path)
    except Exception as exc:
        emit(log, "adk_runner.worktree_cleanup_failed",
             path=worktree_path, err=str(exc)[:200])


async def _run_workflow_async(ticket: tickets_mod.Ticket, log) -> dict:
    """Drive one ADK invocation against the given ticket.

    Returns the final session state dict (verdict, worktree path, etc.).
    """
    bundle = build_aiforge_workflow(
        enable_neo4j_mirror=os.environ.get(
            "AIFORGE_ADK_NEO4J_MIRROR", "1"
        ) != "0",
    )
    runner = bundle.runner
    user_id = "aiforge-runner"
    session_id = f"{ticket.identifier}-{uuid.uuid4().hex[:8]}"

    # Pre-create the session with the ticket id baked into state so the
    # planner sees it on its first turn. ``DatabaseSessionService.create_session``
    # is async; ``InMemorySessionService`` exposes both sync and async forms.
    initial_state = {
        S_TICKET_ID: ticket.id,
        S_WORKTREE: None,
        S_VERDICT: None,
        S_FAIL_COUNT: 0,
        S_COMPILE_FAIL_COUNT: 0,
    }
    session = await runner.session_service.create_session(
        app_name="aiforge",
        user_id=user_id,
        session_id=session_id,
        state=initial_state,
    )

    emit(log, "adk_runner.session_created",
         ticket=ticket.identifier, session_id=session_id)

    # ADK requires either a `new_message` Content or a resume `invocation_id`
    # to start the run; we pass a one-line user message that names the ticket
    # so it shows up in the session transcript.
    from google.genai import types as genai_types
    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=f"Process ticket {ticket.identifier}")],
    )

    event_count = 0
    per_agent_events: dict[str, int] = {}
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=new_message,
    ):
        event_count += 1
        author = getattr(event, "author", "?") or "?"
        per_agent_events[author] = per_agent_events.get(author, 0) + 1
        if event.error_message:
            emit(log, "adk_runner.event.error",
                 author=author, error=event.error_message[:200])

    final = await runner.session_service.get_session(
        app_name="aiforge", user_id=user_id, session_id=session_id,
    )
    state = dict(final.state) if final else {}
    state["_event_count"] = event_count
    state["_per_agent_events"] = per_agent_events
    state["_session_id"] = session_id

    emit(log, "adk_runner.workflow_done",
         ticket=ticket.identifier, session_id=session_id,
         events=event_count, per_agent=per_agent_events,
         verdict=state.get(S_VERDICT))
    return state


def run_adk(ticket_id: int) -> int:
    """Run the ADK workflow for one ticket. Maps state to ticket status."""
    log = get_logger("adk_runner")
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        emit(log, "adk_runner.not_found", ticket_id=ticket_id)
        return 1

    t_start = time.time()
    emit(log, "adk_runner.start",
         ticket=ticket.identifier, title=ticket.title)

    try:
        final_state = asyncio.run(_run_workflow_async(ticket, log))
    except Exception as exc:
        emit(log, "adk_runner.exception",
             ticket=ticket.identifier, error=str(exc)[:500])
        tickets_mod.add_event(
            ticket_id, "adk_runner", "error",
            body=f"adk runner exception: {exc}",
        )
        return 2

    wall_s = round(time.time() - t_start, 2)
    verdict = final_state.get(S_VERDICT) or ""
    worktree = final_state.get(S_WORKTREE)
    fail_count = int(final_state.get(S_FAIL_COUNT, 0) or 0)

    # Map final ADK state → ticket status. If the doer ever asked the
    # operator a question via ask_user, escalate to in_review instead
    # of blocked so the ticket shows up on the human-attention board.
    awaiting_user = False
    try:
        with tickets_mod._conn() as _c:
            with _c.cursor() as _cur:
                _cur.execute(
                    "SELECT 1 FROM ticket_events "
                    "WHERE ticket_id=%s AND kind='doer_question' LIMIT 1",
                    (ticket_id,),
                )
                awaiting_user = _cur.fetchone() is not None
    except Exception:
        awaiting_user = False

    if verdict == "pass":
        new_status = "done"
    elif awaiting_user:
        new_status = "in_review"  # operator answers question on the ticket
    elif verdict in ("scope_violation", "fail"):
        new_status = "blocked"
    elif fail_count > 0:
        new_status = "blocked"
    else:
        new_status = "blocked"  # nothing definitive — kick to human review

    if new_status:
        tickets_mod.update_status(ticket_id, new_status)
        if new_status == "done":
            _commit_and_push_worktree(
                worktree, ticket.identifier, log,
            )
        if new_status in ("done", "blocked"):
            _cleanup_worktree(worktree, log)

    emit(log, "adk_runner.done",
         ticket=ticket.identifier,
         verdict=verdict,
         events=final_state.get("_event_count", 0),
         per_agent=final_state.get("_per_agent_events", {}),
         session_id=final_state.get("_session_id"),
         final_status=new_status,
         wall_s=wall_s)
    tickets_mod.add_event(
        ticket_id, "adk_runner", "comment",
        body=(f"adk run complete verdict={verdict} "
              f"events={final_state.get('_event_count', 0)} "
              f"wall_s={wall_s} status={new_status}"),
        metadata={
            "wall_s": wall_s,
            "final_status": new_status,
            "verdict": verdict,
            "session_id": final_state.get("_session_id"),
            "per_agent_events": final_state.get("_per_agent_events", {}),
            "via": "adk",
        },
    )
    return 0


def main() -> int:
    log = get_logger("adk_runner")
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        emit(log, "adk_runner.idle", note="no todo tickets")
        return 0
    return run_adk(ticket.id)


if __name__ == "__main__":
    sys.exit(main())
