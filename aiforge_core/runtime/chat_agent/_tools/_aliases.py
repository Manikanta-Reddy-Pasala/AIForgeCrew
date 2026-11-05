"""Tool names other coding agents use, mapped to this harness's tools.

A model trained on other agents reaches for their names. Each one answered a
bare "unknown tool", and the model tried the same name again: a click bench
run called ``read_file`` 40 times (all failed), and runs used ``write``,
``write_file``, ``edit`` and ``glob``. A known name with the obvious meaning
runs the tool it means, with its arguments renamed; anything else stays
unknown.
"""
from __future__ import annotations

#: alias -> (tool, {alias arg: tool arg})
_PATH = {"file_path": "path", "filePath": "path", "filename": "path", "file": "path"}
_ALIASES: dict = {
    "read_file": ("file_read", _PATH),
    "read": ("file_read", _PATH),
    "view": ("file_read", _PATH),
    "view_file": ("file_read", _PATH),
    "open_file": ("file_read", _PATH),
    "write": ("file_write", {**_PATH, "contents": "content", "text": "content"}),
    "write_file": ("file_write", {**_PATH, "contents": "content", "text": "content"}),
    "create_file": ("file_write", {**_PATH, "contents": "content", "text": "content"}),
    "edit": ("file_patch", {**_PATH, "oldString": "old_str", "old_string": "old_str",
                            "newString": "new_str", "new_string": "new_str",
                            "replaceAll": "replace_all"}),
    "edit_file": ("file_patch", {**_PATH, "oldString": "old_str", "old_string": "old_str",
                                 "newString": "new_str", "new_string": "new_str",
                                 "replaceAll": "replace_all"}),
    "str_replace": ("file_patch", {**_PATH, "old_string": "old_str",
                                   "new_string": "new_str"}),
    "bash": ("run_command", {"command": "cmd"}),
    "shell": ("run_command", {"command": "cmd"}),
    "execute_command": ("run_command", {"command": "cmd"}),
    "ls": ("list_dir", _PATH),
    "list_files": ("list_dir", _PATH),
}


def resolve(name, args, known) -> "tuple[str, dict]":
    """``(tool, args)`` for a call: an alias in ``_ALIASES`` that is not a
    tool of its own (``known``) becomes the tool it means, its arguments
    renamed (a name the tool already has wins). Anything else is returned
    as it came."""
    if not isinstance(name, str) or name in known:
        return name, args
    hit = _ALIASES.get(name)
    if hit is None or hit[0] not in known:
        return name, args
    tool, rename = hit
    out = {}
    for k, v in (args or {}).items():
        out.setdefault(rename.get(k, k), v)
    for k, v in (args or {}).items():
        if k in rename and rename[k] in args:
            out[rename[k]] = args[rename[k]]   # the tool's own name wins
    if tool == "run_command" and "command" in (args or {}) and "cmd" not in args \
            and isinstance(out.get("timeout"), (int, float)) and out["timeout"] > 1000:
        # The other agent's own shape (Claude Code / OpenCode bash: `command`,
        # timeout in milliseconds); run_command takes seconds. A call written
        # to run_command's schema (`cmd`) keeps its seconds.
        out["timeout"] = max(1, int(out["timeout"] / 1000))
    return tool, out
