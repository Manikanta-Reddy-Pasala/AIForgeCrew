from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from ._shell import (_resolve)

# The approval gate is where the operator accepts/rejects a write — they see the
# WHOLE thing (full page, full diff, full Jira body); it's just text the UI
# scrolls, so display content is UNCAPPED. The only bound is on diff COMPUTE:
# difflib is ~O(n·m), so past this size we show full new content instead of
# paying to compute a diff no one can read. Tunable.
try:
    _DIFF_COMPUTE_MAX = max(10_000, int(os.environ.get(
        "AIFORGE_APPROVAL_DIFF_COMPUTE_MAX", "60000")))
except (TypeError, ValueError):
    _DIFF_COMPUTE_MAX = 60_000


def _fence(body: str, lang: str = "") -> str:
    """Wrap text in a fenced code block so the markdown renderer shows it as a
    monospace block (diffs, commands, JSON) instead of reflowed prose."""
    return f"```{lang}\n{body}\n```"


def _xhtml_to_md(xhtml: str) -> str:
    """Light Confluence storage-XHTML → readable markdown, so the approval
    preview shows formatted text instead of raw ``<p>…</ac:…>`` tags."""
    import html
    s = xhtml or ""
    for i in range(6, 0, -1):                       # headings
        s = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>",
                   lambda m, i=i: "\n" + "#" * i + " " + m.group(1).strip() + "\n",
                   s, flags=re.I | re.S)
    s = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + m.group(1).strip()
               + "\n```\n", s, flags=re.I | re.S)
    s = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", s, flags=re.I | re.S)
    s = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", s, flags=re.I | re.S)
    s = re.sub(r"<(em|i)[^>]*>(.*?)</\1>", r"*\2*", s, flags=re.I | re.S)
    s = re.sub(r'''<a\b[^>]{0,400}href=["']([^"']{1,2000})["'][^>]{0,400}>(.*?)</a>''', r"[\2](\1)",
               s, flags=re.I | re.S)
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|ul|ol|h[1-6]|tr|table|ac:[\w-]+)>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)                    # strip remaining tags + macros
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _change_diff(old: str, new: str, label: str) -> str:
    """Unified diff of ``old`` → ``new`` as a fenced ```diff block (renders as
    a colored monospace block). ``_(no change)_`` when identical.

    The DIFF is uncapped (the operator reviews the whole change), but difflib is
    ~O(n·m): a huge↔huge rewrite could freeze the gate. When both sides exceed
    ``_DIFF_COMPUTE_MAX``, skip the diff and show the FULL new content instead —
    nothing is hidden, we just don't pay the quadratic cost to compute a diff no
    one can read anyway."""
    import difflib
    old, new = old or "", new or ""
    if len(old) > _DIFF_COMPUTE_MAX and len(new) > _DIFF_COMPUTE_MAX:
        return f"_(too large to diff — showing full new {label})_\n\n" + _fence(new)
    d = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"current {label}", tofile=f"new {label}", lineterm=""))
    return _fence(d, "diff") if d.strip() else "_(no change)_"


def _fetch_current(fn, args: dict, cwd: str, timeout: float = 4.0) -> dict:
    """Best-effort fetch of an item's CURRENT state for the approval diff,
    HARD-bounded so a slow/down integration API can't stall the approval gate
    (the tool's own 20s read timeout is too long to block the operator). Runs
    the read in a worker thread and abandons it after ``timeout`` seconds —
    the preview then just shows the new content with no diff."""
    import concurrent.futures
    # NOTE: a `with ThreadPoolExecutor()` block would call shutdown(wait=True)
    # on exit and re-block until the (possibly hung) read finished — defeating
    # the timeout. Shut down WITHOUT waiting so we return immediately; the
    # worker thread finishes on its own (bounded by the tool's own 20s read).
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        r = ex.submit(fn, args, cwd).result(timeout=timeout)
        return r if isinstance(r, dict) and r.get("ok") else {}
    except Exception:  # noqa: BLE001 — timeout / read error → no diff, not a stall
        return {}
    finally:
        ex.shutdown(wait=False)


def _preview_file_write(args: dict, cwd: str) -> str:
    import difflib
    path = args.get("path", "?")
    new = args.get("content", "")
    try:
        old = _resolve(cwd, path).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — no such file yet → it is a creation
        old = ""
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))
    if diff:
        return f"**Write `{path}`**\n\n" + _fence(diff, "diff")
    return f"**New file `{path}`** ({len(new)} bytes)\n\n" + _fence(str(new))


def _preview_file_patch(args: dict, cwd: str) -> str:
    return (f"**Patch `{args.get('path', '?')}`**\n\n" + _fence(
        f"- {str(args.get('old_text', ''))}\n"
        f"+ {str(args.get('new_text', ''))}", "diff"))


def _preview_command(args: dict, cwd: str) -> str:
    return "**Run command**\n\n" + _fence(str(args.get("cmd", "")), "bash")


def _preview_confluence_create(args: dict, cwd: str) -> str:
    return (f"### Create Confluence page\n\n"
            f"**Space:** `{args.get('space', '?')}` · "
            f"**Title:** {args.get('title', '?')}\n\n"
            f"**Body:**\n\n"
            + _xhtml_to_md(str(args.get('body', ''))))


def _preview_confluence_update(args: dict, cwd: str) -> str:
    from aiforge_core.runtime.tools import confluence
    pid = args.get("id", "?")
    cur = _fetch_current(confluence.confluence_read, {"id": pid}, cwd)
    cur_md = _xhtml_to_md(str(cur.get("body") or "")) if cur else ""
    out = f"### Update Confluence page `{pid}`\n\n"
    if args.get("title"):
        out += f"**New title:** {args['title']}\n\n"
    if args.get("body") is not None:
        new_md = _xhtml_to_md(str(args.get("body", "")))
        out += ("**Body changes:**\n\n" + _change_diff(cur_md, new_md, "body")
                if cur_md else "**New body:**\n\n" + new_md)
    return out


def _preview_jira_create(args: dict, cwd: str) -> str:
    from aiforge_core.runtime.tools.jira_format import to_jira_wiki
    md = (f"### Create Jira issue\n\n"
          f"**Project:** `{args.get('project', '?')}` · "
          f"**Type:** {args.get('issuetype', 'Task')}"
          + (f" · **Priority:** {args['priority']}" if args.get('priority') else "")
          + f"\n\n**Summary:** {args.get('summary', '?')}\n")
    if args.get("description"):
        # Preview the ACTUAL Jira wiki markup that will be sent (single-*
        # bold etc.), not the model's raw markdown — so what you approve
        # is what Jira renders.
        md += f"\n{to_jira_wiki(str(args['description']))}\n"
    if args.get("labels"):
        md += f"\n**Labels:** {args['labels']}\n"
    return md


def _preview_jira_update(args: dict, cwd: str) -> str:
    from aiforge_core.runtime.tools import jira
    from aiforge_core.runtime.tools.jira_format import to_jira_wiki
    key = args.get("key", "?")
    cur = _fetch_current(jira.jira_read, {"key": key}, cwd)
    md = f"### Update Jira issue `{key}`\n\n"
    if args.get("summary"):
        md += (f"**Summary:** {cur.get('summary', '(current)')} "
               f"→ **{args['summary']}**\n\n")
    md += _field_lines(args, ("priority", "assignee", "labels"))
    if args.get("description") is not None:
        # Diff Jira-wiki vs Jira-wiki: the current body is already wiki
        # markup, so convert the new one too — otherwise every '*bold*'
        # line reads as a change (markdown '**' vs wiki '*') and the
        # preview shows the wrong '**'. Now the diff is real content only.
        md += ("**Description changes:**\n\n"
               + _change_diff(str(cur.get("description") or ""),
                              to_jira_wiki(str(args["description"])),
                              "description"))
    return md


def _preview_jira_comment(args: dict, cwd: str) -> str:
    return (f"### Comment on Jira `{args.get('key', '?')}`\n\n"
            f"{_xhtml_to_md(str(args.get('body', '')))}")


def _preview_gitlab_create(args: dict, cwd: str) -> str:
    md = (f"### Create GitLab issue\n\n"
          f"**Project:** `{args.get('project', '?')}`\n\n"
          f"**Title:** {args.get('title', '?')}\n")
    if args.get("description"):
        md += f"\n{str(args['description'])}\n"
    if args.get("labels"):
        md += f"\n**Labels:** {args['labels']}\n"
    return md


def _preview_gitlab_update(args: dict, cwd: str) -> str:
    from aiforge_core.runtime.tools import gitlab
    proj, iid = args.get("project", "?"), args.get("iid", "?")
    cur = _fetch_current(gitlab.gitlab_read, {"project": proj, "iid": iid}, cwd)
    md = f"### Update GitLab issue `{proj}#{iid}`\n\n"
    if args.get("title"):
        md += (f"**Title:** {cur.get('title', '(current)')} "
               f"→ **{args['title']}**\n\n")
    md += _field_lines(args, ("labels", "state_event"))
    if args.get("description") is not None:
        md += ("**Description changes:**\n\n"
               + _change_diff(str(cur.get("description") or ""),
                              str(args["description"]), "description"))
    return md


def _preview_gitlab_comment(args: dict, cwd: str) -> str:
    return (f"### Comment on GitLab "
            f"`{args.get('project', '?')}#{args.get('iid', '?')}`\n\n"
            f"{str(args.get('body', ''))}")


def _field_lines(args: dict, keys: tuple) -> str:
    """``**Key:** value`` for each supplied field — the scalar half of an
    update preview, which both trackers spell the same way."""
    return "".join(f"**{k.replace('_', ' ').capitalize()}:** {args[k]}\n\n"
                   for k in keys if args.get(k))


# tool → preview builder. A table, because the old chain was fifteen `if
# tool == …` arms whose only shared part was the fallback.
_PREVIEWS = {
    "file_write": _preview_file_write,
    "file_create": _preview_file_write,
    "file_patch": _preview_file_patch,
    "run_command": _preview_command,
    "bash": _preview_command,
    "shell": _preview_command,
    "confluence_create": _preview_confluence_create,
    "confluence_update": _preview_confluence_update,
    "jira_create": _preview_jira_create,
    "jira_update": _preview_jira_update,
    "jira_comment": _preview_jira_comment,
    "gitlab_create": _preview_gitlab_create,
    "gitlab_update": _preview_gitlab_update,
    "gitlab_comment": _preview_gitlab_comment,
}


def _diff_preview(tool: str, args: dict, cwd: str) -> str:
    """Markdown preview of a mutating action for the approval gate.

    Returns markdown (the chat UI renders it): diffs/commands/JSON go in fenced
    code blocks; the integration write tools (Confluence/Jira/GitLab) get a
    readable heading + fields + body so the operator reviews formatted content,
    not a raw ``{"...": "..."}`` string dump.

    An unknown tool — or a builder that raises, e.g. because the tracker is
    unreachable — falls back to the raw args, since showing SOMETHING is what
    lets the operator decide.
    """
    builder = _PREVIEWS.get(tool)
    if builder is not None:
        try:
            return builder(args, cwd)
        except Exception:  # noqa: BLE001
            pass
    return _fence(json.dumps(args, default=str, indent=2), "json")
