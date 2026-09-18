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
    """Confluence storage-XHTML → readable markdown, so the approval preview
    shows formatted text instead of raw ``<p>…</ac:…>`` tags. The one converter
    lives in ``tools.markup_read``; the dossier uses it too, so a page reads the
    same wherever it is shown."""
    from aiforge_core.runtime.tools.markup_read import storage_to_md
    return storage_to_md(xhtml)


_HTMLISH = re.compile(r"</?(p|br|h[1-6]|ul|ol|li|strong|b|em|i|a|table|tr|td|th|div|span|code|pre|ac:)\b",
                      re.I)


def _body_md(text) -> str:
    """Any body an agent writes — Confluence storage/HTML, Jira wiki markup or
    Markdown — as Markdown the chat renders. The approval card must show the
    comment/page/email as it will READ, never as escaped markup."""
    text = str(text or "")
    if _HTMLISH.search(text):
        return _xhtml_to_md(text)
    from aiforge_core.runtime.tools.jira_format import _looks_like_wiki
    if _looks_like_wiki(text):
        from aiforge_core.runtime.tools.markup_read import wiki_to_md
        return wiki_to_md(text)
    return text


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
    except Exception:  # noqa: BLE001
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
    # No attachment download: analysing images would outrun the 4 s budget,
    # and the merge preview needs only the body.
    cur = _fetch_current(confluence.confluence_read,
                         {"id": pid, "attachments": False}, cwd)
    cur_md = _xhtml_to_md(str(cur.get("body") or "")) if cur else ""
    out = f"### Update Confluence page `{pid}`\n\n"
    if args.get("title"):
        out += f"**New title:** {args['title']}\n\n"
    if args.get("body") is not None:
        # Preview the MERGED page (the same merge the tool performs), so what
        # you approve is the page as it will be, not the fragment sent.
        from aiforge_core.runtime.tools.confluence._tools import merged_body
        from aiforge_core.runtime.tools.edit_merge import EditError
        mode = args.get("mode") or "replace"
        out += f"**Mode:** `{mode}`" + (f" · section *{args['section']}*"
                                         if args.get("section") else "") + "\n\n"
        if not cur:           # current page unavailable: show what is sent
            return out + "**Sent (" + mode + "):**\n\n" + _xhtml_to_md(
                str(args.get("body", "")))
        try:
            merged, _ = merged_body(str(cur.get("body") or ""), args)
        except EditError as exc:
            return out + f"⚠ **Will be refused:** {exc}\n"
        new_md = _xhtml_to_md(merged)
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
        md += f"\n{_body_md(to_jira_wiki(str(args['description'])))}\n"
    if args.get("labels"):
        md += f"\n**Labels:** {args['labels']}\n"
    return md


def _preview_jira_update(args: dict, cwd: str) -> str:
    from aiforge_core.runtime.tools import jira
    key = args.get("key", "?")
    cur = _fetch_current(jira.jira_read, {"key": key, "attachments": False}, cwd)
    md = f"### Update Jira issue `{key}`\n\n"
    if args.get("summary"):
        md += (f"**Summary:** {cur.get('summary', '(current)')} "
               f"→ **{args['summary']}**\n\n")
    md += _field_lines(args, ("priority", "assignee", "labels"))
    from aiforge_core.runtime.tools.jira._edit import description_args
    args = description_args(args)             # a raw fields.description too
    if args.get("description") is not None:
        # Diff Jira-wiki vs Jira-wiki: the current body is already wiki
        # markup, and the new one is the MERGED description (the same merge
        # jira_update performs) — so the diff is the real change only.
        from aiforge_core.runtime.tools.edit_merge import EditError
        from aiforge_core.runtime.tools.jira._edit import merged_description
        if not cur:           # current issue unavailable: show what is sent
            return md + (f"**Description sent ({args.get('mode') or 'replace'}):**"
                         f"\n\n{args['description']}\n")
        current = str(cur.get("description") or "")
        try:
            new = merged_description(current, args)
        except EditError as exc:
            return md + f"⚠ **Will be refused:** {exc}\n"
        md += ("**Description changes:**\n\n"
               + _change_diff(current, new, "description"))
    return md


def _preview_jira_comment(args: dict, cwd: str) -> str:
    # What Jira will render: the body as it is converted to wiki, read back.
    from aiforge_core.runtime.tools.jira_format import to_jira_wiki
    return (f"### Comment on Jira `{args.get('key', '?')}`\n\n"
            f"{_body_md(to_jira_wiki(str(args.get('body', ''))))}")


def _preview_confluence_comment(args: dict, cwd: str) -> str:
    return (f"### Comment on Confluence page `{args.get('id', '?')}`\n\n"
            f"{_body_md(args.get('body') or args.get('text'))}")


def _preview_confluence_add_label(args: dict, cwd: str) -> str:
    labels = args.get("labels") or args.get("label") or []
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(",") if x.strip()]
    return (f"### Add labels to Confluence page `{args.get('id', '?')}`\n\n"
            + ", ".join(f"`{x}`" for x in labels))


def _preview_confluence_attach(args: dict, cwd: str) -> str:
    src = args.get("path") or args.get("url") or "?"
    name = args.get("filename") or os.path.basename(str(src))
    return (f"### Attach a file to Confluence page `{args.get('id', '?')}`\n\n"
            f"**File:** `{name}`  \n**From:** `{src}`")


def _preview_jira_transition(args: dict, cwd: str) -> str:
    md = (f"### Move Jira `{args.get('key', '?')}`\n\n"
          f"**To:** {args.get('transition') or args.get('status') or '?'}\n")
    if args.get("comment"):
        md += f"\n**Comment:**\n\n{_body_md(args['comment'])}\n"
    return md


def _preview_jira_assign(args: dict, cwd: str) -> str:
    return (f"### Assign Jira `{args.get('key', '?')}`\n\n"
            f"**To:** {args.get('assignee') or '(unassigned)'}")


def _preview_jira_link_issues(args: dict, cwd: str) -> str:
    inward = args.get("inward") or args.get("from") or "?"
    outward = args.get("outward") or args.get("to") or "?"
    md = (f"### Link Jira issues\n\n`{inward}` **{args.get('type') or args.get('link_type') or 'Relates'}** "
          f"`{outward}`\n")
    if args.get("comment"):
        md += f"\n**Comment:**\n\n{_body_md(args['comment'])}\n"
    return md


def _preview_jira_log_work(args: dict, cwd: str) -> str:
    md = (f"### Log work on Jira `{args.get('key', '?')}`\n\n"
          f"**Time:** {args.get('time_spent') or '?'}\n")
    if args.get("comment"):
        md += f"\n{_body_md(args['comment'])}\n"
    return md


def _preview_email_send(args: dict, cwd: str) -> str:
    to = args.get("to")
    to = ", ".join(to) if isinstance(to, list) else str(to or "?")
    md = f"### Send an email\n\n**To:** {to}\n"
    for k in ("cc", "bcc"):
        if args.get(k):
            v = args[k]
            md += f"\n**{k.capitalize()}:** {', '.join(v) if isinstance(v, list) else v}\n"
    md += f"\n**Subject:** {args.get('subject') or '(none)'}\n\n---\n\n"
    return md + _body_md(args.get("html") or args.get("body"))


def _preview_gitlab_mr(args: dict, cwd: str) -> str:
    md = (f"### Open a merge request in `{args.get('project', '?')}`\n\n"
          f"**{args.get('title', '?')}**  \n`{args.get('source_branch', '?')}` → "
          f"`{args.get('target_branch') or 'default'}`\n")
    if args.get("description"):
        md += f"\n{_body_md(args['description'])}\n"
    return md


def _preview_gitlab_mr_comment(args: dict, cwd: str) -> str:
    return (f"### Comment on merge request "
            f"`{args.get('project', '?')}!{args.get('iid', '?')}`\n\n"
            f"{_body_md(args.get('body'))}")


def _preview_github_pr(args: dict, cwd: str) -> str:
    md = (f"### Open a GitHub pull request\n\n**{args.get('title', '?')}**  \n"
          f"`{args.get('head') or 'current branch'}` → `{args.get('base') or 'main'}`\n")
    if args.get("body"):
        md += f"\n{_body_md(args['body'])}\n"
    return md


def _preview_code(lang: str) -> Callable[[dict, str], str]:
    def build(args: dict, cwd: str) -> str:
        code = args.get("code") or args.get("script") or args.get("cmd") or ""
        return "**Run:**\n\n" + _fence(str(code), lang)
    return build


def _preview_generic(tool: str, args: dict) -> str:
    """Any other write: a heading and its fields, long text rendered as it will
    read — not a raw ``{"body": "<p>…\\u2014…"}`` dump."""
    md = f"### {tool.replace('_', ' ').capitalize()}\n\n"
    for k, v in args.items():
        label = k.replace("_", " ").capitalize()
        if isinstance(v, str) and ("\n" in v or len(v) > 120 or _HTMLISH.search(v)):
            md += f"**{label}:**\n\n{_body_md(v)}\n\n"
        elif isinstance(v, (dict, list)):
            md += f"**{label}:**\n\n" + _fence(json.dumps(v, default=str, indent=2,
                                                          ensure_ascii=False), "json") + "\n\n"
        elif v not in (None, ""):
            md += f"**{label}:** {v}\n\n"
    return md


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
            f"{_body_md(args.get('body'))}")


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
    "confluence_comment": _preview_confluence_comment,
    "confluence_add_label": _preview_confluence_add_label,
    "confluence_attach": _preview_confluence_attach,
    "jira_transition": _preview_jira_transition,
    "jira_assign": _preview_jira_assign,
    "jira_link_issues": _preview_jira_link_issues,
    "jira_log_work": _preview_jira_log_work,
    "email_send": _preview_email_send,
    "gitlab_mr_create": _preview_gitlab_mr,
    "gitlab_mr_comment": _preview_gitlab_mr_comment,
    "github_pr": _preview_github_pr,
    "execute_ipython_cell": _preview_code("python"),
}


def _diff_preview(tool: str, args: dict, cwd: str) -> str:
    """Markdown preview of a mutating action for the approval gate.

    Returns markdown (the chat UI renders it): diffs/commands/JSON go in fenced
    code blocks; the integration write tools (Confluence/Jira/GitLab) get a
    readable heading + fields + body so the operator reviews formatted content,
    not a raw ``{"...": "..."}`` string dump.

    An unknown tool — or a builder that raises, e.g. because the tracker is
    unreachable — gets a generic readable preview (fields, long text rendered),
    and only if that fails too the raw args.
    """
    builder = _PREVIEWS.get(tool)
    if builder is not None:
        try:
            return builder(args, cwd)
        except Exception:  # noqa: BLE001
            pass
    try:
        return _preview_generic(tool, args)
    except Exception:  # noqa: BLE001
        return _fence(json.dumps(args, default=str, indent=2, ensure_ascii=False), "json")
