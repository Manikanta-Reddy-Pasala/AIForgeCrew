"""Recovery policy — what to do when a detector trips.

Per-mode action:
    F-001 (hallucinated import)  → block + retry doer with ground-truth import list
    F-002 (hallucinated symbol)  → block + retry doer with ground-truth symbol list
    F-003 (diff context hash)    → re-fetch file content + retry doer
    F-004/F-007/F-008/F-010 (loops) → escalate to KGR (knowledge gap resolver)
    F-005 (unreachable plan step) → re-plan with feedback verdict
    F-006 (plan depth)            → force task-split: split into 2 sub-tickets
    F-009 (token budget)          → re-plan with smaller scope
    F-011 (skill misapplication)  → demote skill, fall back to plan-from-scratch
    F-012 (memory contradiction)  → quarantine memory; re-fetch fresh

The orchestrator calls `decide(mode_id)` and gets back an Action enum.
"""
from __future__ import annotations

from enum import Enum


class Action(Enum):
    BLOCK_AND_RETRY     = "block_and_retry"
    REPLAN              = "replan"
    REPLAN_SMALLER      = "replan_smaller"
    SPLIT_TICKET        = "split_ticket"
    KGR_FALLBACK        = "kgr_fallback"
    DEMOTE_SKILL        = "demote_skill"
    QUARANTINE_MEMORY   = "quarantine_memory"
    ESCALATE_HUMAN      = "escalate_human"
    PAUSE_NO_OP         = "pause_no_op"


_POLICY: dict[str, Action] = {
    "F-001": Action.BLOCK_AND_RETRY,
    "F-002": Action.BLOCK_AND_RETRY,
    "F-003": Action.BLOCK_AND_RETRY,
    "F-004": Action.KGR_FALLBACK,
    "F-005": Action.REPLAN,
    "F-006": Action.SPLIT_TICKET,
    "F-007": Action.KGR_FALLBACK,
    "F-008": Action.KGR_FALLBACK,
    "F-009": Action.REPLAN_SMALLER,
    "F-010": Action.KGR_FALLBACK,
    "F-011": Action.DEMOTE_SKILL,
    "F-012": Action.QUARANTINE_MEMORY,
}


def decide(mode_id: str) -> Action:
    return _POLICY.get(mode_id, Action.ESCALATE_HUMAN)
