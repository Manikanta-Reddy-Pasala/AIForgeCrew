"""Resume a turn that was stopped, instead of running it again from scratch.

Retry used to re-send the same words and nothing else, so the agent started
over: it re-read the files it had already read, re-wrote the files it had
already written, and often died in the same place. The work of the failed
attempt was on disk the whole time — nobody told the agent about it.

The turn's own persisted steps know what happened. This turns them into a
briefing: what LANDED, what was only ATTEMPTED, what is still PENDING, what it
died on — and the rule that makes it useful: verify before redoing, and finish
only what is left.

Three things learned the hard way, all of them from reading what the codebase
actually persists rather than what one code path happens to produce:

* Six different places emit ``subtasks``; five of them name the unit of work
  ``goal`` and only one uses ``title``. Reading ``title`` alone produced briefs
  that listed slugs — "finish sub-2, sub-3" — with no statement of the work.
* The ADK team pipeline sets every tool result to ``{"by": author}``; the real
  outcome arrives separately. "No error field" therefore does NOT mean the
  write landed, so an unknown outcome is reported as ATTEMPTED, never as done.
  Claiming a file landed when it did not is how a resume drops it for good.
* ``SPEC.md``'s checkboxes are never ticked by anything in this repo, and
  ``cwd`` is often a real project the user pinned. Reading unchecked boxes as
  "pending work from your last attempt" injected a whole roadmap into the turn.
  That block is gone.

Built from PERSISTED state only: the process may have restarted between the
stop and the retry, and a resume that needs the server to still be up is the
resume nobody gets.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.chat_resume")

# Tools that CHANGE the workspace. Union of tool_gate._MUTATING (the review
# gate's list, aliases included) and the chat agent's own edit-tool set — the
# two had already drifted apart, and a tool missing here is an edit the brief
# does not know about, so the resume repeats it.
_EDIT_TOOLS = frozenset((
    "write_file", "file_write", "edit", "editor", "edit_block", "file_patch",
    "patch", "apply_patch", "str_replace", "create_file", "multi_edit",
    "file_create", "write", "rename_symbol", "format",
))

# ``editor`` multiplexes read and write on ONE tool name. Only the write
# sub-commands touch anything (mirrors tool_gate._EDITOR_READONLY_CMDS).
_EDITOR_READONLY_CMDS = frozenset({"view", "read", "list", "ls", "cat", "open"})

# Where a tool call keeps its path — same key list the syntax-check guard uses.
_PATH_KEYS = ("path", "file", "filename", "file_path", "target")

_SHELL_TOOLS = frozenset({"run_command", "shell", "bash"})

# A subtask in one of these states needs nothing more from the resumed run.
# `won` is best-of-N's winner, `planned` is plan mode's terminal state.
_DONE_STATUSES = frozenset({
    "done", "ok", "complete", "completed", "success", "succeeded",
    "skipped", "won", "planned",
})

_MAX_FILES = 40
_MAX_ERRORS = 3
_MAX_PENDING = 30
_MAX_BRIEF_CHARS = 4000


def _txt(v) -> str:
    """Anything → a trimmed, SINGLE-LINE string.

    Steps come off disk and out of models; a non-string where a string was
    assumed used to raise here, and the caller's except turned that into
    "resume silently did nothing".

    Single-line matters for more than tidiness: every item is rendered as one
    "  - x" line under a heading, and this text is model-, tool- and
    file-derived. Letting a newline through would let an error string or a
    filename fabricate its own heading inside the brief.
    """
    if v is None:
        return ""
    t = v if isinstance(v, str) else str(v)
    return " ".join(t.split())


def _is_stopped(row: dict) -> bool:
    """Did this assistant turn END on a stop rather than an answer?

    Structural marker FIRST: ``chat_persist`` stamps a ``{"type": "stopped"}``
    step on every turn it persists as cancelled or banner-stopped. Prose is the
    fallback for turns written before that marker existed — and only as a
    PREFIX, because "stopped by user" is literally what run_command returns on
    cancel, so an agent quoting its own tool output was being read as a stop.
    """
    steps = row.get("steps")
    if isinstance(steps, list):
        for s in steps:
            if isinstance(s, dict) and (s.get("type") == "stopped"
                                        or s.get("stopped") is True):
                return True
    text = _txt(row.get("content"))
    return text.startswith("(stopped")


def last_stopped_turn(rows: list) -> "tuple[dict, str] | None":
    """(assistant_row, the user prompt that caused it) for the most recent
    turn — but only when that turn was STOPPED. ``None`` when it finished
    normally, which is the common case and must stay untouched: a resume brief
    on a successful turn tells the agent to "finish" work with no remainder.
    """
    if not rows:
        return None
    last_assistant = None
    idx = -1
    for i in range(len(rows) - 1, -1, -1):
        if isinstance(rows[i], dict) and rows[i].get("role") == "assistant":
            last_assistant, idx = rows[i], i
            break
    if last_assistant is None or not _is_stopped(last_assistant):
        return None
    prompt = ""
    for j in range(idx - 1, -1, -1):
        if isinstance(rows[j], dict) and rows[j].get("role") == "user":
            prompt = _txt(rows[j].get("content"))
            break
    return last_assistant, prompt


def _first_path(d) -> str:
    """The first workspace path in ``d`` under any of the known path keys.

    The tools disagree about what they call it (path / file / filename / …),
    so the KEY ORDER is the precedence — and looking it up was written out
    twice, once for the call args and once for each nested edit.
    """
    if not isinstance(d, dict):
        return ""
    for k in _PATH_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _paths(name: str, args: dict, result) -> list:
    """Every workspace path this tool call names.

    Covers the single-path tools, ``multi_edit``'s edit list, and the parallel
    runner's ``wrote files`` step, whose paths live in the RESULT.
    """
    out: list = []
    first = _first_path(args)
    if first:
        out.append(first)
    for edit in (args.get("edits") or []) if isinstance(args, dict) else []:
        nested = _first_path(edit)
        if nested:
            out.append(nested)
    if isinstance(result, dict):
        out.extend(v.strip() for v in (result.get("files") or [])
                   if isinstance(v, str) and v.strip())
    return out


def _mutates(name: str, args: dict) -> bool:
    if name not in _EDIT_TOOLS:
        return False
    if name == "editor":
        cmd = _txt(args.get("command") or args.get("sub_command")).lower()
        return cmd not in _EDITOR_READONLY_CMDS
    return True


def _empty_collection() -> dict:
    return {"landed": [],      # the write reported success
            "attempted": [],   # a write whose outcome we cannot read
            "commands": [], "errors": [],
            "steers": [],      # what the user redirected the turn to, mid-run
            "subtasks": {}}


def _collect_steer(s: dict, acc: dict) -> None:
    """A steer can REPLACE the request (see chat_steer.steer_directive), and
    resume re-sends the ORIGINAL words — so without this the brief lists
    postgres files as landed under a prompt that still says MySQL, and the
    agent dutifully reverts them."""
    t = _txt(s.get("text"))
    if t:
        acc["steers"].append(t[:300])


def _collect_error(s: dict, acc: dict) -> None:
    t = _txt(s.get("text"))
    if t:
        acc["errors"].append(t[:300])


def _collect_subtasks(s: dict, acc: dict) -> None:
    for it in (s.get("items") or []):
        if not (isinstance(it, dict) and it.get("slug")):
            continue
        slug = _txt(it["slug"])
        acc["subtasks"][slug] = {
            # `goal` is what five of the six producers call it; `title` is the
            # sixth. Slug last — a slug is a label, not a statement of the work.
            "what": _txt(it.get("goal") or it.get("title")) or slug,
            "status": _txt(it.get("status")).lower() or "pending",
        }


def _collect_subtask_update(s: dict, acc: dict) -> None:
    slug = _txt(s.get("slug"))
    if not slug:
        return
    slot = acc["subtasks"].setdefault(slug, {"what": slug, "status": "pending"})
    slot["status"] = _txt(s.get("status")).lower() or slot["status"]


def _collect_write(name, args, res, ok: bool, failed: bool, acc: dict) -> None:
    """Record a mutating write's touched paths. A FAILED write is pending work,
    not done, so it is skipped entirely; otherwise each new path lands in
    ``landed`` (confirmed) or ``attempted`` (can't vouch for it)."""
    if failed:
        return
    for path in _paths(name, args, res):
        if path in acc["landed"] or path in acc["attempted"]:
            continue
        key = "landed" if (ok or name == "wrote files") else "attempted"
        acc[key].append(path)


def _collect_tool(s: dict, acc: dict) -> None:
    """A tool step: a write that landed, a write we cannot vouch for, or a shell
    command worth replaying in the brief."""
    name = _txt(s.get("name"))
    args = s.get("args") if isinstance(s.get("args"), dict) else {}
    res = s.get("result")
    failed = isinstance(res, dict) and (res.get("ok") is False or res.get("error"))
    ok = isinstance(res, dict) and res.get("ok") is True
    if _mutates(name, args) or name == "wrote files":
        _collect_write(name, args, res, ok, failed, acc)
    elif name in _SHELL_TOOLS and ok:
        cmd = _txt(args.get("cmd") or args.get("command"))
        if cmd and cmd not in acc["commands"]:
            acc["commands"].append(cmd[:120])


_STEP_HANDLERS = {
    "error": _collect_error,
    "subtasks": _collect_subtasks,
    "subtask_update": _collect_subtask_update,
    "tool": _collect_tool,
}


def _collect(steps) -> dict:
    """What the stopped attempt actually did, from its persisted steps.

    One handler per step type, dispatched — the shape was previously a single
    body with five inlined branches, where the only thing they shared was the
    accumulator.
    """
    acc = _empty_collection()
    if not isinstance(steps, list):
        return acc
    for s in steps:
        if not isinstance(s, dict):
            continue
        stype = s.get("type")
        # A steer arrives as a "thought" with a role, not a type of its own.
        if stype == "thought" and s.get("role") == "steer":
            _collect_steer(s, acc)
            continue
        handler = _STEP_HANDLERS.get(stype)
        if handler:
            handler(s, acc)
    return acc


def _brief_block(title, items, cap, budget, body):
    """Append one titled, budget-bounded block of items to ``body`` (with an
    "…and N more" tail when truncated). Returns the remaining budget."""
    if not items or budget <= 0:
        return budget
    lines = [title]
    used = len(title) + 1
    shown = 0
    for it in items[:cap]:
        line = f"  - {it}"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if not shown:
        return budget
    left = len(items) - shown
    if left > 0:
        lines.append(f"  …and {left} more")
        used += 20
    
    body.append("\n".join(lines))
    return budget - used


def build_brief(row: dict, cwd: str = "") -> str:
    """The resume briefing for a stopped turn, or "" when there is nothing
    worth saying (a turn that stopped before doing anything is just a retry).

    ``cwd`` is accepted and unused: reading the workspace here is what produced
    the SPEC.md roadmap injection. Kept so callers need not care.
    """
    got = _collect(row.get("steps"))
    pending = [v["what"] for v in got["subtasks"].values()
               if v["status"] not in _DONE_STATUSES]
    done = [v["what"] for v in got["subtasks"].values()
            if v["status"] in _DONE_STATUSES]
    if not (got["landed"] or got["attempted"] or got["commands"]
            or pending or done or got["errors"] or got["steers"]):
        return ""

    head = ("[RESUME] Your previous attempt at this same request was "
            "interrupted before it finished. Its work is still on disk. "
            "Continue it — do not start over.")
    # The tail is the part that must NEVER be truncated away: the errors say
    # why it died and the rule says what to do. Truncating the assembled string
    # from the end deleted exactly these on the big runs that need them most,
    # so the LISTS are what gets trimmed, never the framing.
    tail_bits = []
    if got["steers"]:
        tail_bits.append("MID-RUN REDIRECTION — the user changed the request "
                         "while that attempt was running, and these still "
                         "apply (they override the words above):")
        tail_bits += [f"  - {t}" for t in got["steers"][-_MAX_ERRORS:]]
    if got["errors"]:
        tail_bits.append("It failed with:")
        tail_bits += [f"  - {e}" for e in got["errors"][:_MAX_ERRORS]]
    tail_bits.append(
        "Rules for this run: read the files above before rewriting them; keep "
        "work that is already correct; do ONLY what is still missing; if "
        "everything is in fact done, verify it and say so instead of redoing "
        "it.")
    tail = "\n".join(tail_bits)

    budget = _MAX_BRIEF_CHARS - len(head) - len(tail) - 4
    body: list = []


    budget = _brief_block("Already written (verify before touching again):",
           got["landed"], _MAX_FILES, budget, body)
    budget = _brief_block("Attempted, outcome unknown — CHECK these on disk before rewriting:",
           got["attempted"], _MAX_FILES, budget, body)
    budget = _brief_block("Subtasks already completed:", done, _MAX_PENDING, budget, body)
    budget = _brief_block("Subtasks still PENDING:", pending, _MAX_PENDING, budget, body)
    budget = _brief_block("Commands that already ran successfully:", got["commands"], 5, budget, body)

    return "\n".join([head, *body, tail])


def resume_preamble(rows: list, prompt: str, cwd: str = "",
                    *, forced: "bool | None" = None) -> str:
    """The brief to prepend to ``prompt``, or "" when this is not a resume.

    ``forced`` is tri-state on purpose. ``None`` = decide automatically: a
    retry is re-sending the same words after a stopped turn, which is exactly
    what the Retry button does. ``True`` = the user asked to resume after
    rephrasing. ``False`` = the user asked for a CLEAN rerun — the partial work
    may be junk they want abandoned, and without this there was no way to say
    so: every route to "run this again" meant "continue this".
    """
    if forced is False:
        return ""
    found = last_stopped_turn(rows)
    if not found:
        return ""
    row, prev_prompt = found
    if not forced and _txt(prompt) != _txt(prev_prompt):
        return ""
    return build_brief(row, cwd)


__all__ = ["last_stopped_turn", "build_brief", "resume_preamble"]
