"""Before and after a tool call: the stall guard, plan and scope gates, the
workspace jail, and read/edit bookkeeping."""
from __future__ import annotations

import json
import os

from aiforge_core.runtime.stuck_policy import Policy

from .._context import (
    _post_edit_syntax_error,
    _progress_recap,
)
from .._preview import _diff_preview
from .._registry import (
    _FINALIZE_TOOLS,
    _READONLY_TOOLS,
)
from .._obs_text import render_observation
from ._post_edit import edited_files, post_edit_diagnostics, post_edit_format
from .._shell import _MAX_OBS, _MAX_OBS_READ, _READ_OBS_TOOLS
from ._approval import (
    _handle_rejection,
)
from ._stuck.detect import bump_identical, detect, identical_repeats, note_identical
from ._stuck.ladder import Pause, finish_stuck, nudge
from ._idle_steps import note_step, read_is_news
from ._outcomes import _note_green_tests, note_failure
from ._progress import (
    count_key,
    forgive,
    may_recover,
    note_command,
    note_read,
    note_write,
    strike,
)
from ._shared import (
    _ACTION_SIG_MAX,
)
from ._items import note_evidence, review_progress
from ._tasks import apply_progress, context_words, user_dropped_work


_WINDOW_READS = frozenset({"file_read", "read_lines"})
#: Share of a requested line range already shown since the last edit that
#: makes the read a repeat (click a15: lines 1727-1737 of core.py asked ~30
#: times each, shifted a line or two, 340 reads; an exact-match check saw
#: few of them).
_COVERED = 0.8
#: Skipped repeat reads in a row before the stuck recovery takes over.
_SKIP_STREAK = 3


def _window(name, args) -> "tuple[str, int, int] | None":
    """``(path, start, end)`` a ranged read asks for; None for a whole file."""
    if name not in _WINDOW_READS or not isinstance(args, dict):
        return None
    if not any(args.get(k) not in (None, "") for k in ("start", "end", "offset", "limit")):
        return None
    try:
        from .._tools._code import _line_range
        st_, en = _line_range(args, 10 ** 9)
    except Exception:  # noqa: BLE001
        return None
    path = str(args.get("path") or "")
    if not path or en == 10 ** 9 or en < st_:
        return None
    import os
    return os.path.normpath(path), st_, en


def _seen_lines(st, name, args) -> "tuple[set, int, int] | None":
    want = _window(name, args)
    wins = getattr(st, "read_windows", None)
    if not want or not wins:
        return None
    path, a, b = want
    seen = set()
    for sig, (p, x, y) in list(wins.items()):
        if p == path and sig in st.read_sigs_seen:
            seen.update(range(max(a, x), min(b, y) + 1))
    return seen, a, b


def _covered(st, name, args) -> bool:
    """The range was (almost all) shown already, since the last edit and
    still in the conversation (its read signature not forgotten)."""
    got = _seen_lines(st, name, args)
    return bool(got) and len(got[0]) >= _COVERED * (got[2] - got[1] + 1)


def _unseen(st, name, args) -> str:
    """The lines of the range not shown yet, as ``"101-120, 140"``."""
    got = _seen_lines(st, name, args)
    if not got:
        return ""
    seen, a, b = got
    out, run = [], None
    for i in range(a, b + 1):
        if i not in seen:
            run = [i, i] if run is None else [run[0], i]
        elif run:
            out.append(run)
            run = None
    if run:
        out.append(run)
    return ", ".join(f"{x}-{y}" if x != y else str(x) for x, y in out)


def _edit_paths(args) -> list:
    """The files an edit call names (``path``/``file``/``file_path``, and each
    of a multi_edit's ``edits``)."""
    if not isinstance(args, dict):
        return []
    out = [args.get(k) for k in ("path", "file", "file_path")]
    for e in args.get("edits") or []:
        if isinstance(e, dict):
            out += [e.get("path"), e.get("file"), e.get("file_path")]
    return [str(p) for p in out if p]


def _forget_after_failed_edit(st, name, args, result) -> None:
    """An edit that failed (the old text did not match) leaves the model
    needing the file's exact text again: its reads of that file stop counting
    as shown — ranged or whole — so a re-read is let through (sqlglot a5: 54
    failed patches, 54 re-reads skipped as 'already shown', 7/25)."""
    from aiforge_core.runtime.tools.mutating import FILE_WRITE_TOOLS
    if name not in FILE_WRITE_TOOLS or not isinstance(result, dict) or result.get("ok") is not False:
        return
    import os
    names = {os.path.basename(os.path.normpath(p)) for p in _edit_paths(args)}
    if not names:
        return                        # cannot tell which file: keep the rest
    wins = getattr(st, "read_windows", None) or {}
    for sig, (p, _a, _b) in list(wins.items()):
        if os.path.basename(p) in names:
            wins.pop(sig, None)
            st.read_sigs_seen.discard(sig)
    for sig in list(st.read_sigs_seen):       # whole-file reads have no window
        if any(n in str(sig) for n in names):
            st.read_sigs_seen.discard(sig)


def _log_read(st, name, args) -> bool:
    """A read of a log or a job's output file: it grows while a job runs, so
    reading it again is not a duplicate."""
    try:
        from ._probe_guard import _READS, _watching
        return name in _READS and _watching(st, name, args)
    except Exception:  # noqa: BLE001
        return False


def _action_stall_guard(st, name, args, sig, _long_chain_help):
    """Stall guard for an action: short-circuit a duplicate read with a progress
    recap, and on the same tool+args repeated too often first recover with a
    recap+nudge (bounded), else pause for the user. Returns continue/return/None."""
    # Bounded, LEAST-RECENTLY-USED first. An uncapped turn (cap 0) removes
    # the 2000-step ceiling that used to bound this table in practice, and
    # nothing else prunes it (convo is condensed, recent_outputs is a maxlen
    # deque, read_sigs_seen is cleared on compaction).
    #
    # The order matters more than the bound: a plain dict is ordered by
    # FIRST insert, so dropping "the oldest half" would evict exactly the
    # signatures that have been repeating longest — the ones accumulating
    # strikes — and the stall guard would never fire on the very runaway
    # this table exists to catch. move_to_end on each touch makes the
    # eviction least-recently-SEEN instead.
    from ._waiting import waiting
    if waiting(st, name, args):
        return None                   # a check on a job still running: not a repeat
    key = count_key(name, sig)
    st.action_counts[key] = st.action_counts.get(key, 0) + 1
    st.action_counts.move_to_end(key)
    while len(st.action_counts) > _ACTION_SIG_MAX:
        st.action_counts.popitem(last=False)
    # Duplicate-READ short-circuit: a local model on a long sweep re-issues a
    # read it already ran (its result is still above in the convo). Don't
    # re-execute it — hand back a cheap progress recap that points at the
    # next unread file / the write step, so every read must make NEW
    # progress. Cleared on any edit (a file just written is worth re-reading).
    # Disabled with the same env switch as the recovery nudge
    # (AIFORGE_CHAT_STUCK_RECOVERIES=0 → full legacy behaviour). A skipped
    # read counts only toward the lifetime backstop, so a model that keeps
    # re-asking between new reads is still stopped eventually.
    from ._probe_guard import _shell_tools, is_shell_read
    shell_read = is_shell_read(args, name)
    shell_seen = getattr(st, "shell_read_sigs", None) or set()
    if name in _shell_tools() and not shell_read and shell_seen:
        # any other shell command may have changed files (sed -i, git
        # checkout, a codegen script): earlier shell reads are stale
        shell_seen.clear()
    duplicate = (_long_chain_help and (
        (name in _READ_OBS_TOOLS
         and (sig in st.read_sigs_seen or _covered(st, name, args))
         and not _log_read(st, name, args))
        or (shell_read and sig in shell_seen)))
    signal = detect("action", st, sig, strike(st, sig, per_state=not duplicate),
                    duplicate)
    looping = signal.detail if signal else ""
    st.dup_skip_streak = (getattr(st, "dup_skip_streak", 0) + 1) if duplicate else 0
    if duplicate and not looping and st.dup_skip_streak > _SKIP_STREAK:
        # skipped reads in a row: the plain "skipped" note is not working —
        # the stuck recovery (recap, change of approach, else a pause) takes over
        looping = (f"asked {st.dup_skip_streak} times in a row for reads whose "
                   "result it already has")
        st.dup_skip_streak = 0          # one recovery per run of skips
    if duplicate and not looping:
        _recap = _progress_recap(st.convo)
        _rest = "" if sig in st.read_sigs_seen else _unseen(st, name, args)
        yield from nudge(
            st, f"⏭ duplicate read skipped ({name})",
            ("OBSERVATION: [skipped — duplicate] You ALREADY ran this exact "
             "read; its result is above and re-reading wastes a step. "
             if sig in st.read_sigs_seen else
             "OBSERVATION: [skipped — already shown] These lines are above, "
             "from an earlier read of this file; re-reading wastes a step. "
             + (f"Lines {_rest} are not shown yet: read just those if you need "
                "them. " if _rest else ""))
            + (_recap + ". " if _recap else "")
            + "Read a DIFFERENT file you have not read yet, or if you have "
            "enough, WRITE your output now (file_write) or emit FINAL.")
        return "continue"
    if looping:
        # A local model on a long chain re-issues an action it already ran —
        # most often re-reading a file it read earlier (it lost track over the
        # growing history), which the old hard bail turned into an abandoned
        # task. Recover FIRST: recap what's already done + point at the next
        # step (bounded); only give up if the model keeps repeating. Repeats
        # are counted per workspace state, so re-running a check after a real
        # change is not a repeat.
        # The identical-result run beats the recovery budget, which a moving
        # workspace fingerprint can refill forever: a call that has returned the
        # same thing this many times in a row goes straight to a change of
        # approach (the budget of those is bounded).
        _ident_limit = Policy.load().identical_repeats
        _bump = identical_repeats(st, sig) >= _ident_limit
        if _bump:
            bump_identical(st, sig)
        if identical_repeats(st, sig) < 2 * _ident_limit and may_recover(st):
            forgive(st, sig)
            yield from nudge(
                st, f"↺ repeated `{name}` — recap + nudge to continue",
                _loop_nudge(name, looping, _progress_recap(st.convo)))
            return "continue"
        _r = yield from finish_stuck(
            st, f"You keep repeating `{name}` without progress.",
            Pause(f"I keep trying the same step (`{name}`) without "
                  "progress. I've paused — could you clarify or tell "
                  "me how you'd like me to proceed?"))
        if _r == "continue":
            forgive(st, sig)
        return _r
    return None


def _safely(fn, *args):
    """Progress bookkeeping must never end a turn (an odd path, a vanished
    file). A failed note simply does not count."""
    try:
        return fn(*args)
    except (OSError, ValueError, TypeError):
        return False


def _loop_nudge(name, reason, recap) -> str:
    if reason == "often":
        return (f"[loop guard — not the user] You have run `{name}` many times "
                "in this run without finishing. Step back: say what the recent "
                "results have in common, then try a different approach — or, "
                "if you are blocked, finish with FINAL and say what blocks you. "
                + (recap + "." if recap else ""))
    return (f"[loop guard — not the user] You already ran `{name}` with "
            "these exact args and its result is ABOVE — repeating it makes "
            "no progress. "
            + (recap + ". " if recap else "")
            + "Do the NEXT, DIFFERENT step now: act on something not yet "
            "done (e.g. the next unread file from the request), or output "
            "`FINAL: <answer>` if everything is complete. Do NOT repeat a "
            "previous action.")


#: Tools on the read-only list that write anyway (files in the shared work
#: folder, a memory note, an index): off in a temporary chat.
_TEMP_CHAT_BLOCKED = frozenset({"context_gather", "web_crawl",
                                # a background watch outlives the turn
                                "gitlab_pipeline_watch"})
#: Read tools that save attachments by default: run without them there.
_TEMP_CHAT_NO_ATTACHMENTS = frozenset({"jira_read", "confluence_read"})



def _note_decisions(st, args, result, before) -> None:
    """The model's own course changes on its board, for the Run overview
    (runtime/chat_decisions): an item it added that nobody asked for, one it
    skipped, failed or took off, and the ``decision`` line it gave."""
    try:
        from aiforge_core.runtime import chat_decisions
        sid = getattr(st, "session_id", None)
        why = " ".join(str(args.get("decision") or "").split())
        if not result.get("ok"):
            # A call the board refused still carries the choice it reports.
            if why:
                chat_decisions.record(sid, why, by="agent", kind="choice")
            return
        board = st.board
        slug = result.get("slug")
        it = board.get(slug) if slug else None
        title = (it or {}).get("title") or ""
        lines = []
        if it is not None and slug not in before and it.get("self_added"):
            lines.append(("plan", f"Agent added a task of its own — {slug}: {title}"))
        elif it is not None and before.get(slug) != it.get("status") \
                and it.get("status") in ("skipped", "failed"):
            lines.append(("skip" if it["status"] == "skipped" else "fail",
                          f"Agent marked {slug} {it['status']}: {title}"))
        if result.get("removed") and slug in before and slug not in board:
            lines.append(("plan", f"Agent took {slug} off the task list"))
        if why:
            lines.append(("choice", why + (f" ({slug})" if slug else "")))
        for kind, text in lines:
            chat_decisions.record(sid, text, by="agent", kind=kind, slug=slug or "")
    except Exception:  # noqa: BLE001 — the overview never breaks a step
        pass

def _pre_dispatch_gates(st, name, args, readonly_mode, analyze_mode):
    """Pre-dispatch bookkeeping gates: plan_progress flips a UI subtask (pure
    bookkeeping, allowed in every mode); read-only Plan/Analyze mode blocks a
    mutating tool. Returns "handled" (bookkeeping done, nothing refused),
    continue, or None."""
    if isinstance(args, dict) and "_invalid_args" in args:
        # The model's arguments did not parse (cut off, malformed JSON). Its
        # own tool says so and the model sends them again — the turn stays on
        # native tool calling (a switch to the text protocol was a different
        # prompt: no prefix cache, and a ~40K catalog to read).
        result = {"ok": False, "error": "invalid_arguments",
                  "detail": f"The arguments of your {name} call are not valid JSON "
                            f"(cut off or malformed): {args['_invalid_args']}. Call "
                            f"{name} again with complete, valid JSON arguments; for a "
                            "long file, write it in parts or patch it."}
        yield {"type": "tool", "name": name, "args": {}, "result": result}
        st.convo.append({"role": "user", "content": f"OBSERVATION: {json.dumps(result)}"})
        return "handled"
    # Simple-mode task tracker: plan_progress flips a checklist item in
    # the UI's subtasks dock. Pure bookkeeping — no side effects, allowed
    # in every mode (incl. plan), never gated.
    if name == "tool_help":
        wanted = str((args or {}).get("name") or "").strip()
        result = {
            "ok": bool(wanted),
            "added": wanted,
            "note": (f"{wanted} is available on your next step. Call it directly."
                     if wanted else "Pass the tool's exact name."),
        }
        yield {"type": "tool", "name": name, "args": args, "result": result}
        st.convo.append({"role": "user",
                         "content": f"OBSERVATION: {json.dumps(result)}"})
        return "handled"

    if name == "plan_progress" and _repo_roles_call(args) and str(
            (args or {}).get("slug") or "repos") != "repos":
        # Roles on a board item: the model scoping one step, not the chat's
        # record (it flipped whole repositories to read-only that way). Only
        # the "repos" item records roles.
        args = {k: v for k, v in args.items() if not _is_role_key(k)}
    elif name == "plan_progress" and _repo_roles_call(args):
        result = _record_repo_roles(st, args)
        if result.get("ok") and result.get("changed"):
            yield {"type": "thought", "role": "system",
                   "text": "📌 Repositories for this chat — " + result["note"].split(": ", 1)[-1]}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        st.convo.append({"role": "user",
                         "content": f"OBSERVATION: {json.dumps(result)}"})
        return "handled"

    if name == "plan_progress":
        # A part of the request comes off the board only when the user
        # dropped it: a message arrived mid-run.
        # A part of the user's request is never taken off by the model: it is
        # marked skipped, so the user sees what happened to it. (The harness's
        # own revise for a message the user sent mid-run removes dropped work.)
        # "_"-keys and ``by_harness`` are the harness's own flags (``_harness``
        # closes a target item, ``by_harness`` keeps an item off the scope
        # net); the model's call never carries them.
        args = {k: v for k, v in args.items()
                if not str(k).startswith("_") and k != "by_harness"}
        _before = {s: it.get("status") for s, it in st.board.items()}
        result, events = apply_progress(
            st.board, args,
            # A message the user sent mid-run may drop a part of the request:
            # then the model may take it off (or skip it) as the user asked.
            allow_request_removal=user_dropped_work(getattr(st, "steers", None))
            or bool(getattr(st, "user_dropped_goal", False)),
            context_words=context_words(st.board, getattr(st, "goal", ""),
                                        getattr(st, "steers", None)))
        from ._section_check import check_back
        result, events = check_back(st, result, events, _before)
        result, events = review_progress(st, result, events)
        events = _with_evidence(st, result, events, _before)
        if not readonly_mode:
            _note_decisions(st, args, result, _before)
        st.board_used = st.board_used or any(
            ev["type"] == "subtasks" for ev in events)
        # The model is working the board (also an item that came from the
        # request's own parts): a FINAL that leaves any item open is premature.
        if result.get("ok") and (result.get("slug") in st.board
                                 or result.get("removed") or result.get("order")):
            st.board_touched = True
        if readonly_mode:
            # Plan mode's panel holds the planner's steps; a whole-list
            # event would replace them.
            events = ([{"type": "subtask_update", "slug": result.get("slug"),
                        "status": result.get("status")}]
                      if result.get("ok") and result.get("status") else [])
        yield from events
        yield {"type": "tool", "name": name, "args": args, "result": result}
        st.convo.append({"role": "user",
                      "content": f"OBSERVATION: {json.dumps(result)}"})
        return "handled"

    # A temporary chat: read tools that still WRITE something (context_gather
    # saves dossiers and a memory note, web_crawl a page dossier) are not
    # available either; Jira/Confluence reads skip the attachments they save.
    if name in _TEMP_CHAT_BLOCKED or name in _TEMP_CHAT_NO_ATTACHMENTS:
        try:
            from aiforge_core.runtime import chat_private
            temp = chat_private.is_temporary(getattr(st, "session_id", None))
        except Exception:  # noqa: BLE001
            temp = False
        if temp and name in _TEMP_CHAT_NO_ATTACHMENTS:
            if isinstance(args, dict):
                args["attachments"] = "false"
                args.pop("images", None)
            temp = False
        if temp:
            result = {"ok": False, "blocked": "temporary_chat", "error": (
                f"'{name}' saves what it gathers or keeps running after the turn, "
                "and this is a TEMPORARY chat that "
                "keeps nothing. Read the ticket/page/files directly (web_fetch for a web "
                "page, gitlab_pipeline to check a pipeline once) instead.")}
            yield {"type": "tool", "name": name, "args": args, "result": result}
            st.convo.append({"role": "user",
                             "content": f"OBSERVATION: {json.dumps(result)}"})
            return "continue"

    # PLAN/ANALYZE mode (#2): block mutating tools — read-only only.
    if readonly_mode and name not in _READONLY_TOOLS:
        _mname = "Analyze" if analyze_mode else "Plan"
        _mtail = ("Report your FINDINGS." if analyze_mode
                  else "Finish with a PLAN; the user will switch to Act "
                       "mode to execute it.")
        result = {"ok": False, "blocked": "plan_mode",
                  "error": f"'{name}' is blocked in {_mname} mode "
                           f"(read-only). {_mtail}"}
        try:
            from aiforge_core.runtime import chat_private
            if chat_private.is_temporary(getattr(st, "session_id", None)):
                result["error"] = (
                    f"'{name}' is not available: this is a TEMPORARY chat — it only "
                    "reads and keeps nothing; no file is written, no command changes "
                    "anything, nothing is saved. Tell the user plainly that this "
                    "request needs a normal chat, and answer what can be answered "
                    "by reading.")
        except Exception:  # noqa: BLE001
            pass
        yield {"type": "tool", "name": name, "args": args, "result": result}
        st.convo.append({"role": "user",
                      "content": f"OBSERVATION: {json.dumps(result)}"})
        return "continue"
    return None


def _ask_write_grant(st, name, args, cwd, jailed):
    """Ask the user to let this chat write outside its workspace. Approve →
    the folder (see chat_write_grants.grant_root) is granted for the rest of
    the session and the call proceeds (returns None). A target that is ``/``,
    the home directory or above it is approved for THIS call only, never
    granted. Reject/expire → the usual rejection handling ("continue"/"return")."""
    from aiforge_core.runtime import chat_approve, chat_write_grants
    roots: list[str] = []
    once: list[str] = []
    for raw in jailed:
        target = os.path.join(cwd or "", str(raw))
        r = chat_write_grants.grant_root(target)
        if r is None:
            once.append(os.path.realpath(target))
        elif r not in roots:
            roots.append(r)
    if once:
        reason = ("Allow this ONE write to " + ", ".join(once) + "? It is outside "
                  "the chat's folder (" + str(cwd) + ") and too broad to allow for "
                  "the rest of the chat.")
    else:
        reason = ("Allow this chat to write in " + ", ".join(roots) + "? It is "
                  "outside the chat's folder (" + str(cwd) + "). Allowing covers "
                  "the rest of this chat.")
    seq = chat_approve.request(st.session_id)
    yield {"type": "approval", "id": seq, "name": name, "args": args,
           "grant_roots": roots, "reason": reason,
           "preview": _diff_preview(name, args, cwd)}
    decision = chat_approve.wait(st.session_id)
    if decision.get("note") == "approval timed out":
        yield {"type": "approval_expired", "id": seq, "name": name}
    if decision.get("decision") != "approve":
        return (yield from _handle_rejection(
            name, args, st.session_id, st.convo, decision))
    if roots and not once:
        chat_write_grants.grant(st.session_id, roots)
        st.user_roots = list(getattr(st, "user_roots", ()) or ()) + roots
    return None


def _team_repo_guard(st, name, args, cwd):
    """In a team run the user's REAL checkout is not the run's to change: a
    shell command that WRITES there (``cd <repo> && …``, ``git -C <repo>
    commit``, ``sed -i``/``tee``/``cp``/``mv``/``rm``/a redirect targeting
    it) is refused with a pointer to the worktree. Reads and copies FROM the
    repo, and writes anywhere else (toolchain installs), are left alone; a
    repo the user explicitly granted is not guarded. Returns "continue" to
    skip the call, else None."""
    try:
        from aiforge_core.runtime import team_run_life, team_workspace
        ws = team_workspace.for_cwd(cwd)
        if ws is None or any(
                team_run_life.fold(r) == team_run_life.fold(ws.repo)
                for r in _explicit_grants(getattr(st, "session_id", None))):
            return None
        hits = team_run_life.repo_writes(cwd, name, args or {})
    except Exception:  # noqa: BLE001 — never break dispatch
        return None
    if not hits:
        return None
    result = {
        "ok": False, "error": "writes_users_checkout", "blocked_paths": hits,
        "hint": (f"Refused: this command enters or changes the user's checkout "
                 f"{ws.repo}. This team run works in its own worktree "
                 f"{ws.cwd} — run the command there (use paths under "
                 f"{ws.cwd}, or relative paths). Reading or copying FROM "
                 f"{ws.repo} is fine."),
    }
    yield {"type": "tool", "name": name, "args": args, "result": result}
    st.convo.append({"role": "user",
                     "content": f"OBSERVATION: {json.dumps(result)}"})
    return "continue"


def _with_evidence(st, result, events, before) -> list:
    """An item that just closed carries what the run did while it was open
    (:mod:`._evidence`); the dock's events carry it to the UI."""
    try:
        from ._evidence import on_status
        slug = result.get("slug") if isinstance(result, dict) else None
        if slug in st.board and before.get(slug) in ("done", "failed") \
                and st.board[slug]["status"] not in ("done", "failed"):
            # Opened again — also when the close the model tried was refused:
            # the evidence of its earlier close no longer stands.
            from ._evidence import forget
            forget(st.board[slug])
        if not (result.get("ok") and slug in st.board):
            return events
        ev = on_status(st, slug, before.get(slug), st.board[slug]["status"])
        if ev is None:
            return events
        # Evidence travels only on its item's subtask_update (a whole-list
        # event would resend every item's evidence on each board change).
        out = [{**e, "evidence": ev}
               if e.get("type") == "subtask_update" and e.get("slug") == slug
               else e for e in events]
        if not any(e.get("type") == "subtask_update" and e.get("slug") == slug
                   for e in out):
            out.append({"type": "subtask_update", "slug": slug,
                        "status": st.board[slug]["status"], "evidence": ev})
        return out
    except Exception:  # noqa: BLE001 — evidence must never break a turn
        return events


def _repo_roles_call(args) -> bool:
    from aiforge_core.runtime import repo_roles
    return repo_roles.enabled() and (repo_roles.has_roles(args) or str(
        (args or {}).get("slug") or "") == repo_roles.SLUG)


def _is_role_key(key: str) -> bool:
    from aiforge_core.runtime import repo_roles
    return key in repo_roles.WORK_KEYS + repo_roles.READ_KEYS


#: A side task's request starts with its parent chat's recent messages
#: (api/routes/_chat/_side_tasks._context_preamble): the model's words, not
#: the user's. Only what follows this marker is the task.
_SIDE_TASK_MARK = "\n\nThe side task:\n"


def _said(st) -> str:
    """The user's words in this turn: the request and any message sent mid-run."""
    goal = str(getattr(st, "goal", "") or "")
    if goal.startswith("This is a SIDE TASK") and _SIDE_TASK_MARK in goal:
        # The task text may be the parent AGENT's (spawn_task), not the user's.
        goal = ""
    return "\n".join([goal] + [str(s) for s in (getattr(st, "steers", None) or [])])


def _record_repo_roles(st, args) -> dict:
    """plan_progress {"slug": "repos", "work_in": [...], "read_only": [...]}:
    the chat's repository roles (runtime/repo_roles)."""
    from aiforge_core.runtime import chat_private, repo_roles
    if chat_private.is_temporary(getattr(st, "session_id", None)):
        return {"ok": False, "error": "a temporary chat only reads: no folders or "
                "branches are set up for work"}
    if not repo_roles.has_roles(args):
        r = repo_roles.roles(getattr(st, "session_id", None))
        return {"ok": False, "repos": r, "error": (
            "Pass the folders: plan_progress {\"slug\": \"repos\", \"work_in\": "
            "[<folders to change>], \"read_only\": [<source / reference folders>]}. "
            "Recorded now: " + repo_roles.summary(r))}
    try:
        return repo_roles.record(getattr(st, "session_id", None), args,
                                 getattr(st, "cwd", None), _said(st),
                                 getattr(st, "user_roots", None) or ())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"repo roles not recorded: {exc}"}


def _git_target_gate(st, name, args, cwd):
    """A call that would write in a repository this chat only reads, or in
    one it does not work in (runtime/repo_roles), or a git command that would
    land in the wrong repository or on the wrong branch (runtime/git_target),
    is refused before it runs, and before any approval is asked for it.
    Returns "continue" to skip the call, else None."""
    session_id = getattr(st, "session_id", None)
    if session_id is None:
        return None
    _seen = 1 + len(getattr(st, "steers", None) or [])
    if getattr(st, "roles_allow_checked", 0) != _seen:
        # "allow edits in calc-java": the harness moves the folder itself, at
        # the first call of the turn and after each message sent mid-run (the
        # model may get its own call wrong).
        st.roles_allow_checked = _seen
        try:
            from aiforge_core.runtime import repo_roles
            moved = repo_roles.allow_from_user(session_id, _said(st))
        except Exception:  # noqa: BLE001
            moved = []
        if moved:
            text = ("📌 You allowed changes in " + ", ".join(moved)
                    + " — it is now a folder this chat works in.")
            yield {"type": "thought", "role": "system", "text": text}
            st.convo.append({"role": "user", "content": (
                "[harness — not the user] " + text + " Writes and commits there are "
                "allowed from now on.")})
    try:
        from aiforge_core.runtime import git_target, repo_roles, shell_writes
        result = repo_roles.check(session_id, name, args or {}, cwd,
                                  getattr(st, "user_roots", None) or (), _said(st))
        if result is None and name in shell_writes.SHELL_TOOLS:
            cmd = shell_writes.command_of(args or {})
            said = _said(st)
            if "git" in cmd:
                # A branch the user named in an earlier turn counts too.
                if getattr(st, "_user_history", None) is None:
                    st._user_history = repo_roles._user_history(session_id) \
                        if not repo_roles.is_child(session_id) else ""
                said = said + "\n" + st._user_history
            result = git_target.check(session_id, cmd, _command_base(cwd), said)
    except Exception:  # noqa: BLE001 — never break dispatch
        return None
    if result is None:
        return None
    try:
        from aiforge_core.runtime import repo_roles
        folder = repo_roles.refused_folder(result)
        if folder:
            st.roles_refused = list(getattr(st, "roles_refused", None) or []) + [folder]
    except Exception:  # noqa: BLE001
        pass
    key = (result.get("blocked"), result.get("reason"),
           result.get("folder") or str(result.get("error") or "")[:80])
    seen = dict(getattr(st, "refusals_seen", None) or {})
    seen[key] = seen.get(key, 0) + 1
    st.refusals_seen = seen
    if seen[key] >= 2:
        result = {**result, "repeated": seen[key], "hint": (
            f"This has been refused {seen[key]} times and will not run, however it is "
            "worded. If the work is done, finish with FINAL; otherwise do what the "
            "refusal says or ask the user.")}
    yield {"type": "tool", "name": name, "args": args, "result": result}
    st.convo.append({"role": "user",
                     "content": f"OBSERVATION: {json.dumps(result)}"})
    if seen[key] >= 4:
        _r = yield from finish_stuck(
            st, "The same refused step was tried again and again.",
            Pause("I kept trying a step the harness refuses: "
                  + str(result.get("error") or "")[:200]
                  + " I've paused — tell me how you'd like to go on."))
        return _r if _r in ("return", "continue") else "return"
    return "continue"


def _command_base(cwd) -> str:
    """The folder run_command runs a command in."""
    from aiforge_core.runtime.git_target import command_base
    return command_base(cwd)


def _explicit_grants(session_id) -> list:
    from aiforge_core.runtime import chat_write_grants
    return chat_write_grants.granted(session_id) if session_id is not None else []


def _approvals_on(st) -> bool:
    """This chat asks before acting (its own toggle, else the mode's default)."""
    try:
        from aiforge_core.runtime import chat_approve
        return chat_approve.approvals_required(getattr(st, "session_id", None))
    except Exception:  # noqa: BLE001 — unknown: ask, as before
        return True


def _workspace_jail(st, name, args, cwd):
    """Workspace jail (on by default). The session's cwd is otherwise only a
    DEFAULT: an absolute path in a mutating file tool (or, in a chat, a shell
    write) lands anywhere, and an off-topic recall must never turn into an edit
    in a repo the user never brought into this chat. Interactive: ASK the user
    (one click grants the folder for the rest of this chat). Unattended: refuse
    without writing. Returns "continue"/"return" to skip the call, else None."""
    # A shell write is only parsed when there is someone to ask (best-effort
    # parsing: a false positive is an Allow prompt, never a hard block).
    interactive = getattr(st, "session_id", None) is not None and _approvals_on(st)
    blocked = yield from _team_repo_guard(st, name, args, cwd)
    if blocked is not None:
        return blocked
    try:
        from aiforge_core.runtime import scope_guard as _sg_jail
        _roots = list(getattr(st, "user_roots", ()) or ())
        _jailed = _sg_jail.outside_workspace(
            name, args or {}, cwd, _roots, include_shell=interactive)
    except Exception:  # noqa: BLE001 — never break dispatch
        _jailed, _roots = [], []
    if not _jailed:
        return None
    if interactive:
        return (yield from _ask_write_grant(st, name, args, cwd, _jailed))
    # Unattended, or approvals off for this chat: refuse at once. Asking here
    # waited out the 15-minute approval timeout with nobody to answer (a test
    # file written one folder above the repo stalled a 3-minute task for 15).
    _allowed = [cwd] + _roots
    result = {
        "ok": False, "error": "outside_workspace",
        "blocked_paths": _jailed, "allowed_folders": _allowed,
        "hint": ("Write refused: this run may only write in "
                 + ", ".join(map(str, _allowed)) + ". Do NOT write there "
                 "another way (a shell redirect, cp, mv); do the work "
                 "inside those folders, or report that it needs a folder "
                 "outside them: the user can allow one by naming it in their "
                 "next message, or by turning approvals on for this chat."),
    }
    yield {"type": "tool", "name": name, "args": args, "result": result}
    st.convo.append({"role": "user",
                     "content": f"OBSERVATION: {json.dumps(result)}"})
    return "continue"


def _pre_tool_checks(st, name, args, cwd, _scope_globs):
    """PreToolUse hook block, workspace jail and autonomous scope-allowlist
    enforcement. Returns "continue"/"return" when the call must not run (a
    refused or rejected write); otherwise the hook-block dict (or None) for
    dispatch."""
    # Lifecycle hook (Claude Code parity): PreToolUse can block a tool
    # (a `block_on_nonzero` hook that exits non-zero) — surface it like the
    # plan-mode/policy blocks. Hooks soft-fail; a hooks error never breaks
    # the turn.
    _hook_block = None
    try:
        from aiforge_core.runtime import hooks as _hooks
        _pre = _hooks.fire("PreToolUse", {"tool": name, "args": args}, cwd)
        _hooks.note_into(st.convo, "PreToolUse", _pre)
        if _pre.get("blocked"):
            _hook_block = _pre
    except Exception:  # noqa: BLE001 — hooks must never break dispatch
        _hook_block = None

    # A call a PreToolUse hook already blocked never runs — do not ask for
    # (and persist) a write grant it will not use.
    if _hook_block is None:
        _sig = yield from _workspace_jail(st, name, args, cwd)
        if _sig is not None:
            return _sig

    # Scope allowlist enforcement (autonomous Doer path). Reject a
    # mutating file tool whose resolved target path is outside the
    # ticket's scope_allowlist_globs — refuse WITHOUT writing, and hand
    # the model a corrective observation. Reuses scope_guard's matcher
    # so the text path enforces exactly like the native callback.
    if _scope_globs:
        try:
            from aiforge_core.runtime import scope_guard as _sg
            _off = [p for p in _sg._path_from_args(name, args or {})
                    if not _sg._matches_any(p, _scope_globs)]
        except Exception:  # noqa: BLE001 — never break dispatch
            _off = []
        if _off:
            result = {
                "ok": False, "error": "scope_violation",
                "blocked_paths": _off,
                "scope_allowlist_globs": _scope_globs,
                "hint": ("Edit refused: path is outside the ticket's "
                         "scope_allowlist_globs. Edit only files inside "
                         "an allowed glob."),
            }
            yield {"type": "tool", "name": name, "args": args,
                   "result": result}
            st.convo.append({"role": "user",
                          "content": f"OBSERVATION: {json.dumps(result)}"})
            return "continue"
    return _hook_block


def _record_edit(st, name, args, result, cwd):
    """When an edit tool landed: format the files (project's formatter), bump
    the edit counter, invalidate the duplicate-read guard, and run the post-
    edit checks — syntax, then the language's error check — that surface +
    feed back any error this step. Yields the events."""
    _check = os.environ.get("AIFORGE_CHAT_POST_EDIT_CHECK", "1") not in ("0", "false")
    files = edited_files(name, args, result, cwd) if _check else []
    # Formatted before note_write hashes the files: the progress state sees
    # the file as it stays.
    formatted = list(post_edit_format(st, files, cwd))
    if _safely(note_write, st, name, args, result, cwd):
        st.edits_made += 1
        st.read_sigs_seen.clear()   # a file just changed → re-reads are valid again
        if getattr(st, "shell_read_sigs", None):
            st.shell_read_sigs.clear()
        if name == "file_write" and isinstance(args, dict) and args.get("path"):
            # earlier whole-file writes to this file are replaced text: cut them
            from .._context._aging import trim_superseded_writes
            _safely(trim_superseded_writes, st.convo, args["path"], cwd)
        yield from formatted
        # D: post-edit self-check. Immediately syntax-check the file just
        # written and, if broken, hand the model the error THIS step (tight
        # feedback) instead of letting it surface only at the end-of-run test
        # gate. Best-effort + opt-out (AIFORGE_CHAT_POST_EDIT_CHECK=0).
        if _check:
            try:
                _pe = _post_edit_syntax_error(name, args, cwd)
            except Exception:  # noqa: BLE001
                _pe = None
            if _pe:
                yield {"type": "thought", "role": "system",
                       "text": f"⚠ syntax error in the file you just edited "
                               f"— fix it now: {_pe[:160]}"}
                from ._notes import hold
                hold(st, "[automated syntax check — not the user] The file you just "
                         f"wrote has a syntax error; fix it before continuing:\n{_pe[:600]}")
            else:
                yield from post_edit_diagnostics(st, files, cwd)
                from . import _structure_guard
                yield from _structure_guard.check(st, name, files, args, cwd)
                from . import _test_guard
                yield from _test_guard.check(st, name, files, args, cwd)
                yield from _test_guard.unasked_new_tests(st, files, cwd)
                from . import _stub_note
                yield from _stub_note.check(st, files, cwd)

def _git_target_after(st, name, args, result, cwd) -> str:
    from aiforge_core.runtime import git_target
    return git_target.after(getattr(st, "session_id", None), name, args, result, cwd)


def _record_read(st, name, sig, result, _long_chain_help, args=None):
    """Count a landed READ as new progress the first time its signature is seen
    (feeds the extension budget), and remember it for the duplicate-read guard."""
    from ._probe_guard import is_shell_read as _shell_read
    shell_read = name not in _READ_OBS_TOOLS and _shell_read(args, name)
    if ((name in _READ_OBS_TOOLS or shell_read) and not (
            isinstance(result, dict) and result.get("ok") is False)):
        # F2: counted OUTSIDE the _long_chain_help gate —
        # AIFORGE_CHAT_STUCK_RECOVERIES=0 turns off the duplicate-read
        # NUDGE, and must not silently delete half the progress signal with
        # it (a read-only research turn would never extend on that box).
        if sig not in st.read_sigs_ever:
            # Real progress is knowledge it did not have: a call it had not
            # made that returns lines it had not seen. New ARGUMENTS alone
            # (the same file, another line range) are not.
            if read_is_news(st, result):
                st.reads_new += 1
            st.read_sigs_ever[sig] = True
            while len(st.read_sigs_ever) > _ACTION_SIG_MAX:
                st.read_sigs_ever.popitem(last=False)
        if _long_chain_help and shell_read:
            seen = getattr(st, "shell_read_sigs", None)
            if seen is None:
                seen = st.shell_read_sigs = set()
            seen.add(sig)               # cleared by any other shell command
        elif _long_chain_help:
            st.read_sigs_seen.add(sig)
            # the lines the result HOLDS, not the ones asked for (a long range
            # is cut at 5000 lines / 60k chars)
            win = _window(name, args)
            if win and isinstance(result, dict) and result.get("start") and result.get("text") \
                    and not str(result["text"]).lstrip().startswith("[already in context:"):
                s0 = int(result["start"])
                got = s0 + str(result["text"]).count("\n") - 1
                win = (win[0], s0, min(int(result.get("end") or got), max(s0, got)))
            elif win:
                win = None              # no line range came back: record nothing
            if win:
                wins = getattr(st, "read_windows", None)
                if wins is None:
                    wins = st.read_windows = {}
                wins[sig] = win
                while len(wins) > _ACTION_SIG_MAX:
                    wins.pop(next(iter(wins)))


def _note_target_compare(st, name, args, result) -> list:
    """A compare command on a target file: a pass closes the target's
    enforced item; a fail is recorded on the target (the facts line says
    "differs") and the item stays open. A compare left running and checked
    on later counts as its original command, like a test run."""
    from aiforge_core.runtime import cmd_finished, shell_writes, targets
    run = cmd_finished.as_run(name, args, result)
    if run is None:
        return []
    name, args, result = run
    if name not in shell_writes.SHELL_TOOLS or not isinstance(result, dict):
        return []
    sid = getattr(st, "session_id", None)
    cmd = shell_writes.command_of(args or {})
    tid = targets.is_compare(cmd, sid)
    if not tid:
        return []
    out = str(result.get("stdout") or result.get("stderr") or "")[:400]
    code = result.get("code")
    if result.get("ok") is not True:
        # Only the command's own verdict counts: a job the user or a guard
        # stopped, or one with no exit code, proves nothing either way.
        if code is not None and not result.get("stopped"):
            targets.note_compare(sid, tid, False,
                                 f"{cmd[:200]} → exit {code}{(' · ' + out) if out else ''}")
        return []
    board = getattr(st, "board", None) or {}
    if tid not in board or board[tid].get("status") == "done":
        return []
    targets.note_compare(sid, tid, True,
                         f"{cmd[:200]} → exit 0{(' · ' + out) if out else ''}")
    before = board[tid].get("status")
    board[tid]["status"] = "done"
    board[tid]["note"] = "compare passed: " + cmd[:120]
    ev = None
    try:
        from ._evidence import on_status
        ev = on_status(st, tid, before, "done")
    except Exception:  # noqa: BLE001
        pass
    return [{"type": "subtask_update", "slug": tid, "status": "done",
             **({"evidence": ev} if ev else {})}]


def _post_tool(st, name, args, result, cwd, sig, n, _long_chain_help, _bundle):
    """Post-tool bookkeeping: PostToolUse hook, emit the tool result, count landed
    reads/edits (feeding the progress + verify gates), post-edit syntax self-
    check, builder-finalize signal, and append the (smart-truncated) OBSERVATION.
    Returns "return" when the same-failure rule paused the turn, else None."""
    # PostToolUse hook (best-effort, never blocks).
    try:
        from aiforge_core.runtime import hooks as _hooks
        _post = _hooks.fire("PostToolUse",
                            {"tool": name, "args": args, "result": result}, cwd)
        # held until this call's result is written (_turn/_notes.py); the
        # store side of note_into still runs
        _hook_note = _hooks.context_note(_post)
        if _hook_note:
            from ._notes import hold
            hold(st, f"[hook PostToolUse — not the user]\n{_hook_note}")
        _hooks.note_into(None, "PostToolUse", _post)
    except Exception:  # noqa: BLE001 — hooks must never break the turn
        pass
    yield {"type": "tool", "name": name, "args": args, "result": result,
           "call_id": n}
    # Loop-engineering bookkeeping: count edits that actually LANDED (gates
    # the verify-on-final loop — a 0-edit Q&A turn is never test-gated).
    # Remember a successful read so a later identical re-read short-circuits.
    _record_read(st, name, sig, result, _long_chain_help, args)
    _forget_after_failed_edit(st, name, args, result)
    from ._evidence import note as _note_evidence
    _note_evidence(st, name, args, result)
    if name in _READ_OBS_TOOLS:
        _safely(note_read, st, args, result, cwd)
    _safely(note_command, st, name, result, cwd)
    _safely(note_identical, st, sig, result)
    from .._guards.external import note_external
    _safely(note_external, st, name, result)
    yield from _record_edit(st, name, args, result, cwd)
    # Builder finalize: a successful create_job_script / learn_skill /
    # learn_workflow / remember_rule ends the interview. Signal the UI so it
    # can drop this session's builder mode — otherwise every later message
    # re-fires the charter and the user is stuck building forever (and can be
    # walked into duplicate artifacts).
    if name in _FINALIZE_TOOLS and isinstance(result, dict) and result.get("ok"):
        st.builder_finalized = True
        yield {"type": "builder_done", "kind": name}
    # A blocked / unreachable call tells the model to change approach instead
    # of retrying or routing around the block (see _blocked). The guidance
    # goes FIRST so a long result cannot truncate it away.
    from .._blocked import for_model as _blocked_for_model
    _seen = _blocked_for_model(st, name, result)
    if _seen is not result:
        result = {"next_step": _seen["next_step"], **result}
    # The UI event above already carried the raw result. What the model sees
    # drops a skill, workflow, OKF page, or memory hit whose identical body
    # is already in this turn. A changed body is kept whole.
    from aiforge_core.runtime.context_seen import (
        carries_fresh_body, dedupe_tool_result)
    try:
        model_result = dedupe_tool_result(st.convo, name, args, result, cwd)
    except Exception:  # noqa: BLE001 — a dedupe miss must still deliver the result
        model_result = result
    from .._shell import _MAX_OBS_TEST, compact_test_output, is_test_command
    _test_run = is_test_command(name, args)
    _obs_cap = _MAX_OBS_READ if (
        name in _READ_OBS_TOOLS or carries_fresh_body(name, model_result)
    ) else (_MAX_OBS_TEST if _test_run else _MAX_OBS)
    if _test_run and isinstance(model_result, dict) \
            and isinstance(model_result.get("stdout"), str):
        # (what the model reads; the UI event above had the whole output)
        model_result = {**model_result, "stdout": compact_test_output(
            model_result["stdout"], _MAX_OBS_TEST - 1000)}
    # Content-READ tools: cut oversized documents at a STRUCTURE boundary
    # (chonkie) with a continuation note, instead of a blunt slice that
    # hands the model a broken JSON/sentence tail. Others keep the slice.
    # File reads and commands are plain text (raw lines, or exit code then
    # the stderr tail then the stdout tail). Other tools stay JSON so a
    # structured field such as next_step is still parseable. Deduped
    # bodies (same skill/OKF/memory hit already in this turn) stay short.
    # A recipe rewrite (or ignored recipe) marked the args in the loop; the
    # observation header reads the marks off the result.
    if isinstance(model_result, dict) and isinstance(args, dict):
        for k in ("_recipe", "_recipe_was", "_recipe_warn"):
            if args.get(k):
                model_result[k.lstrip("_")] = args[k]
    obs = render_observation(name, model_result, _obs_cap)
    _note_green_tests(st, name, args, result, cwd)
    # A compare command that passed on a target file: the harness closes
    # that target's enforced board item (the model cannot).
    for _ev in (_safely(_note_target_compare, st, name, args, result) or []):
        yield _ev
    note_evidence(st, name, args, result)
    # The same failure after a different fix, again: nudge once (the nudge
    # rides on this observation), then stop and ask.
    # Steps that change nothing, read nothing new and move no test, again
    # and again (varied scripts guessing a hash): the same plumbing.
    # Where a git write landed (repository + branch), so a commit in the wrong
    # place is visible to the model at once; also records where it works.
    _git_line = _safely(_git_target_after, st, name, args, result, cwd)
    if _git_line:
        obs = f"{obs}\n{_git_line}"
    seen = _safely(note_failure, st, name, args, result)
    idle = _safely(note_step, st, name, args, result)
    seen = seen or idle
    if seen and seen[0] == "stop":
        from .._obs_vision import observation_content as _obs_content
        st.convo.append({"role": "user",
                         "content": _obs_content(obs, model_result)})
        from ._notes import flush as _flush
        _flush(st)
        _r = yield from finish_stuck(
            st, "No progress after the warning: " + str(seen[1])[:300],
            Pause(seen[1], "⛔ no progress after the warning — pausing"))
        return None if _r == "continue" else _r
    if seen:
        yield {"type": "thought", "role": "system",
               "text": "↺ going round without progress — asking why before "
                       "another try"}
    # Recency reminder: a strict output format from an APPLICABLE SKILL sits
    # in the system prompt (far above), while this fresh tool result sits at
    # the end where the model attends most — so after a tool round-trip it
    # tends to summarize the result in its own words and drop the format
    # (e.g. a jira-reading skill's exact layout). Re-assert the format right
    # next to the data so the FINAL honours it. Only when a skill fired.
    _tail = ("\n[format reminder] If your FINAL presents this result and an "
             "APPLICABLE SKILL above specifies an output format, reproduce "
             "it EXACTLY — no extra prose, headers, or table it does not "
             "specify.") if _bundle.skills_md else ""
    # A loop note goes to the model as its own message after the result, not
    # inside it. Replays of a recorded sqlglot run (11-08): the same note
    # appended to the tool result moved 1 of 8 samples off a read streak; as
    # its own message, 8 of 8 ran the tests.
    _notes = []
    if seen:
        _notes.append(seen[1])
    try:
        from ._probe_guard import note as _probe_note
        _probe = _probe_note(st, name, args)
    except Exception:  # noqa: BLE001 — a nudge never ends a turn
        _probe = ""
    if _probe and not seen:          # one nudge per observation
        yield {"type": "thought", "role": "system",
               "text": ("↺ many reads and no edit or test — asking the agent to run the tests"
                        if " reads in a row" in _probe else
                        "↺ many shell commands and no edit — asking the agent to write the change")}
        _notes.append(_probe)
        if getattr(st, "want_force_write", False):
            # armed only with its explanation in the observation
            from .._native import force_write_next
            force_write_next(getattr(st, "session_id", None))
    elif getattr(st, "want_force_write", False):
        # the note gave way to a stuck notice: force at the very next command
        from ._probe_guard import FORCE_AT
        st.shell_streak = FORCE_AT - 1
    st.want_force_write = False
    if _test_run:
        # the same tests failed again after a fix: which added lines ran
        from ._trace_hint import after_test
        _trace = _safely(after_test, st, name, args, result, cwd)
        if _trace:
            yield {"type": "thought", "role": "system",
                   "text": "🔎 same tests still failing — traced which of the new lines ran"}
            _notes.append(_trace)
    from .._obs_vision import observation_content as _obs_content
    st.convo.append({"role": "user",
                     "content": _obs_content(f"{obs}{_tail}", model_result)})
    # notes held during this step (the post-edit error check) and the loop
    # notes: after the result, its own message in native mode (_turn/_notes.py)
    from ._notes import flush, send
    flush(st)
    for _n in _notes:
        send(st, _n)
    return None


def _rewrite_gate(st, name, args, cwd):
    """A whole-file write that would drop lines this turn added to the file
    is refused (``_rewrite_guard``). Returns "continue" when refused."""
    from ._rewrite_guard import refusal
    lost = refusal(st, name, args, cwd)
    if lost is None:
        return None
    yield {"type": "thought", "role": "system",
           "text": "⛔ refused a whole-file write that would drop this turn's own edits"}
    yield {"type": "tool", "name": name, "args": args, "result": lost}
    st.convo.append({"role": "user", "content": f"OBSERVATION: {json.dumps(lost)}"})
    return "continue"


def _discard_gate(st, name, args, cwd):
    """A shell command that would throw away this turn's own edits (an
    `rm core.py` / `git checkout .` reset mid-run) is refused before any
    approval is asked. Returns "continue" when refused."""
    from ._discard_guard import refusal
    gone = refusal(st, name, args, cwd)
    if gone is None:
        return None
    yield {"type": "thought", "role": "system",
           "text": "⛔ refused a command that would discard this turn's own edits"}
    yield {"type": "tool", "name": name, "args": args, "result": gone}
    st.convo.append({"role": "user", "content": f"OBSERVATION: {json.dumps(gone)}"})
    return "continue"
