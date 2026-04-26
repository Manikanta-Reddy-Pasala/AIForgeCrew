"""Batch tool — run multiple tool calls in parallel within a turn.

Mirrors Claude Code's ability to call several tools per response.
Model emits a single ``batch`` call with a list of sub-calls; we
fan them out across a small ThreadPoolExecutor and return joined
output.

Bounded concurrency to avoid hammering the worktree (mvn + git
ops don't parallelise well). Tools that mutate state (file_patch,
file_write) are blacklisted — only read-side tools allowed.
"""
from __future__ import annotations

import concurrent.futures as _cf
from typing import Callable

# Read-only tools safe for parallel dispatch. file_patch/file_write
# mutate the worktree; bash mutates env. Keep parallel surface
# strictly read-side to avoid race conditions.
_PARALLEL_SAFE = {"file_read", "glob", "grep", "ask_explorer", "web_search"}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "batch",
        "description": (
            "Run several read-side tools in parallel within one turn. "
            "Use to fan out exploration: glob + grep + file_read at "
            "once. Allowed sub-tools: file_read, glob, grep, "
            "ask_explorer, web_search. Returns joined results in "
            "input order. Mutating tools (file_patch, bash) are "
            "rejected for safety — call those one at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "description": (
                        "List of sub-calls. Each item: "
                        "{tool: <name>, args: <object>}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["tool", "args"],
                    },
                },
            },
            "required": ["calls"],
        },
    },
}


def handle(dispatch: Callable[[str, dict], str], calls: list[dict],
           max_workers: int = 4) -> str:
    """Fan out ``calls`` via ``dispatch(tool_name, args) -> str``.

    Caller (handler) supplies a dispatch function that maps a tool
    name + args dict to the same string a single-tool handler
    would yield. Pure logic — no GA dependency.
    """
    if not calls:
        return "[batch] empty calls list"
    rejected = [c for c in calls if c.get("tool") not in _PARALLEL_SAFE]
    safe = [c for c in calls if c.get("tool") in _PARALLEL_SAFE]
    if rejected:
        names = sorted({c.get("tool", "?") for c in rejected})
        return (f"[batch] rejected unsafe tools: {names}. "
                f"Allowed: {sorted(_PARALLEL_SAFE)}")
    workers = max(1, min(max_workers, len(safe)))
    out: list[str] = [""] * len(safe)
    with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(dispatch, c["tool"], c.get("args") or {}): i
            for i, c in enumerate(safe)
        }
        for fut in _cf.as_completed(futures):
            i = futures[fut]
            try:
                out[i] = fut.result()
            except Exception as exc:  # noqa: BLE001
                out[i] = f"[batch] sub-call {i} crashed: {exc}"
    parts: list[str] = []
    for i, (call, result) in enumerate(zip(safe, out)):
        head = f"=== [{i}] {call['tool']} ==="
        parts.append(head + "\n" + result)
    return "\n".join(parts)
