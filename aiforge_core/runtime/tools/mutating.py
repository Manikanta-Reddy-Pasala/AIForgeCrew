"""The ONE list of tool names that write files — every gate reads it from here.

There were four copies. They had drifted: the team pipeline's pre-apply review
gate (``tool_gate``) listed eight names, the chat loop's gate seven different
ones, and the ADK Doer is handed ``multi_edit``, ``rename_symbol`` and
``format`` — which the pipeline gate did not list. With "review edits" armed,
those three changed files without the Approve/Reject prompt, although the
gate's own comment warned that a missing name skips review.

A tool name belongs here when calling it changes files in the workspace,
including every alias a model may call (the Doer surfaces ``write``/``patch``/
``edit``/``str_replace`` for the canonical ``file_write``/``file_patch``).
Shells (``bash``, ``run_command``) are NOT here: they are gated by the command
risk classifier, which looks at what the command does rather than its name.
"""
from __future__ import annotations

#: Every tool name that writes workspace files.
FILE_WRITE_TOOLS: frozenset[str] = frozenset({
    # canonical
    "file_write", "file_create", "file_patch", "editor", "multi_edit",
    "rename_symbol", "format",
    # aliases models call, and names other agent stacks use
    "write", "write_file", "create_file", "patch", "apply_patch", "edit",
    "edit_block", "str_replace",
})

#: The subset that changes PART of a file rather than writing it whole. Callers
#: that describe an action ("patched x" vs "wrote x") split on this.
PATCH_STYLE_TOOLS: frozenset[str] = frozenset({
    "file_patch", "patch", "apply_patch", "edit", "edit_block", "str_replace",
    "multi_edit",
})

#: ``editor`` multiplexes read and write on ONE name; only these read.
EDITOR_READONLY_CMDS: frozenset[str] = frozenset({
    "view", "read", "list", "ls", "cat", "open",
})


def editor_command(args: "dict | None") -> str:
    """The sub-command an ``editor`` call carries, normalised."""
    a = args or {}
    return str(a.get("command") or a.get("sub_command") or "").strip().lower()


def writes_files(name: str, args: "dict | None" = None) -> bool:
    """True when THIS call changes files — ``editor view`` does not."""
    if name not in FILE_WRITE_TOOLS:
        return False
    if name == "editor":
        return editor_command(args) not in EDITOR_READONLY_CMDS
    return True


__all__ = ["EDITOR_READONLY_CMDS", "FILE_WRITE_TOOLS", "PATCH_STYLE_TOOLS",
           "editor_command", "writes_files"]
