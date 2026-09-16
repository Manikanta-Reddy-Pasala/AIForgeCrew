"""Before and after a tool call: continue steps, the stall guard, plan and
scope gates, the workspace jail, and read/edit bookkeeping."""
from __future__ import annotations

import json
import os

from .._context import (
    _EDIT_TOOL_NAMES,
    _LOOP_REPEAT,
    _fire_stop,
    _post_edit_syntax_error,
    _progress_recap,
    _stuck_recovery_max,
)
from .._preview import _diff_preview
from .._registry import (
    _BUILDER_FINALIZE_TOOL,
    _FINALIZE_TOOLS,
    _READONLY_TOOLS,
)
from .._shell import _MAX_OBS, _MAX_OBS_READ, _READ_OBS_TOOLS, _smart_truncate_obs
from ._approval import (
    _handle_rejection,
)
from ._shared import (
    _ACTION_SIG_MAX,
    _THE_FINALIZE_TOOL,
)


def _handle_continue_step(st, step, builder, cwd):
    """Handle a continue step (narrated-no-action, or empty_final signalled
    completion with no answer): nudge appropriately (bounded), else stop cleanly.
    Returns continue/return."""
    # Two shapes land here. (a) The model narrated a next step
    # (THOUGHT) but emitted no ACTION — truncated turn or dropped
    # protocol line. (b) reason="empty_final": it SIGNALLED completion
    # and wrote no answer, which used to publish the marker itself as
    # the reply ("ACTION: FINAL" in the chat). Both are nudged; the
    # wording differs because the missing thing differs.
    _empty_final = step.get("reason") == "empty_final"
    if step.get("thought"):
        yield {"type": "thought", "text": step["thought"]}
    st.continue_nudges += 1
    if st.continue_nudges > 2:
        # It keeps not delivering — stop cleanly rather than loop to
        # the safety cap.
        _fire_stop("no_action", cwd)
        if _empty_final:
            # Deliberately NOT "I finished the work": zero tools may
            # have run, and text_doer / analysis_pipeline treat a
            # message that does NOT start with "(stopped:" as a clean
            # outcome — so claiming completion here would poison the
            # pipeline's own quality record. Deliberately no
            # _progress_recap either: that is model-facing text, and it
            # tallies the FINAL markers themselves.
            yield {"type": "message", "text":
                   "(stopped: I signalled I was done but never wrote "
                   "the reply. Ask me to summarise what happened and "
                   "I'll write it up.)"}
        else:
            yield {"type": "message",
                   "text": (step.get("thought") or "").strip()
                   or "I described a next step but couldn't complete the "
                      "action. Could you rephrase or narrow the request?"}
        yield {"type": "done"}
        return "return"
    if _empty_final and builder:
        # A builder session's "answer" is an ARTIFACT: it must call its
        # finalize tool. Telling it "reply with FINAL, do not emit
        # ACTION" is the exact opposite instruction, and the turn would
        # end claiming success with nothing created.
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        st.convo.append({"role": "user", "content":
                      f"You signalled you were finished but never called "
                      f"`{_fin}`, so nothing was created. Call `{_fin}` NOW "
                      f"with the values you have collected."})
    elif _empty_final:
        # The work is done; what is missing is the reply. "Emit an
        # ACTION" is the wrong instruction for that.
        st.convo.append({"role": "user", "content":
                      "You signalled you were finished but wrote no "
                      "answer — the user saw nothing. Reply now with "
                      "`FINAL: <answer>` where <answer> tells them what "
                      "you did and what it means for their request, in "
                      "plain prose. Do not emit ACTION, THOUGHT or any "
                      "other marker."})
    else:
        st.convo.append({"role": "user",
                      "content": "You described your next step but did NOT "
                      "emit an ACTION. Continue now — output the next ACTION "
                      "(tool call) to make progress, or `FINAL: <answer>` if "
                      "you are genuinely done. Do not just narrate."})
    # NOT `n += 1`: the loop head already counted this iteration, and
    # the sibling implicit-final nudge does not double-charge either.
    # On a 6-step Quick turn the double charge turned one nudge into a
    # "used up Quick mode's step budget" stop.
    return "continue"


def _action_stall_guard(st, name, args, sig, _long_chain_help):
    """Stall guard for an action: short-circuit a duplicate read with a progress
    recap, and on the same tool+args repeated too often first recover with a
    recap+nudge (bounded), else pause for the user. Returns continue/return/None."""
    # Duplicate-READ short-circuit: a local model on a long sweep re-issues a
    # read it already ran (its result is still above in the convo). Don't
    # re-execute or count it toward the stuck guard — hand back a cheap
    # progress recap that points at the next unread file / the write step, so
    # every read must make NEW progress. Cleared on any edit (a file just
    # written is worth re-reading). Disabled with the same env switch as the
    # recovery nudge (AIFORGE_CHAT_STUCK_RECOVERIES=0 → full legacy behaviour).
    if _long_chain_help and name in _READ_OBS_TOOLS and sig in st.read_sigs_seen:
        _recap = _progress_recap(st.convo)
        yield {"type": "thought", "role": "system",
               "text": f"⏭ duplicate read skipped ({name})"}
        st.convo.append({"role": "user", "content":
            "OBSERVATION: [skipped — duplicate] You ALREADY ran this exact "
            "read; its result is above and re-reading wastes a step. "
            + (_recap + ". " if _recap else "")
            + "Read a DIFFERENT file you have not read yet, or if you have "
            "enough, WRITE your output now (file_write) or emit FINAL."})
        return "continue"
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
    st.action_counts[sig] = st.action_counts.get(sig, 0) + 1
    st.action_counts.move_to_end(sig)
    while len(st.action_counts) > _ACTION_SIG_MAX:
        st.action_counts.popitem(last=False)
    if st.action_counts[sig] >= _LOOP_REPEAT:
        # A local model on a long chain re-issues an action it already ran —
        # most often re-reading a file it read earlier (it lost track over the
        # growing history), which the old hard bail turned into an abandoned
        # task. Recover FIRST: recap what's already done + point at the next
        # step (bounded); only give up if the model keeps repeating.
        if st.stuck_recoveries < _stuck_recovery_max():
            st.stuck_recoveries += 1
            st.action_counts[sig] = 0          # clear this action's strike count
            _recap = _progress_recap(st.convo)
            yield {"type": "thought", "role": "system",
                   "text": f"↺ repeated `{name}` — recap + nudge to continue"}
            st.convo.append({"role": "user", "content":
                f"[loop guard — not the user] You already ran `{name}` with "
                "these exact args and its result is ABOVE — repeating it makes "
                "no progress. "
                + (_recap + ". " if _recap else "")
                + "Do the NEXT, DIFFERENT step now: act on something not yet "
                "done (e.g. the next unread file from the request), or output "
                "`FINAL: <answer>` if everything is complete. Do NOT repeat a "
                "previous action."})
            return "continue"
        yield {"type": "message", "awaiting_input": True,
               "text": f"I keep trying the same step (`{name}`) without "
                       "progress. I've paused — could you clarify or tell "
                       "me how you'd like me to proceed?"}
        yield {"type": "done"}
        return "return"
    return None


def _pre_dispatch_gates(st, name, args, readonly_mode, analyze_mode):
    """Pre-dispatch bookkeeping gates: plan_progress flips a UI subtask (pure
    bookkeeping, allowed in every mode); read-only Plan/Analyze mode blocks a
    mutating tool. Returns continue or None."""
    # Simple-mode task tracker: plan_progress flips a checklist item in
    # the UI's subtasks dock. Pure bookkeeping — no side effects, allowed
    # in every mode (incl. plan), never gated.
    if name == "plan_progress":
        _slug = str(args.get("slug") or args.get("part") or "").strip()
        _st = str(args.get("status") or "done").strip().lower()
        if _st not in ("pending", "running", "done", "failed"):
            _st = "done"
        if _slug:
            yield {"type": "subtask_update", "slug": _slug, "status": _st}
        result = {"ok": bool(_slug), "slug": _slug, "status": _st,
                  **({} if _slug else {"error": "missing 'slug'"})}
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


def _workspace_jail(st, name, args, cwd):
    """Workspace jail (on by default). The session's cwd is otherwise only a
    DEFAULT: an absolute path in a mutating file tool (or, in a chat, a shell
    write) lands anywhere, and an off-topic recall must never turn into an edit
    in a repo the user never brought into this chat. Interactive: ASK the user
    (one click grants the folder for the rest of this chat). Unattended: refuse
    without writing. Returns "continue"/"return" to skip the call, else None."""
    interactive = getattr(st, "session_id", None) is not None
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
    _allowed = [cwd] + _roots
    result = {
        "ok": False, "error": "outside_workspace",
        "blocked_paths": _jailed, "allowed_folders": _allowed,
        "hint": ("Write refused: this unattended run may only write in "
                 + ", ".join(map(str, _allowed)) + ". Do NOT write there "
                 "another way (a shell redirect, cp, mv); do the work "
                 "inside those folders or report that it needs a folder "
                 "outside them."),
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
    """When an edit tool landed: bump the edit counter, invalidate the duplicate-
    read guard, and run a post-edit syntax self-check that surfaces + feeds back
    any error this step. Yields the syntax-warning events."""
    if name in _EDIT_TOOL_NAMES and not (
            isinstance(result, dict) and result.get("ok") is False):
        st.edits_made += 1
        st.read_sigs_seen.clear()   # a file just changed → re-reads are valid again
        # D: post-edit self-check. Immediately syntax-check the file just
        # written and, if broken, hand the model the error THIS step (tight
        # feedback) instead of letting it surface only at the end-of-run test
        # gate. Best-effort + opt-out (AIFORGE_CHAT_POST_EDIT_CHECK=0).
        if os.environ.get("AIFORGE_CHAT_POST_EDIT_CHECK", "1") not in ("0", "false"):
            try:
                _pe = _post_edit_syntax_error(name, args, cwd)
            except Exception:  # noqa: BLE001
                _pe = None
            if _pe:
                yield {"type": "thought", "role": "system",
                       "text": f"⚠ syntax error in the file you just edited "
                               f"— fix it now: {_pe[:160]}"}
                st.convo.append({"role": "user", "content":
                    "[automated syntax check — not the user] The file you just "
                    f"wrote has a syntax error; fix it before continuing:\n{_pe[:600]}"})


def _record_read(st, name, sig, result, _long_chain_help):
    """Count a landed READ as new progress the first time its signature is seen
    (feeds the extension budget), and remember it for the duplicate-read guard."""
    if (name in _READ_OBS_TOOLS and not (
            isinstance(result, dict) and result.get("ok") is False)):
        # F2: counted OUTSIDE the _long_chain_help gate —
        # AIFORGE_CHAT_STUCK_RECOVERIES=0 turns off the duplicate-read
        # NUDGE, and must not silently delete half the progress signal with
        # it (a read-only research turn would never extend on that box).
        if sig not in st.read_sigs_ever:
            st.reads_new += 1        # real progress: knowledge it did not have
            st.read_sigs_ever[sig] = True
            while len(st.read_sigs_ever) > _ACTION_SIG_MAX:
                st.read_sigs_ever.popitem(last=False)
        if _long_chain_help:
            st.read_sigs_seen.add(sig)


def _post_tool(st, name, args, result, cwd, sig, n, _long_chain_help, _bundle):
    """Post-tool bookkeeping: PostToolUse hook, emit the tool result, count landed
    reads/edits (feeding the progress + verify gates), post-edit syntax self-
    check, builder-finalize signal, and append the (smart-truncated) OBSERVATION."""
    # PostToolUse hook (best-effort, never blocks).
    try:
        from aiforge_core.runtime import hooks as _hooks
        _hooks.fire("PostToolUse",
                    {"tool": name, "args": args, "result": result}, cwd)
    except Exception:  # noqa: BLE001 — hooks must never break the turn
        pass
    yield {"type": "tool", "name": name, "args": args, "result": result,
           "call_id": n}
    # Loop-engineering bookkeeping: count edits that actually LANDED (gates
    # the verify-on-final loop — a 0-edit Q&A turn is never test-gated).
    # Remember a successful read so a later identical re-read short-circuits.
    _record_read(st, name, sig, result, _long_chain_help)
    yield from _record_edit(st, name, args, result, cwd)
    # Builder finalize: a successful create_job_script / learn_skill /
    # learn_workflow / remember_rule ends the interview. Signal the UI so it
    # can drop this session's builder mode — otherwise every later message
    # re-fires the charter and the user is stuck building forever (and can be
    # walked into duplicate artifacts).
    if name in _FINALIZE_TOOLS and isinstance(result, dict) and result.get("ok"):
        st.builder_finalized = True
        yield {"type": "builder_done", "kind": name}
    _obs_cap = _MAX_OBS_READ if name in _READ_OBS_TOOLS else _MAX_OBS
    # A blocked / unreachable call tells the model to change approach instead
    # of retrying or routing around the block (see _blocked). The guidance
    # goes FIRST so a long result cannot truncate it away.
    from .._blocked import for_model as _blocked_for_model
    _seen = _blocked_for_model(st, name, result)
    if _seen is not result:
        result = {"next_step": _seen["next_step"], **result}
    # Content-READ tools: cut oversized documents at a STRUCTURE boundary
    # (chonkie) with a continuation note, instead of a blunt slice that
    # hands the model a broken JSON/sentence tail. Others keep the slice.
    obs = (_smart_truncate_obs(result, _obs_cap)
           if name in _READ_OBS_TOOLS else json.dumps(result)[:_obs_cap])
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
    st.convo.append({"role": "user", "content": f"OBSERVATION: {obs}{_tail}"})
