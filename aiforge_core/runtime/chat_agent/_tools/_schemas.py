"""OpenAI-native function schemas for the chat agent's CORE coding tools.

The text ACTION/ARGS_JSON protocol makes local models fumble arguments into
``ARGS_JSON: {}`` (arg-less tool calls). Native OpenAI tool-calling — the same
mechanism OpenWebUI uses — gets real structured arguments instead. We declare
schemas for the high-frequency coding tools where the empty-args bug bites; the
long tail (jira/confluence/gitlab/…) is still reachable via the text protocol in
the same turn (hybrid), so nothing is lost.

Names MUST match the ``TOOLS`` registry keys (``_registry.py``) so a native
``tool_calls`` reply dispatches through the exact same code path as a text
ACTION. ``additionalProperties: true`` lets optional keys (force, timeout, …)
through without enumerating every one.
"""
from __future__ import annotations


def _fn(name: str, desc: str, props: dict, required: tuple = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": list(required), "additionalProperties": True}}}


_S = {"type": "string"}
_I = {"type": "integer"}


# Curated CORE set — file ops, search, run, edit, tests, git, memory, repo.
NATIVE_TOOL_SCHEMAS: list[dict] = [
    _fn("file_read", "Read a file's contents.",
        {"path": {"type": "string", "description": "relative or absolute path"}},
        ("path",)),
    _fn("file_write", "Create or overwrite a file with the given content "
        "(syntax-checked before it lands; pass force:true to override).",
        {"path": _S, "content": _S, "force": {"type": "boolean"}},
        ("path", "content")),
    _fn("file_create", "Create a new file with the given content.",
        {"path": _S, "content": _S}, ("path", "content")),
    _fn("file_patch", "Replace old_text with new_text in a file "
        "(syntax-checked; force:true overrides).",
        {"path": _S, "old_text": _S, "new_text": _S, "force": {"type": "boolean"}},
        ("path", "old_text", "new_text")),
    _fn("multi_edit", "Apply several find/replace edits across one or many files "
        "in one atomic call (validated first, then all-or-nothing).",
        {"edits": {"type": "array", "items": {"type": "object", "properties": {
            "path": _S, "old_str": _S, "new_str": _S,
            "replace_all": {"type": "boolean"}},
            "required": ["path", "old_str", "new_str"]}}},
        ("edits",)),
    _fn("editor", "Structured file editor with syntax-check + undo. command: "
        "view | create | str_replace | insert | undo_edit.",
        {"command": _S, "path": _S, "old_str": _S, "new_str": _S,
         "file_text": _S, "insert_line": _I}, ("command", "path")),
    _fn("list_dir", "List a directory.", {"path": _S}, ("path",)),
    _fn("find", "Fuzzy-locate files/dirs by partial name.",
        {"name": _S, "kind": {"type": "string", "enum": ["file", "dir"]}},
        ("name",)),
    _fn("grep", "Recursively search file contents for a pattern.",
        {"pattern": _S, "path": _S}, ("pattern",)),
    _fn("read_lines", "Read a line range from a file.",
        {"path": _S, "start": _I, "end": _I}, ("path",)),
    _fn("run_command", "Run a shell command (timeout in SECONDS, default 600). "
        "For a test suite, run ONE file/case, not the whole suite.",
        {"cmd": _S, "timeout": _I}, ("cmd",)),
    _fn("project", "Detect + build/test/run the project (maven/gradle/node/"
        "python/go/rust). action: build|test|run|install.",
        {"action": _S}, ("action",)),
    _fn("ensure_runtime", "Install + verify missing toolchain binaries.",
        {"tools": {"type": "array", "items": _S}}, ("tools",)),
    _fn("run_tests", "Run the project's tests. mode: fast|all|discover; "
        "optional -k/-Dtest pattern.",
        {"mode": _S, "pattern": _S}),
    _fn("typecheck", "Run the project's type-checker (tsc/mypy/go vet).", {}),
    _fn("format", "Auto-format a file (ruff/prettier/gofmt).", {"path": _S}),
    _fn("lsp", "Symbol navigation: goto_definition|find_references|hover "
        "(0-indexed).",
        {"command": _S, "path": _S, "line": _I, "character": _I},
        ("command", "path")),
    _fn("rename_symbol", "Rename a symbol across the project.",
        {"path": _S, "old_name": _S, "new_name": _S}),
    _fn("git_status", "Show git working-tree status.", {"path": _S}),
    _fn("git_diff", "Show a git diff.", {"path": _S}),
    _fn("git_log", "Show recent git commits.", {"path": _S, "limit": _I}),
    _fn("git_blame", "Show git blame for a file.", {"path": _S}, ("path",)),
    _fn("memory_lookup", "Recall learnings/decisions from knowledge memory.",
        {"query": _S}, ("query",)),
    _fn("memory_write", "Persist a durable fact/decision for future recall.",
        {"text": _S, "kind": _S, "scope": _S,
         "tags": {"type": "array", "items": _S}}, ("text",)),
    _fn("search_chat_sessions", "Find things discussed in PAST chat sessions.",
        {"query": _S, "limit": _I}, ("query",)),
    _fn("remember_rule", "Persist a user rule applied to every session.",
        {"text": _S, "description": _S, "scope": _S,
         "triggers": {"type": "array", "items": _S}}, ("text",)),
    _fn("resolve_repo", "Loosely-typed repo/service name → local path.",
        {"name": _S}, ("name",)),
]

# Fast membership check + name set (native dispatch validates against this).
NATIVE_TOOL_NAMES = frozenset(s["function"]["name"] for s in NATIVE_TOOL_SCHEMAS)
