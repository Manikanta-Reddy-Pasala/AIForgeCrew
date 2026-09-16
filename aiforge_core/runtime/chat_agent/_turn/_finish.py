"""Handling the model's non-tool replies: finals (verify-on-final, false-claim
guard, final nudges, turn summary, next-step suggestion) and narration
without an action."""
from __future__ import annotations

import subprocess

from .._context import (
    _claims_file_edits,
    _edit_claim_disclaimer,
    _edit_claim_guard_enabled,
    _edit_claim_nudge,
    _fire_stop,
    _repo_name,
    _run_project_verify,
    _text_of,
    _verify_fix_message,
    _verify_max_rounds,
    _verify_on_final_enabled,
    _worktree_fingerprint,
)
from .._prompt import _strip_reasoning_prefix
from .._registry import (
    _BUILDER_FINALIZE_TOOL,
)
from ._shared import (
    _THE_FINALIZE_TOOL,
    _log,
)
from ._tasks import board_nudge_allowed, open_planned, unfinished_reminder


def _verify_on_final(st, cwd, plan_mode, builder):
    """Progress-gated verify->fix on FINAL: only an act-mode run that edited
    files with a real suite; loop while failures drop, else accept honestly.
    Returns continue or None."""
    # A + B: enforced verify→fix on FINAL (progress-gated). Only for an
    # act-mode run that actually EDITED files with a real test suite —
    # a Q&A turn (0 edits) or read-only plan mode is untouched. Keep
    # looping while the failure count DROPS; once it stalls (2 rounds no
    # improvement) accept the HONEST still-failing final rather than
    # churn. This gives simple/doer runs the pipeline's no-false-green
    # guarantee. Opt out: AIFORGE_CHAT_VERIFY_ON_FINAL=0.
    if (not plan_mode and not builder and st.edits_made > 0
            and st.verify_rounds < _verify_max_rounds()
            and _verify_on_final_enabled()):
        yield {"type": "thought", "role": "system",
               "text": "⧗ running the project's checks before finishing…"}
        _vok, _vout = _run_project_verify(cwd)
        if _vok is False:
            try:
                from aiforge_core.runtime.parallel_subtasks import _fail_count
                _fails = _fail_count(_vout)
            except Exception:  # noqa: BLE001
                _fails = 1
            if st.verify_prev_fails is not None and _fails >= st.verify_prev_fails:
                st.verify_stalls += 1
            else:
                st.verify_stalls = 0
            st.verify_prev_fails = _fails
            if st.verify_stalls < 2:
                st.verify_rounds += 1
                yield {"type": "thought", "role": "system",
                       "text": f"✗ tests failing ({_fails}) — fixing "
                               f"(verify round {st.verify_rounds}/"
                               f"{_verify_max_rounds()})…"}
                st.convo.append({"role": "user",
                              "content": _verify_fix_message(_vout)})
                return "continue"
            yield {"type": "thought", "role": "system",
                   "text": f"⚠ tests still failing ({_fails}) after "
                           f"{st.verify_rounds} fix rounds — stopping with "
                           "the honest state."}
    return None


def _claim_guard(st, step, cwd, readonly_mode, builder, _wt_fp0):
    """Claim-vs-reality guard: when the model claims edits but landed zero and
    the tree is unchanged, nudge it to write (bounded); on the last try prepend
    an honest disclaimer. Returns continue or None."""
    # Claim-vs-reality guard: the model asserts it edited/created files
    # but landed ZERO edits this turn AND the working tree is unchanged
    # (checked against every tool + any on-disk write, not just counted
    # ones) — a hallucinated tool-use surfaced as prose (the frequent
    # "I applied the fix to X / Confirmed Fixes Applied" with no diff).
    # Nudge it to actually write (bounded); if it still won't, prepend an
    # honest note so the user is never told a change landed that didn't.
    # Opt out: AIFORGE_CHAT_EDIT_CLAIM_GUARD=0.
    # Disk cross-check: "" = no git signal (honor the contract — NOT
    # "clean"), so in a non-git workspace we rely on _edits_made==0 alone;
    # with git, fire only when the tree is UNCHANGED (a real write would
    # have dirtied it — an incidental dirty tree suppressing the guard is
    # an accepted conservative miss).
    _wt_now = (_worktree_fingerprint(cwd)
               if _edit_claim_guard_enabled() else "")
    _no_landed_write = (_wt_now == "" or _wt_now == _wt_fp0)
    if (not readonly_mode and not builder and st.edits_made == 0
            and _edit_claim_guard_enabled()
            and _claims_file_edits(step.get("text") or "")
            and _no_landed_write):
        if st.edit_claim_nudges < 2:
            st.edit_claim_nudges += 1
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            yield {"type": "thought", "role": "system",
                   "text": "⚠ you described file edits but no write ran "
                           "and nothing changed on disk — applying for "
                           "real…"}
            st.convo.append({"role": "user", "content": _edit_claim_nudge()})
            return "continue"
        step["text"] = _edit_claim_disclaimer(step.get("text") or "")
    return None


def _final_nudges(st, step, builder, strict_finish, _asks):
    """Pre-accept FINAL nudges: builder-not-finalized reminder, implicit-final
    doer nudge (strict_finish), and the one-time multi-ask completeness gate.
    Returns continue to loop again, or None to proceed."""
    # In a builder session, a "final" BEFORE the finalize tool succeeded
    # means the model narrated/stalled ("let me test what's happening…")
    # instead of building the artifact — don't end the interview with
    # nothing created. Nudge it to call the finalize tool and continue the
    # loop (bounded so a model that truly can't finalize still exits).
    if builder and not st.builder_finalized and st.builder_final_tries < 2:
        st.builder_final_tries += 1
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        if step.get("text"):
            yield {"type": "thought", "text": step["text"]}
        st.convo.append({"role": "user", "content":
            f"[system reminder] You stopped without creating the {builder}. "
            f"Call `{_fin}` NOW with the collected values to finish — do "
            f"not just narrate or 'test'. If ONE required value is genuinely "
            f"missing, ask only for that, then finalize."})
        return "continue"
    # Doer guard: an IMPLICIT final (bare prose, no explicit `FINAL:`
    # marker) from a work-producing run (strict_finish — the text-doer /
    # subtask path) is almost always premature narration ("let me test…"),
    # not a real answer. Nudge to act/finish instead of ending with no work.
    # Bounded by continue_nudges so a model that truly can't finish still
    # exits. Interactive chat / generic callers (strict_finish=False) keep
    # bare prose as the legitimate answer — unchanged.
    if step.get("implicit") and strict_finish and not builder:
        st.continue_nudges += 1
        if st.continue_nudges <= 2:
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            st.convo.append({"role": "user", "content":
                "You narrated but did NOT emit an ACTION or an explicit "
                "`FINAL:` line. Continue: take the next ACTION (tool call) "
                "to make progress, or output `FINAL: <answer>` ONLY when "
                "the work is actually done. Do not just narrate or 'test'."})
            return "continue"
    # Task board gate: the model planned items and some are still open. A
    # long run must not stop to report half the work; bounded so a model
    # that cannot finish still exits.
    if (st.board_used and open_planned(st.board) and not builder
            and not st.readonly_mode and board_nudge_allowed(st)):
        if step.get("text"):
            yield {"type": "thought", "text": step["text"]}
        yield {"type": "thought", "role": "system",
               "text": f"☐ {len(open_planned(st.board))} task(s) still open — "
                       "continuing"}
        st.convo.append({"role": "user", "content": unfinished_reminder(st.board)})
        return "continue"
    # Multi-ask completeness gate (once): before accepting FINAL on a
    # multi-part message, make the model self-check its answer against
    # the checklist — the #1 simple-mode complaint is answering ask 1
    # and silently dropping the rest.
    if _asks and not st.multiask_checked and not builder:
        st.multiask_checked = True
        yield {"type": "thought", "role": "system",
               "text": f"✔ checking all {len(_asks)} parts of the "
                       "request are addressed…"}
        st.convo.append({"role": "user", "content":
            "[completeness check — not the user] The user's message "
            f"contained {len(_asks)} distinct asks:\n"
            + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(_asks))
            + "\nRe-read your answer above. If EVERY ask is addressed, "
            "resend it unchanged as FINAL. If any is missing, do the "
            "missing work now (ACTIONs as needed) and produce ONE "
            "complete FINAL covering all parts, numbered."})
        return "continue"
    return None


def _is_clean_tree(cwd) -> bool:
    """True only when ``cwd`` IS a git repo AND has nothing uncommitted.

    Deliberately NOT ``_worktree_fingerprint(cwd) == ""``: that helper returns
    "" for a clean tree *and* for "not a git repo / git unavailable", and its
    own docstring warns that "" means "no signal", never "clean". Reusing it
    here would let a tier-2 prediction act automatically in a directory with no
    undo at all — precisely the case the clean-tree rule exists to exclude.

    Anything unclear answers False, which costs an offer instead of an action.
    """
    if not cwd:
        return False
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(cwd),
                             capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 — a git hiccup must never break the turn
        return False
    return out.returncode == 0 and not out.stdout.strip()


def _turn_summary(st) -> str:
    """One line naming what this turn actually did, for the prediction prompt.

    Read off ``action_counts``, which the loop already maintains — the
    alternative is a second tally that drifts from the first.
    """
    try:
        counts = getattr(st, "action_counts", None) or {}
        names = list(dict.fromkeys(str(k).split("|", 1)[0]
                                   for k, v in counts.items() if v))
    except Exception:  # noqa: BLE001
        return ""
    return ", ".join(names[:8])


def _last_user_message(st) -> str:
    try:
        for m in reversed(list(getattr(st, "convo", []) or [])):
            if isinstance(m, dict) and m.get("role") == "user":
                # _text_of takes the whole MESSAGE, not its content: it handles
                # the multimodal list form a vision turn rewrites content into.
                return _text_of(m)[:2000]
    except Exception:  # noqa: BLE001 — a malformed convo predicts nothing
        return ""
    return ""


def _predict_next_step(message: str, did: str, cwd):
    """The prediction, or None. Split out so a test can replace exactly this.

    The kill switch is honoured BEFORE the context is gathered. ``_is_clean_tree``
    shells out to git on every turn end, so building the argument first meant a
    disabled feature still paid for a subprocess per turn — "off" has to mean
    it costs nothing, not merely that it emits nothing.
    """
    from aiforge_core.runtime import next_step
    from aiforge_core.runtime.next_step import _predict as _np

    if _np._disabled():
        return None
    return next_step.predict({"message": message, "did": did,
                              "repo": _repo_name(str(cwd or "")),
                              "clean_tree": _is_clean_tree(cwd)})


def _emit_suggestion(message: str, did: str, cwd):
    """Yield at most one ``suggestion`` event. Never raises.

    Emitted AFTER the answer and before ``done`` — the same ordering
    ``plan_ready`` uses. The user reads what they asked for either way, so a
    prediction that is slow, wrong or broken costs them nothing.
    """
    try:
        p = _predict_next_step(message, did, cwd)
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        _log.debug("next_step: prediction skipped: %s", exc)
        return
    if p is not None:
        yield p.as_event()


def _handle_final(st, step, builder, strict_finish, plan_mode, readonly_mode,
                  cwd, _asks, _wt_fp0):
    """Handle a FINAL step: builder-not-finalized nudge, implicit-final doer
    nudge, multi-ask completeness gate, claim-vs-reality guard, and the
    progress-gated verify→fix loop — then accept (fire stop + emit the answer).
    Returns "continue"/"return"."""
    _sig = yield from _final_nudges(st, step, builder, strict_finish, _asks)
    if _sig == "continue":
        return "continue"
    _sig = yield from _claim_guard(st, step, cwd, readonly_mode, builder, _wt_fp0)
    if _sig == "continue":
        return "continue"
    _sig = yield from _verify_on_final(st, cwd, plan_mode, builder)
    if _sig == "continue":
        return "continue"
    # FINAL accepted on a multi-part turn: close out the tracker so
    # the dock never ends with stale pending items the model forgot
    # to flip.
    if _asks:
        for _i in range(len(_asks)):
            _slug = f"part-{_i + 1}"
            if st.board.get(_slug, {}).get("status") in ("failed", "skipped"):
                continue            # the model said so; don't overwrite it
            yield {"type": "subtask_update", "slug": _slug, "status": "done"}
    _fire_stop("final", cwd)
    yield {"type": "message", "text": _strip_reasoning_prefix(step["text"])}
    yield from _emit_suggestion(_last_user_message(st), _turn_summary(st), cwd)
    yield {"type": "done"}
    return "return"


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
