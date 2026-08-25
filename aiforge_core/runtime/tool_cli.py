"""aiforge-tool — call ONE registered integration tool from a shell script.

Job scripts and workflow helper scripts run headless bash; when they need
Jira/Confluence/GitLab/email DATA they must NOT hand-roll `curl` against the
REST APIs (no creds in the script env, breaks when the integration config
changes). This CLI is the bridge: it dispatches to the SAME tool registry the
chat agent uses — so the configured integration (URL + auth from Settings)
does the work and the script just consumes JSON.

Usage:
    aiforge-tool jira_search '{"jql": "project = CLR AND status = Open"}'
    aiforge-tool confluence_read '{"title": "Runbook", "space": "ENG"}'
    aiforge-tool --list                      # show callable tool names

Output: the tool's JSON result on stdout. Exit 0 on {"ok": true}-ish results,
2 on tool errors, 3 on unknown/refused tool.

SAFETY: headless scripts can't answer an approval gate, so only READ-ONLY
tools are callable by default (the chat agent's read-only classification).
Set AIFORGE_TOOL_CLI_ALLOW_WRITES=1 to also allow write tools — only for
operator-audited jobs.
"""
from __future__ import annotations

import json
import os
import sys


def _allow_writes() -> bool:
    return str(os.environ.get("AIFORGE_TOOL_CLI_ALLOW_WRITES", "")).strip() \
        .lower() in ("1", "true", "yes", "on")


def _callable_names() -> tuple[set, dict]:
    from aiforge_core.runtime import chat_agent
    readonly = set(chat_agent._READONLY_TOOLS)
    return readonly, chat_agent.TOOLS


def _resolve_cli_tool(name: str, tools: dict, readonly: set) -> "tuple[object, int | None]":
    """(fn, error_code) for a tool name: an unknown tool → (None, 3); a WRITE tool
    without the allow-writes opt-in → (None, 3); otherwise (fn, None). Prints the
    error envelope on rejection — headless scripts can't answer approval gates."""
    fn = tools.get(name)
    if fn is None:
        print(json.dumps({"ok": False, "error": f"unknown tool {name!r} — "
                          "run aiforge-tool --list"}))
        return None, 3
    if name not in readonly and not _allow_writes():
        print(json.dumps({"ok": False, "error":
                          f"{name!r} is a WRITE tool — headless scripts can't "
                          "answer approval gates. Reads only; set "
                          "AIFORGE_TOOL_CLI_ALLOW_WRITES=1 for an "
                          "operator-audited job."}))
        return None, 3
    return fn, None


def _parse_cli_args(raw: str) -> "tuple[dict | None, int | None]":
    """Parse the JSON args argument → (args_dict, None) or (None, 3) on bad JSON /
    a non-object."""
    try:
        tool_args = json.loads(raw) if raw.strip() else {}
        if not isinstance(tool_args, dict):
            raise ValueError("args must be a JSON object")
        return tool_args, None
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"bad JSON args: {exc}"}))
        return None, 3


def _run_cli_tool(fn, tool_args):
    """Invoke one resolved CLI tool, print its JSON result (or error), and return the process exit code."""
    try:
        res = fn(tool_args, os.getcwd())
    except Exception as exc:  # noqa: BLE001 — CLI surfaces, never tracebacks
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(res, default=str))
    return 0 if not (isinstance(res, dict) and res.get("ok") is False) else 2


def _run_list(tools, readonly):
    """Print every callable tool name (write tools only when writes are allowed)."""
    for n in sorted(tools):
        if n in readonly or _allow_writes():
            print(n)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    readonly, tools = _callable_names()
    if args[0] == "--list":
        return _run_list(tools, readonly)
    name = args[0]
    fn, err = _resolve_cli_tool(name, tools, readonly)
    if err is not None:
        return err
    tool_args, err = _parse_cli_args(args[1] if len(args) > 1 else "{}")
    if err is not None:
        return err
    return _run_cli_tool(fn, tool_args)


if __name__ == "__main__":
    raise SystemExit(main())
