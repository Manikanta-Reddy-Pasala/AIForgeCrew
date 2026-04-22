from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict):
    ticket_id: int
    ticket: dict
    role: str
    messages: list[dict]
    tool_results: list[dict]
    worktree_path: str | None
    stop_reason: str | None
    compile_fail_count: int
    verdict: str | None
    feedback_fixlist: str | None
    learner_digest: str | None
    flags: dict
