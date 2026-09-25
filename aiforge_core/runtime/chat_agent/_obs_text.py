"""Plain-text tool results for the model.

``json.dumps`` then slicing the first 6,000 characters turns a file into
escaped ``\\n`` and, for a command, drops stderr when stdout fills the
budget. File reads go back as raw lines. Commands go back as the exit
code, then the stderr tail, then the stdout tail.
"""
from __future__ import annotations

import json

_READS = frozenset({"file_read", "read_lines", "read_files"})
_COMMANDS = frozenset({
    "run_command", "run_shell", "bash", "shell", "run", "run_tests",
})


def _tail(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""
    if len(text) <= budget:
        return text
    return "…\n" + text[-budget:]


def render_read(result: dict, cap: int) -> str:
    """A file or directory as text the model can copy into a patch."""
    if result.get("ok") is False:
        return f"error: {result.get('error') or 'read failed'}"
    if result.get("is_dir"):
        entries = result.get("entries") or []
        body = "\n".join(str(e) for e in entries)
        path = result.get("path") or ""
        text = (path + "\n" if path else "") + body
        return text[:cap]
    content = str(result.get("content") or "")
    numbered = "\n".join(f"{i}|{line}" for i, line in enumerate(content.splitlines(), 1))
    path = str(result.get("path") or "")
    note = str(result.get("note") or "")
    parts = [p for p in (path, note, numbered) if p]
    text = "\n".join(parts)
    if len(text) > cap:
        text = text[:cap] + "\n…(truncated)"
    return text


def render_command(result: dict, cap: int) -> str:
    """Exit code, then the stderr tail, then the stdout tail."""
    code = result.get("code")
    if code is None:
        code = 0 if result.get("ok") else 1
    err = str(result.get("stderr") or "")
    out = str(result.get("stdout") or result.get("output") or "")
    header = f"exit {code}\n"
    spare = max(0, cap - len(header))
    err_budget = spare // 2 if out else spare
    err_tail = _tail(err, err_budget)
    used = len(err_tail) + (len("stderr:\n") if err_tail else 0)
    out_tail = _tail(out, max(0, spare - used - 8))
    parts = [header]
    if err_tail:
        parts.append("stderr:\n" + err_tail + "\n")
    if out_tail:
        parts.append("stdout:\n" + out_tail)
    if result.get("error") and not err_tail:
        parts.append("error: " + str(result["error"]))
    return "".join(parts)[:cap]


def render_observation(name: str, result, cap: int) -> str:
    """What the model reads after a tool call.

    File reads and commands are plain text. Everything else stays a short
    JSON object so structured fields (a blocked-call ``next_step``) survive.
    """
    if not isinstance(result, dict):
        return str(result)[:cap]
    guide = str(result.get("next_step") or "")
    if name in _READS:
        body = render_read(result, cap)
    elif name in _COMMANDS or "stdout" in result or "stderr" in result:
        body = render_command(result, cap)
    else:
        return json.dumps(result, ensure_ascii=False)[:cap]
    if guide:
        body = guide + "\n" + body
    return body[:cap]
