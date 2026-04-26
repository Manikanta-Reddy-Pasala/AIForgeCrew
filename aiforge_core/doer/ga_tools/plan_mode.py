"""Plan Mode for Doer — read-only think pass before writes unlock.

Mirrors Claude Code's Plan Mode. Doer enters plan_mode at task start
when ``AIFORGE_DOER_PLAN_MODE=1``; while active any write tool
(``file_patch`` / ``file_write`` / ``bulk_edit`` / ``java_refactor`` /
``code_run`` containing edit verbs) is rejected with a guidance
message. Doer must call ``exit_plan_mode`` once the read-only think
phase is complete; from there writes are unlocked for the rest of the
run.

Wire-up (single line in handler):
    if plan_mode.is_active(handler) and tool_name in plan_mode.WRITE_TOOLS:
        return plan_mode.reject(tool_name)

Or via a tool_before_callback short-circuit. KISS: handler stashes
``self._plan_mode_active`` (bool) and a counter ``self._plan_mode_reads``
so we can require N read-tool calls before allowing exit.
"""
from __future__ import annotations


WRITE_TOOLS = frozenset({
    "file_patch", "file_write", "bulk_edit", "java_refactor",
})

ENTER_TOOL = "enter_plan_mode"
EXIT_TOOL = "exit_plan_mode"


SCHEMA_ENTER = {
    "type": "function",
    "function": {
        "name": ENTER_TOOL,
        "description": (
            "Enter Plan Mode — read-only think phase before writes "
            "unlock. Use file_read / glob / grep / ask_explorer to "
            "build understanding, then call exit_plan_mode with the "
            "plan summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why plan mode is needed (1 line)",
                },
            },
            "required": [],
        },
    },
}


SCHEMA_EXIT = {
    "type": "function",
    "function": {
        "name": EXIT_TOOL,
        "description": (
            "Leave Plan Mode and unlock write tools. Pass the plan "
            "summary you intend to execute."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": (
                        "Numbered short plan — files to edit, in what "
                        "order, with one-line rationale per step."
                    ),
                },
            },
            "required": ["plan"],
        },
    },
}


def is_active(handler: object) -> bool:
    """True iff this handler entered plan mode and hasn't exited."""
    return bool(getattr(handler, "_plan_mode_active", False))


def reject_message(tool_name: str) -> str:
    """Standard guidance when a write tool fires during plan mode."""
    return (
        f"[plan_mode] '{tool_name}' is blocked while Plan Mode is "
        f"active. Call read-only tools (file_read / glob / grep) to "
        f"investigate, then call exit_plan_mode(plan=...) to unlock "
        f"writes."
    )


def enter(handler: object, reason: str = "") -> str:
    """Mark handler as plan-mode-active. Idempotent."""
    handler._plan_mode_active = True  # type: ignore[attr-defined]
    handler._plan_mode_reason = (reason or "")[:200]  # type: ignore[attr-defined]
    return f"[plan_mode] entered. Reason: {reason or '(none)'}"


def exit_(handler: object, plan: str) -> str:
    """Unlock writes; stash plan text on handler for downstream tools."""
    handler._plan_mode_active = False  # type: ignore[attr-defined]
    handler._plan_mode_plan = (plan or "")[:4000]  # type: ignore[attr-defined]
    return f"[plan_mode] exited. Plan ({len(plan)} chars) recorded; writes unlocked."
