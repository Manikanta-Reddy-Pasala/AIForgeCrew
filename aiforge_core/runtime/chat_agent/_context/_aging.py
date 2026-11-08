"""Old, large tool output leaves the prompt before it forces a condense.

A file read stays in the history at full size for the rest of the run, and in
a long task most of the prompt is reads the model finished with many steps ago.
Once an observation is more than a few messages old, its full text is saved
(``runtime.context_offload``) and the history keeps a head, a tail and the id:
``memory_lookup {"id": ...}`` or a fresh read brings it back. Reads,
searches and command output age (a command keeps its error lines); so do
edit results, which carry the changed lines (their errors are kept too).

It runs when the history reaches the condense point, as the first and cheap
stage (``force``): one rewrite, then the prompt only grows again, so a local
server keeps reusing what it already processed. ``AIFORGE_CHAT_AGE_OBS=1``
also ages every step, in bursts (the old way).
"""
from __future__ import annotations

import os

from .._shell import _ACTION_RE, _ARGS_RE

#: Output of these is re-readable from disk or the web, so ageing loses nothing.
_AGEABLE = frozenset({"file_read", "read_files", "read_lines", "grep", "find",
                      "list_dir", "web_fetch", "web_crawl", "git_diff",
                      "git_log", "git_blame", "codegraph_query",
                      "codegraph_explore"})
#: Output of these ages too, but its errors stay: the failing lines are what the
#: next step needs, and they are kept in full.
_AGEABLE_CMD = frozenset({"run_command", "run_tests", "run_shell", "typecheck",
                          "format", "command_output",
                          # an edit's result now carries the lines it changed
                          # (``now_reads``): re-readable, its errors kept
                          "file_patch", "multi_edit"})
_MARK = "[aged:"
#: Calls whose arguments are spent once they ran or applied: a script that
#: ran, a file that was written, an edit that landed. Their long string
#: arguments are what the model wrote, not what it needs to read again.
_SPENT_ARGS = frozenset({"run_command", "run_shell", "run_tests", "file_write",
                         "file_create", "file_patch", "multi_edit", "editor"})
_ARG_KEEP = 200
_ARG_MIN = 600
_HEAD, _TAIL = 700, 400
_CMD_HEAD, _CMD_TAIL, _CMD_ERR_LINES = 300, 900, 15
_ERR = __import__("re").compile(
    r"(?:Traceback|Error\b|Exception\b|FAILED|\berror:|AssertionError|\bE\s{2,})", __import__("re").I)


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_AGE_OBS", "0").strip().lower() \
        not in ("0", "false", "no", "off")


def _int_env(key: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(key, default)))
    except ValueError:
        return default


def _read_sig(prev: dict) -> "str | None":
    """The duplicate-read signature (``name|args``, as the loop keys it) of the
    call in this assistant message."""
    import json
    text = str(prev.get("content") or "")
    mt = _ACTION_RE.search(text)
    ma = _ARGS_RE.search(text)
    if not mt:
        return None
    try:
        args = json.loads(ma.group(1)) if ma else {}
    except ValueError:
        return None
    return mt.group(1) + "|" + json.dumps(args, sort_keys=True, default=str)


def _keep_by_chars(convo: list, keep_chars: int) -> int:
    """How many of the newest messages fit in ``keep_chars``."""
    total, n = 0, 0
    for m in reversed(convo[1:]):
        c = m.get("content") if isinstance(m, dict) else ""
        total += len(c) if isinstance(c, str) else len(str(c or ""))
        if total > keep_chars:
            break
        n += 1
    return max(n, 4)


def age_observations(convo: list, protect_from: "int | None" = None,
                     forget: "set | None" = None, *, force: bool = False,
                     keep_chars: "int | None" = None,
                     keep_msgs: "int | None" = None, dry: bool = False) -> int:
    """Shrink old, large read/search observations in place. Returns how many.

    ``protect_from``: messages at or after this index are results the model has
    not read yet (a queued batch) and are never aged. ``forget``: the loop's set
    of reads already done; an aged read leaves it, so reading the file again is
    allowed instead of being refused as a duplicate of text that is now a stub.
    ``force``: the condense point was reached — age now, whatever the switch
    and the burst size say, keeping the newest ``keep_chars`` of history whole.
    ``dry``: change nothing, save nothing; return how many characters the
    ageing WOULD free (every edit of an old message costs the model server a
    re-read of everything after it, so a caller weighs that first)."""
    if not convo or not (force or _on()):
        return 0
    keep = _int_env("AIFORGE_CHAT_AGE_KEEP", 10)       # newest messages untouched
    floor = _int_env("AIFORGE_CHAT_AGE_MIN_CHARS", 3000)
    if force:
        keep = _keep_by_chars(convo, keep_chars) if keep_chars else keep
        floor = min(floor, 1500)
    if keep_msgs is not None:
        # The user asked to compact: only the newest messages stay whole.
        keep = max(1, keep_msgs)
    from aiforge_core.runtime import context_offload
    # Age in BURSTS, not one message per step: every edit to an old message
    # changes the prompt from that point on, and a local server then re-reads
    # everything after it. A burst is one rebuild instead of many.
    burst = max(1, _int_env("AIFORGE_CHAT_AGE_BURST", 4))
    if not force and _candidates(convo, keep, floor) < burst:
        return 0
    aged = 0
    stop = max(1, len(convo) - keep)
    if protect_from is not None:
        stop = min(stop, max(1, protect_from))
    if force:
        aged += _age_spent_args(convo, stop, dry)
    for i in range(1, stop):
        m = convo[i]
        text = m.get("content") if isinstance(m, dict) else None
        if (m.get("role") != "user" or not isinstance(text, str)
                or not text.startswith("OBSERVATION:") or _MARK in text
                or len(text) < floor):
            continue
        prev = convo[i - 1]
        mt = _ACTION_RE.search(str(prev.get("content") or "")) \
            if prev.get("role") == "assistant" else None
        tool = mt.group(1).lower() if mt else ""
        if tool not in _AGEABLE and tool not in _AGEABLE_CMD:
            continue
        body = text[len("OBSERVATION:"):].strip()
        if tool in _AGEABLE_CMD:
            shrunk = _shrink_command(body)
        else:
            shrunk = f"{body[:_HEAD]}\n…\n{body[-_TAIL:]}"
        if dry:
            aged += max(0, len(text) - len(shrunk) - 120)
            continue
        oid = context_offload.save(text)
        if not oid:
            continue
        convo[i] = {**m, "content": (
            f"OBSERVATION: {_MARK} {tool} output, {len(body)} chars — "
            f'full text saved: memory_lookup {{"id": "{oid}"}}, or run it '
            f"again]\n{shrunk}")}
        aged += 1
        if forget is not None:
            sig = _read_sig(prev)
            if sig:
                forget.discard(sig)
    return aged


def _cut_strings(value):
    """``value`` with every long string cut to a head and a note; returns
    ``(new_value, chars_saved)``."""
    if isinstance(value, str):
        if len(value) < _ARG_MIN:
            return value, 0
        note = f"…[{len(value)} chars — already ran/applied]"
        return value[:_ARG_KEEP] + note, len(value) - _ARG_KEEP - len(note)
    if isinstance(value, list):
        out, saved = [], 0
        for v in value:
            nv, n = _cut_strings(v)
            out.append(nv)
            saved += n
        return out, saved
    if isinstance(value, dict):
        out, saved = {}, 0
        for k, v in value.items():
            nv, n = _cut_strings(v)
            out[k] = nv
            saved += n
        return out, saved
    return value, 0


def _age_spent_args(convo: list, stop: int, dry: bool) -> int:
    """At the condense point: the long arguments of old calls that already
    ran or applied (scripts, written files, edit text) are cut to a head.

    In a late click-completion prompt (341K chars) the model's own past
    messages were 38% of it -- 134 calls, mostly python probe scripts and
    edit text -- while ageing only ever shrank outputs. Cutting them here, on
    the same step as the output prune (one prompt re-read for both), frees
    far more per prune. Returns chars freed (``dry``) or calls cut."""
    import json as _json
    done = 0
    for i in range(1, stop):
        m = convo[i]
        text = m.get("content") if isinstance(m, dict) else None
        if m.get("role") != "assistant" or not isinstance(text, str) or len(text) < _ARG_MIN:
            continue
        mt = _ACTION_RE.search(text)
        if not mt or mt.group(1).lower() not in _SPENT_ARGS:
            continue
        ma = _ARGS_RE.search(text)
        if not ma:
            continue
        try:
            args = _json.loads(ma.group(1))
        except ValueError:
            continue
        new_args, saved = _cut_strings(args)
        if saved <= 0:
            continue
        if dry:
            done += saved
            continue
        new_text = text[:ma.start(1)] + _json.dumps(new_args, ensure_ascii=False) + text[ma.end(1):]
        convo[i] = {**m, "content": new_text}
        done += 1
    return done


def trim_superseded_writes(convo: list, path: str, cwd: str = "") -> int:
    """A whole-file write just landed on ``path``: the content of EARLIER
    whole-file writes to that same file is replaced text, so it is cut to a
    head now, not at the condense point.

    Live (click z1, 11-08): 7 file_write calls of 17-22K chars each - one
    file rewritten again and again - were 137K of a 463K prompt, carried by
    every later call. The current text is in the file (and in the latest
    write); a read brings back anything else. Returns calls cut."""
    import json as _json
    import os

    def _real(p):
        p = str(p or "")
        return os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd or ".", p)) if p else ""
    want = _real(path)
    if not want:
        return 0
    writes = []
    for i, m in enumerate(convo):
        text = m.get("content") if isinstance(m, dict) else None
        if m.get("role") != "assistant" or not isinstance(text, str) or len(text) < _ARG_MIN:
            continue
        mt = _ACTION_RE.search(text)
        if not mt or mt.group(1).lower() != "file_write":
            continue
        ma = _ARGS_RE.search(text)
        if not ma:
            continue
        try:
            args = _json.loads(ma.group(1))
        except ValueError:
            continue
        got = _real(args.get("path") or args.get("file_path"))
        if got and got == want:                   # the same file, not a same-named one
            writes.append((i, text, ma, args))
    done = 0
    for i, text, ma, args in writes[:-1]:         # the latest write stays whole
        new_args, saved = _cut_strings(args)
        if saved <= 0:
            continue
        convo[i] = {**convo[i], "content": text[:ma.start(1)]
                    + _json.dumps(new_args, ensure_ascii=False) + text[ma.end(1):]}
        done += 1
    return done


def _shrink_command(body: str) -> str:
    """Head, every error line (bounded), tail: the exit status and the failure
    survive, the long passing output does not."""
    lines = body.splitlines()
    errs = [ln for ln in lines if _ERR.search(ln)][:_CMD_ERR_LINES]
    out = [body[:_CMD_HEAD], "…"]
    if errs:
        out += ["[error lines]"] + errs + ["…"]
    out.append(body[-_CMD_TAIL:])
    return "\n".join(out)


def _candidates(convo: list, keep: int, floor: int) -> int:
    n = 0
    for i in range(1, max(1, len(convo) - keep)):
        m = convo[i]
        text = m.get("content") if isinstance(m, dict) else None
        if (m.get("role") == "user" and isinstance(text, str)
                and text.startswith("OBSERVATION:") and _MARK not in text
                and len(text) >= floor):
            prev = convo[i - 1]
            mt = _ACTION_RE.search(str(prev.get("content") or "")) \
                if prev.get("role") == "assistant" else None
            if mt and (mt.group(1).lower() in _AGEABLE
                       or mt.group(1).lower() in _AGEABLE_CMD):
                n += 1
    return n
