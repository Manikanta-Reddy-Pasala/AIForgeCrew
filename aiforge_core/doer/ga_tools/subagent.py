"""Sub-agent Task tool — isolated GA loop with summary-only return.

Mirrors Claude Code's ``Task`` tool: the parent Doer dispatches a
focused question to a fresh GA agent loop with its own tools-schema +
history. The sub-agent runs to completion (or max_turns), produces a
short summary, and the parent's history sees ONLY that summary —
none of the sub-agent's intermediate tool noise.

KISS: pure dispatch helper. Caller (handler.do_dispatch_subagent)
builds the cfg / handler and passes them in. Sub-agent uses the same
``LLMSession`` cfg as the parent unless overridden via the
``model_override`` arg.

Allowed-tools allowlist is enforced in the schema injected into the
sub-agent's prompt — caller is responsible for filtering.

Toggle via ``AIFORGE_DOER_SUBAGENT=1`` (default off until smoke).
"""
from __future__ import annotations

from typing import Callable, Iterable


SCHEMA = {
    "type": "function",
    "function": {
        "name": "dispatch_subagent",
        "description": (
            "Spawn a focused sub-agent with an isolated context to "
            "investigate a question. The sub-agent runs read-only "
            "tools (file_read / glob / grep / search_memory) and "
            "returns a short summary. Use when you need an answer "
            "without polluting your own history with the search "
            "trail. Cap: 1 dispatch per parent turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Self-contained question. Sub-agent has no "
                        "memory of parent history; include any "
                        "necessary file paths, ticket ids, error "
                        "snippets in the task text itself."
                    ),
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional whitelist of tool names the sub-"
                        "agent may call. Defaults to read-only set."
                    ),
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Cap on sub-agent turns (default 8).",
                },
            },
            "required": ["task"],
        },
    },
}


# Default read-only tool whitelist — sub-agents shouldn't edit files
# or run mvn. The intent is "send me research, not actions".
DEFAULT_ALLOWED_TOOLS = frozenset({
    "file_read", "glob", "grep", "search_memory",
    "ask_explorer", "web_search",
})


def filter_schema(
    full_schema: list[dict], allowed: Iterable[str] | None,
) -> list[dict]:
    """Return only tool entries whose ``function.name`` is allowed."""
    keep = (
        frozenset(allowed) if allowed
        else DEFAULT_ALLOWED_TOOLS
    )
    out: list[dict] = []
    for entry in full_schema:
        name = (entry.get("function") or {}).get("name") or ""
        if name in keep:
            out.append(entry)
    return out


SUBAGENT_PREAMBLE = """You are a sub-agent dispatched by the parent
Doer. You have an isolated context — the parent's history and tools
are NOT available to you. Your job:

1. Investigate the question using the read-only tools provided.
2. Produce a short, factual summary as your final reply (≤ 600 chars).
3. Cite file:line when quoting code.

Hard rules:
- No file edits, no mvn, no shell mutations. Read-only only.
- Stop as soon as you have enough to answer; don't explore further.
- If you can't answer, say so in one line — do NOT speculate.
"""


def run_subagent(
    *,
    task: str,
    parent_cfg: dict,
    full_tools_schema: list[dict],
    allowed_tools: list[str] | None,
    max_turns: int,
    spawn_session: Callable[[dict], object],
    handler_cls: Callable[..., object],
    runner: Callable[..., object],
) -> str:
    """Run an isolated GA agent_runner_loop and return summary text.

    Caller injects the GA primitives (LLMSession constructor,
    ToolClient/handler factory, runner callable) so this module
    stays decoupled from `import_ga` and avoids a hard GA dependency
    in every test environment.
    """
    sub_schema = filter_schema(full_tools_schema, allowed_tools)
    if not sub_schema:
        return "[subagent] no allowed tools — nothing to do."

    sub_cfg = dict(parent_cfg)
    sub_cfg["name"] = "ga-subagent"
    session = spawn_session(sub_cfg)
    handler = handler_cls()

    captured = {"answer": ""}

    # Wrap the handler's final_answer (if it has one) so sub-agent
    # returns clean. If caller didn't wire final_answer the runner
    # exit returns whatever last text the model emitted.
    def _capture(text: str) -> None:
        captured["answer"] = (text or "")[:600]

    handler._on_final_answer = _capture  # type: ignore[attr-defined]

    try:
        gen = runner(
            session, system_prompt=SUBAGENT_PREAMBLE,
            user_input=f"## Task\n{task}",
            handler=handler, tools_schema=sub_schema,
            max_turns=int(max_turns or 8), verbose=False,
        )
        for _ in gen:
            pass
    except Exception as exc:
        return f"[subagent] error: {exc}"

    return captured["answer"] or "(no answer captured)"
