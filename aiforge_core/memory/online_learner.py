"""Online learner — step_trace + episodic + skills/failures/attachments.

This was backed by Postgres tables (episodic_outcomes, procedural_patterns,
audit_events, step_traces, skills, failures, attachments). Postgres has been
removed (SQLite-only build), so every writer is a soft no-op and every reader
returns empty. The public surface is preserved so callers degrade gracefully.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StepTrace:
    ticket_id: str
    agent_role: str
    step_index: int
    plan_step_id: str = ""
    input_context: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    tools_used: list[str] | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    prompt_version: str = "v1"
    status: str = "ok"


def migrate() -> bool:
    return False


def record_step_trace(t: StepTrace) -> None:
    return None


def record_episodic(*, ticket_id: str, stage: str, agent_role: str,
                    outcome: str, summary: str,
                    artifacts: dict | None = None,
                    hitl_weight: int = 1) -> None:
    return None


def update_procedural(*, agent_role: str, task_class: str,
                      tool_sequence: list[str], success: bool) -> None:
    return None


def record_audit(*, ticket_id: str, agent_role: str, event_type: str,
                 payload: dict | None, duration_ms: int = 0,
                 status: str = "ok", trace_id: str | None = None) -> bool:
    return False


def promote_skill(*, repo: str, task_class: str, name: str,
                  summary: str, body_md: str, success: bool) -> bool:
    return False


def top_skills_for(*, repo: str, task_class: str, k: int = 3) -> list[dict]:
    return []


def record_failure(*, repo: str, task_class: str, mode: str,
                   evidence: str, lesson: str = "") -> bool:
    return False


def top_failures_for(*, repo: str, task_class: str, k: int = 5) -> list[dict]:
    return []


def add_attachment(*, ticket_id: str, filename: str, file_path: str,
                   role: str = "other", content_type: str = "",
                   bytes_: int = 0) -> bool:
    return False


def attachments_for(ticket_id: str) -> list[dict]:
    return []


def detect_attachment_role(filename: str) -> str:
    """Cheap pattern-match — see process docs for the contract."""
    low = (filename or "").lower()
    if "tally" in low:
        return "tally"
    if "oneshell" in low:
        return "oneshell"
    if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "screenshot"
    return "other"
