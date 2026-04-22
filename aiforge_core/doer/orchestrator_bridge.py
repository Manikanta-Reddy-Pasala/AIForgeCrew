"""Bridge between the orchestrator and the smolagents Doer agent.

``run_smolagents_doer`` is called from ``orchestrator.py`` when
``AIFORGE_FLAG_DOER_BACKEND=smolagents`` is set.  It returns a dict in the
same shape as ``_run_tool_loop`` so ``_finalize_ticket`` works unchanged.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiforge_core.runtime import tickets
from aiforge_core.runtime.config import DOER_MODEL, LM_STUDIO_API_KEY, LM_STUDIO_BASE_URL
from aiforge_core.runtime.logging_setup import emit

from .agent import build_doer_agent
from .scope_guard import ScopeViolation

if TYPE_CHECKING:
    pass


class _LLMConfig:
    """Minimal config shim so we don't import the full RoleConfig."""

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key


def run_smolagents_doer(ticket: object, worktree_path: str, log: object) -> dict:
    """Run the smolagents ToolCallingAgent for one Doer tick.

    Args:
        ticket: Ticket dataclass from ``tickets.claim_next``.
        worktree_path: Absolute path to the git worktree.
        log: Structured logger from ``get_logger``.

    Returns:
        Dict with keys ``stop_reason``, ``has_commented``, ``turns``,
        ``wall_s``, ``summary`` — same shape ``_run_tool_loop`` returns
        (plus ``summary`` for the smolagents final_answer text).
    """
    t_start = time.time()
    ticket_id = ticket.id  # type: ignore[attr-defined]
    role_name = "doer"

    # Build a minimal context bundle (deep-context CLI may not be available
    # in smolagents mode; caller can pass a richer bundle in future phases).
    try:
        import subprocess
        import os
        proc = subprocess.run(
            ["/Users/manikanta/.local/bin/aiforge-deep-context",
             ticket.title],  # type: ignore[attr-defined]
            capture_output=True, text=True, timeout=150,
            env={**os.environ, "ROLE": role_name}, check=False,
        )
        context_bundle = proc.stdout or "(deep-context empty)"
    except Exception:
        context_bundle = "(deep-context unavailable — use grep/read_file tools)"

    llm_config = _LLMConfig(
        base_url=LM_STUDIO_BASE_URL,
        model=DOER_MODEL,
        api_key=LM_STUDIO_API_KEY,
    )

    emit(log, "smolagents.start", ticket=ticket.identifier)  # type: ignore[attr-defined]

    try:
        agent, task_prompt = build_doer_agent(ticket, worktree_path, context_bundle, llm_config)
        result = agent.run(task=task_prompt)

        # smolagents returns the final_answer string on success.
        summary_text = str(result) if result is not None else ""
        tickets.add_event(
            ticket_id, role_name, "comment",
            body=summary_text[:4000],
            metadata={"source": "smolagents_final_answer"},
        )
        emit(log, "smolagents.done", ticket=ticket.identifier,  # type: ignore[attr-defined]
             summary_chars=len(summary_text))
        return {
            "stop_reason": "final_answer",
            "has_commented": bool(summary_text),
            "turns": getattr(agent, "step_number", 0),
            "wall_s": round(time.time() - t_start, 2),
            "summary": summary_text,
        }

    except ScopeViolation as exc:
        emit(log, "smolagents.scope_violation",
             ticket=getattr(ticket, "identifier", "?"),
             path=exc.path)
        tickets.add_event(
            ticket_id, role_name, "error",
            body=f"scope violation: {exc}",
            metadata={"stop_reason": "scope_violation"},
        )
        return {
            "stop_reason": "scope_violation",
            "has_commented": False,
            "turns": 0,
            "wall_s": round(time.time() - t_start, 2),
            "summary": str(exc),
        }

    except Exception as exc:
        emit(log, "smolagents.exception",
             ticket=getattr(ticket, "identifier", "?"),
             error=str(exc)[:300])
        tickets.add_event(
            ticket_id, role_name, "error",
            body=f"smolagents exception: {exc}",
            metadata={"stop_reason": "exception"},
        )
        return {
            "stop_reason": "exception",
            "has_commented": False,
            "turns": 0,
            "wall_s": round(time.time() - t_start, 2),
            "summary": str(exc),
        }
