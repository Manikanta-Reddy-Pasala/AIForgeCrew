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
_JOB_TOOLS = frozenset({"command_wait", "command_output", "command_kill"})


def _tail(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""
    if len(text) <= budget:
        return text
    return "…\n" + text[-budget:]


def render_read(result: dict, cap: int) -> str:
    """A file or directory as text the model can copy into a patch."""
    if result.get("ok") is False:
        err = f"error: {result.get('error') or 'read failed'}"
        if result.get("outline"):
            err += "\noutline of the file:\n" + str(result["outline"])
        return err[:cap]
    if result.get("is_dir"):
        entries = result.get("entries") or []
        body = "\n".join(str(e) for e in entries)
        path = result.get("path") or ""
        text = (path + "\n" if path else "") + body
        return text[:cap]
    path = str(result.get("path") or "")
    note = str(result.get("note") or "")
    if result.get("outline") and not result.get("content"):
        return "\n".join(p for p in (path, note, str(result["outline"])) if p)[:cap]
    # read_lines (and a file_read of one symbol) carry ``text`` from ``start``
    content = result.get("content")
    if content is None:
        content = result.get("text")
    content = str(content or "")
    try:
        first = max(1, int(result.get("start") or 1))
    except (TypeError, ValueError):
        first = 1
    if result.get("already_in_context"):
        numbered = content            # a pointer to an earlier copy, not lines
    else:
        lines = content.split("\n")  # as read_lines counts (not splitlines)
        if lines and lines[-1] == "":
            lines.pop()
        numbered = "\n".join(f"{i}|{line}" for i, line in enumerate(lines, first))
    if not content and result.get("total_lines") is not None:
        numbered = f"(no lines there — the file has {result['total_lines']} lines)"
    parts = [p for p in (path, note, numbered) if p]
    text = "\n".join(parts)
    if len(text) > cap:
        text = text[:cap] + "\n…(truncated)"
    return text


def render_job(result: dict, cap: int) -> str:
    """A command handed back at a check-in, or a look at one. A running job
    has no exit code: calling it ``exit 0`` told the model a build that was
    still printing errors had passed."""
    key = result.get("id") or "?"
    secs = result.get("elapsed_s")
    took = f" after {secs}s" if secs is not None else ""
    if result.get("running") is True:
        state = [f"STILL RUNNING (job {key}){took} — not finished, no exit code yet"]
        if result.get("stuck"):
            state.append("looks stuck: no new output and no CPU")
        elif "cpu_active" in result:
            state.append("output growing" if result.get("output_growing")
                         else "no new output")
            state.append("using CPU" if result.get("cpu_active") else "idle CPU")
    else:
        code = result.get("code")
        state = [f"FINISHED (job {key}){took}, exit {code if code is not None else '?'}"]
        if result.get("stopped"):
            state.append(f"stopped: {result.get('error') or 'killed'}")
    head = "; ".join(state) + "\n"
    if result.get("returned_because"):
        head += f"returned early: {result['returned_because']}\n"
    hint = str(result.get("hint") or "")
    tail = ("\nnext: " + hint) if hint else ""
    body = str(result.get("new_output") or "")
    label = "new output:\n" if body else "new output: (none)\n"
    spare = max(0, cap - len(head) - len(label) - len(tail))
    return (head + label + _tail(body, spare) + tail)[:cap]


def render_command(result: dict, cap: int) -> str:
    """Exit code, then the stderr tail, then the stdout tail."""
    if result.get("running") is True or "new_output" in result:
        return render_job(result, cap)
    code = result.get("code")
    if code is None:
        code = 0 if result.get("ok") else 1
    err = str(result.get("stderr") or "")
    out = str(result.get("stdout") or result.get("output") or "")
    header = f"exit {code}\n"
    # A recipe ran (or was ignored): say so first, so the model knows the
    # project's own command is what produced this result.
    head = ""
    if result.get("recipe"):
        head = (f"[recipe {result['recipe']} replaced: {result['recipe_was']}]\n"
                if result.get("recipe_was") else f"[recipe {result['recipe']}]\n")
    elif result.get("recipe_warn"):
        head = (f"[this project has a {result['recipe_warn']} recipe — use it next time]\n")
    header = head + header
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
    elif (name in _COMMANDS or name in _JOB_TOOLS or "stdout" in result
          or "stderr" in result):
        body = render_command(result, cap)
    else:
        return json.dumps(result, ensure_ascii=False)[:cap]
    if guide:
        body = guide + "\n" + body
    return body[:cap]
