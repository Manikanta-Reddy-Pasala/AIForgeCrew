"""Per-step guards and mid-run input: step cap and deadline extensions, the
builder nudge, steering, condensing, and the stuck-output guard."""
from __future__ import annotations

import time

from .._context import (
    _OUTPUT_REPEAT,
    _compact_convo,
    _ctx_budget_chars,
    _fire_stop,
    _progress_recap,
    _text_of,
    _worktree_fingerprint,
)
from .._registry import (
    _BUILDER_FINALIZE_TOOL,
    _BUILDER_NUDGE_AFTER,
)
from ._batch import (
    _rebase_batch,
    _unread_batch_msgs,
)
from ._convo import (
    _append_directive,
)
from ._progress import may_recover
from ._shared import (
    _THE_FINALIZE_TOOL,
)
from ._tasks import pin_board, turn_pin


def _may_extend(st, n):
    """True when this turn EARNED another budget: extensions remain and it
    produced NEW work (a landed edit, or a read of something not read before)
    since the last one. Both counters are monotonic, so a spinning agent leaves
    the mark unchanged and is stopped. Consumes one extension when True."""
    if st.granted_at_step == n:
        return True
    if st.extensions_used >= st.ext_budget:
        return False
    _fp = _worktree_fingerprint(st.cwd) if st.wt_fp0 else ""
    mark = (st.reads_new, st.edits_made, _fp)
    if mark == st.progress_mark:
        return False
    st.extensions_used += 1
    st.progress_mark = mark
    st.granted_at_step = n
    return True


def _cap_stop_reason(st):
    """The user-facing reason string when the step cap stops a run, naming the
    knob that actually applied (Quick max_steps / unattended cap / safety cap)."""
    if st.caller_cap is not None:
        _why = (f"(stopped: used up Quick mode's {st.safety}-step "
                "budget — send it again with Quick off, or raise "
                "AIFORGE_CHAT_QUICK_STEPS)")
    elif st.unattended:
        # The operator may have set the step cap to 0; saying
        # "raise the step cap" would send them to a knob that is
        # already off and had no say in this stop.
        _why = (f"(stopped: hit the {st.safety}-step cap for runs with "
                "nobody watching — raise the background step cap in "
                "Settings → Agent limits, or "
                "AIFORGE_CHAT_UNATTENDED_CAP)")
    else:
        _why = ("(stopped: hit the runaway safety cap — raise the "
                "step cap in Settings → Agent limits, or "
                "AIFORGE_CHAT_SAFETY_CAP; 0 = no limit — if this "
                "was real work)")
    return _why


def _step_cap_guard(st, n):
    """Runaway step-cap check: a turn still producing new work extends its step
    budget (after a forced condense); otherwise stop, naming the knob that
    actually applied (Quick / unattended / safety cap). Returns "return" to end
    the turn, or None to continue."""
    if st.capped and n > st.safety:
        # Runaway step cap. Before giving up, offer an extension to a turn
        # that is still producing new work — condense the history first so
        # the extra steps run in a clean window rather than a bloated one.
        if _may_extend(st, n):
            st.safety += st.cap_base
            _before_ext = len(st.convo)
            _unread = _unread_batch_msgs(st)
            st.convo = _compact_convo(st.convo, keep_recent=8, role=st.role,
                                   complete_fn=st.complete_fn,
                                   session_id=st.session_id, force=True,
                                   keep_min=_unread, pin=turn_pin(st),
                                   run_key=getattr(st, "compact_key", None))
            _after_condense(st, _unread, _before_ext)
            if len(st.convo) < _before_ext:
                st.read_sigs_seen.clear()   # results dropped → re-reads are valid
            _did = ("condensed the history and " if len(st.convo) < _before_ext
                    else "")
            yield {"type": "thought", "role": "system",
                   "text": f"⏳ still making progress — {_did}extended the "
                           f"step budget to {st.safety} "
                           f"({st.extensions_used}/{st.ext_budget})"}
        else:
            _fire_stop("cap", st.cwd)
            # Name the knob that ACTUALLY stopped this run. A Quick-mode
            # turn is bounded by its caller's max_steps, so pointing the
            # user at the Settings cap sends them to a number that had no
            # say — and with the cap set to 0 that number is already off.
            _why = _cap_stop_reason(st)
            yield {"type": "message", "text": _why}
            yield {"type": "done"}
            return "return"
    return None


def _deadline_guard(st, n):
    """Wall-clock turn-deadline check: a turn still landing new work buys another
    time slice (after a forced condense); otherwise stop. Returns "return" to end
    the turn, or None to continue."""
    if st.turn_deadline is not None and time.monotonic() > st.turn_deadline:
        # Same deal as the step cap: a turn still landing new work buys
        # another slice of wall clock instead of losing everything.
        if _may_extend(st, n):
            st.turn_deadline = time.monotonic() + st.turn_budget_s
            _before_ext = len(st.convo)
            _unread = _unread_batch_msgs(st)
            st.convo = _compact_convo(st.convo, keep_recent=8, role=st.role,
                                   complete_fn=st.complete_fn,
                                   session_id=st.session_id, force=True,
                                   keep_min=_unread, pin=turn_pin(st),
                                   run_key=getattr(st, "compact_key", None))
            _after_condense(st, _unread, _before_ext)
            if len(st.convo) < _before_ext:
                st.read_sigs_seen.clear()
            _did = ("condensed the history and " if len(st.convo) < _before_ext
                    else "")
            yield {"type": "thought", "role": "system",
                   "text": f"⏳ still making progress — {_did}extended the "
                           f"turn by {int(st.turn_budget_s)}s "
                           f"({st.extensions_used}/{st.ext_budget})"}
        else:
            _fire_stop("deadline", st.cwd)
            yield {"type": "message",
                   "text": f"(stopped: hit the {int(st.turn_budget_s)}s turn "
                           "time budget — raise the turn deadline in "
                           "Settings → Agent limits (or "
                           "AIFORGE_CHAT_TURN_DEADLINE_S) if this was real "
                           "long-running work)"}
            yield {"type": "done"}
            return "return"
    return None

def _drain_steering(st, session_id):
    """Fold any user-injected mid-run steering/rejections into the working
    context as ONE user turn before the next model call (merging into a trailing
    user turn to avoid two-in-a-row), and surface each as a steer event."""
    from aiforge_core.runtime import chat_interject
    # Mid-run steering (Gap A): fold any user-injected guidance into the
    # working context as a user turn BEFORE the next model call, so the
    # agent adjusts course without a Stop + new turn. Surface it so the UI
    # shows the steer was applied.
    if session_id is not None:
        _items = chat_interject.drain_items(session_id)
        if _items:
            from aiforge_core.runtime import chat_steer
            _steers = [t for k, t in _items if k != "reject"]
            st.steers.extend(_steers)
            _rejects = [t for k, t in _items if k == "reject"]
            # ONE block for everything that drained together, so three
            # queued messages cannot each claim to be the latest.
            _parts = ([chat_steer.steer_block(_steers)] if _steers else [])
            _parts += [chat_steer.reject_note(g) for g in _rejects]
            _directive = "\n\n".join(p for p in _parts if p)
            _append_directive(st, _directive)
            for _k, _t in _items:
                yield chat_steer.steer_event(_t)

def _condense_and_report(st, role, complete_fn, session_id, _meter):
    """Auto-condense the running history to stay within the window (clearing the
    duplicate-read guard + notifying once when it fires), then emit the context-
    fullness + LLM-request usage snapshot."""
    # The window is resolved ONCE for all of this (condense budget, meter,
    # source label); the events are yielded after the cache block closes.
    from .._context._window import step_cache
    with step_cache():
        events = _condense_events(st, role, complete_fn, session_id, _meter)
    yield from events


def _condense_events(st, role, complete_fn, session_id, _meter) -> list:
    # Auto-condense the running history before the call so a long session
    # can't overflow the model's context window (MUST). Tell the user it
    # happened (one-time per condense) for transparency.
    events: list = []
    _before = len(st.convo)
    _unread = _unread_batch_msgs(st)
    st.convo = _compact_convo(st.convo, role=role, complete_fn=complete_fn,
                              session_id=session_id, keep_min=_unread,
                              pin=turn_pin(st),
                              run_key=getattr(st, "compact_key", None))
    _after_condense(st, _unread, _before)
    if len(st.convo) < _before:
        # The dropped turns took their tool RESULTS with them, so a read
        # whose output is no longer in the window is no longer a duplicate.
        # Without this the guard tells the model "you already ran this, its
        # result is above" about content the condense just deleted — and the
        # turn can never recover the file it is being refused.
        st.read_sigs_seen.clear()
    if len(st.convo) < _before and not st.condensed_notified:
        st.condensed_notified = True   # notify ONCE, not every over-budget turn
        events.append({"type": "thought", "role": "system",
                       "text": "⚙ condensed earlier context to stay within the window"})
    # M3: surface how full the context window is (char-estimate; ~4 chars/
    # token) so the user can see they're approaching the condense point.
    # MUST mirror _compact_convo's math exactly (history-only sum vs a
    # budget that reserves the ACTUAL system prompt, list-safe _text_of).
    _sys_len = (len(_text_of(st.convo[0]))
                if st.convo and st.convo[0].get("role") == "system" else 0)
    _ctx_chars = sum(len(_text_of(m)) for m in st.convo[1:])
    _ctx_budget = _ctx_budget_chars(role, sys_chars=_sys_len)
    if _ctx_budget > 0:
        # ~4 chars/token. The meter shows the context against the MODEL'S
        # window ("30k / 256k") and where compaction fires.
        from .._context._window import (
            _history_fraction,
            _window_source,
            _window_tokens,
        )
        _model_win = _window_tokens(role) or (_ctx_budget + _sys_len) // 4
        _win_src = _window_source(role)[1]
        _ctx_tokens = (_ctx_chars + _sys_len) // 4          # what is sent
        _compact_at = (_ctx_budget + _sys_len) // 4
        _calls = _meter.snapshot(session_id) if _meter is not None else {}
        events.append({"type": "usage", "context_chars": _ctx_chars,
               "budget_chars": _ctx_budget,
               "context_tokens": _ctx_tokens,
               "window_tokens": _model_win,
               "window_source": _win_src,
               "compact_at_tokens": _compact_at,
               "compact_pct": round(_history_fraction(role) * 100),
               "pct": min(100, round(_ctx_tokens * 100 / max(1, _model_win))),
               # Requests actually sent to the LLM — this turn, this chat,
               # and the machine-wide rate. "Why is one question 40 calls?"
               "llm_turn": _calls.get("turn", 0),
               "llm_session": _calls.get("session", 0),
               "llm_per_min": _calls.get("per_minute", 0),
               # How many of those requests came back with nothing. A
               # subset of llm_turn, not an extra: "12 requests, 7 failing"
               # is a retry storm, "12 requests" alone looks like a
               # thorough turn.
               "llm_turn_failed": _calls.get("turn_failed", 0),
               "llm_failed_per_min": _calls.get("failed_per_minute", 0),
               # Tokens the model has WRITTEN for this message so far,
               # as the provider reported them.
               "llm_turn_tokens_out": _calls.get("turn_tokens_out", 0)})
    return events


def _after_condense(st, unread, before):
    """Re-point the batch at its results and, when the history shrank, pin
    the task board back where the model can see it."""
    _rebase_batch(st, unread)
    if len(st.convo) < before and st.board:
        pin_board(st.convo, st.board)


def _stuck_output_guard(st, out):
    """Stuck-output guard: on N identical model replies, first recover with a
    progress recap + nudge (bounded); if it keeps repeating, stop and ask the
    user. Returns continue/return/None."""
    # Stuck-output loop: identical model reply N times running. A local model
    # deep in a long tool chain (esp. a many-file read sweep) loses track and
    # re-emits an action it already ran — so FIRST recover with a progress
    # recap + "do the NEXT step" nudge (bounded); only give up if that keeps
    # failing. The old hard bail here discarded all the work done so far.
    st.recent_outputs.append(out.strip())
    if (len(st.recent_outputs) == _OUTPUT_REPEAT
            and len(set(st.recent_outputs)) == 1):
        if may_recover(st):
            st.recent_outputs.clear()          # fresh slate for the recovered plan
            _recap = _progress_recap(st.convo)
            yield {"type": "thought", "role": "system",
                   "text": "↺ repeated output — recap + nudge to continue"}
            # Append the repeated assistant turn BEFORE the nudge — else two
            # consecutive user turns (the prior OBSERVATION + this nudge)
            # break providers like claude_local.
            st.convo.append({"role": "assistant", "content": out})
            st.convo.append({"role": "user", "content":
                "[loop guard — not the user] You repeated the SAME output — "
                "that makes no progress. "
                + (_recap + ". " if _recap else "")
                + "Take the NEXT, DIFFERENT step now: act on something not yet "
                "done (e.g. the next unread file), or output `FINAL: <answer>` "
                "if the task is fully complete. Do NOT repeat a previous action."})
            return "continue"
        yield {"type": "message", "awaiting_input": True,
               "text": "I seem to be going in circles on this. Could you "
                       "clarify what you'd like me to do, or give a bit "
                       "more detail? (I stopped rather than keep retrying "
                       "the same thing.)"}
        yield {"type": "done"}
        return "return"

    return None


def _builder_nudge(st, builder, n):
    """Once a builder session has interviewed enough, inject a one-time reminder
    to call the finalize tool NOW so the session ends with an artifact."""
    # Builder nudge (#7): a local model can interview forever and never emit
    # the finalize tool, leaving the session with no artifact. Once it has had
    # enough back-and-forth, inject a one-time reminder to finalize NOW.
    if builder and not st.builder_nudged and n >= _BUILDER_NUDGE_AFTER:
        st.builder_nudged = True
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        st.convo.append({"role": "user", "content":
            f"[system reminder] You have gathered enough detail. Call "
            f"`{_fin}` NOW with the collected values to finish — do not keep "
            f"asking questions. If one required value is genuinely missing, "
            f"ask ONLY for that, then finalize."})
